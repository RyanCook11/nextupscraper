"""Official data APIs behind the school schema.

Almost none of the origin database's school fields need scraping:

* **NCAA member directory** — division, conference, athletics site URL, state.
  Public JSON, no key.
* **College Scorecard** (US Dept. of Education) — city, state, cost, SAT/ACT
  percentiles, public/private. Needs a free api.data.gov key.

Using these instead of scraping is faster, more accurate, and avoids the terms
of service of the various college-data sites that republish the same numbers.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re

import httpx

from . import usregions
from .config import DEFAULT_USER_AGENT

log = logging.getLogger("scrapbot.apis")

NCAA_MEMBER_LIST = "https://web3.ncaa.org/directory/api/directory/memberList"
SCORECARD_URL = "https://api.data.gov/ed/collegescorecard/v1/schools"

# The NAIA publishes no member API. Its own school finder is a third-party SPA
# with neither robots.txt nor sitemap, and Wikipedia's transcription is both
# second-hand and rate-limited to bots. The authoritative list is this PDF,
# published by the NAIA itself on a host whose robots.txt allows everything.
# The path carries the year it was posted, so it changes each season — override
# with --naia-pdf when it moves.
NAIA_PDF_URL = (
    "https://www.naia.org/wp-content/uploads/2026/07/2026-2027_NAIA_Institutions.pdf"
)

# The PDF abbreviates conferences; the origin database stores full names.
NAIA_CONFERENCES = {
    "AAC": "Appalachian Athletic Conference",
    "Am. Midwest": "American Midwest Conference",
    "CAC": "Continental Athletic Conference",
    "Cal Pac": "California Pacific Conference",
    "Cascade": "Cascade Collegiate Conference",
    "CCAC": "Chicagoland Collegiate Athletic Conference",
    "Chicagoland": "Chicagoland Collegiate Athletic Conference",
    "Crossroads": "Crossroads League",
    "Frontier": "Frontier Conference",
    "GPAC": "Great Plains Athletic Conference",
    "GSAC": "Golden State Athletic Conference",
    "HAAC": "Heart of America Athletic Conference",
    "HBCUAC": "HBCU Athletic Conference",
    "KCAC": "Kansas Collegiate Athletic Conference",
    "Mid-South": "Mid-South Conference",
    "Red River": "Red River Athletic Conference",
    "RSC": "River States Conference",
    "Sooner": "Sooner Athletic Conference",
    "SSAC": "Southern States Athletic Conference",
    "SUN": "The Sun Conference",
    "TSC": "The Sun Conference",
    "WHAC": "Wolverine-Hoosier Athletic Conference",
}

# api.data.gov's shared demo key: ~30 requests/hour. Fine for a few schools,
# useless for a few hundred — get a free key at https://api.data.gov/signup/
DEMO_KEY = "DEMO_KEY"

DIVISION_CODES = {
    "1": "DI", "2": "DII", "3": "DIII", "I": "DI", "II": "DII", "III": "DIII",
    # The NAIA has had no divisions since 2020 — the association *is* the tier.
    "NAIA": "NAIA",
    # The NJCAA tier is a per-sport designation, not a property of the college
    # — the same school plays DI in one sport and DII in another, which is why
    # 29 of them turned up in two of the source lists. All three collapse to a
    # single "NJCAA"; the article keys below still fetch all three lists.
    "NJCAA DI": "NJCAA",
    "NJCAA DII": "NJCAA",
    "NJCAA DIII": "NJCAA",
}

OWNERSHIP = {1: "Public", 2: "Private (not-for-profit)", 3: "Private (for-profit)"}

SCORECARD_FIELDS = [
    "id",
    "school.name",
    "school.city",
    "school.state",
    "school.ownership",
    "school.school_url",
    "latest.cost.tuition.in_state",
    "latest.cost.tuition.out_of_state",
    "latest.cost.attendance.academic_year",
    "latest.admissions.sat_scores.25th_percentile.math",
    "latest.admissions.sat_scores.75th_percentile.math",
    "latest.admissions.sat_scores.25th_percentile.critical_reading",
    "latest.admissions.sat_scores.75th_percentile.critical_reading",
    "latest.admissions.act_scores.25th_percentile.cumulative",
    "latest.admissions.act_scores.75th_percentile.cumulative",
]


def scorecard_key() -> str:
    return os.environ.get("SCRAPBOT_SCORECARD_KEY", DEMO_KEY)


class ApiClient:
    """Small async JSON client with the same retry manners as the fetcher."""

    def __init__(self, timeout: float = 30.0, max_retries: int = 2) -> None:
        self._client: httpx.AsyncClient | None = None
        self.timeout = timeout
        self.max_retries = max_retries
        self.stats = {"requests": 0, "errors": 0}

    async def __aenter__(self) -> "ApiClient":
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            # Wikimedia rejects clients whose User-Agent carries no contact
            # address, so reuse the project's — it has one.
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def get_bytes(self, url: str) -> bytes | None:
        assert self._client is not None, "use as an async context manager"
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            log.info("could not download %s: %s", url, exc)
            self.stats["errors"] += 1
            return None
        self.stats["requests"] += 1
        if resp.status_code >= 400:
            log.info("%s returned %s", url, resp.status_code)
            self.stats["errors"] += 1
            return None
        return resp.content

    async def get_json(self, url: str, params: dict | None = None):
        assert self._client is not None, "use as an async context manager"
        for attempt in range(self.max_retries + 1):
            try:
                self.stats["requests"] += 1
                resp = await self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                log.debug("%s failed (%s), retrying", url, exc)
                await asyncio.sleep(min(2**attempt, 8))
                continue

            if resp.status_code == 429:
                # Scorecard's demo key throttles hard; say so rather than
                # silently returning half a dataset.
                log.warning(
                    "rate limited by %s — set SCRAPBOT_SCORECARD_KEY to a free "
                    "api.data.gov key (the shared DEMO_KEY allows ~30/hour)",
                    httpx.URL(url).host,
                )
                self.stats["errors"] += 1
                return None
            if resp.status_code >= 500 and attempt < self.max_retries:
                await asyncio.sleep(min(2**attempt, 8))
                continue
            if resp.status_code >= 400:
                log.info("%s returned %s", url, resp.status_code)
                self.stats["errors"] += 1
                return None
            try:
                return resp.json()
            except ValueError:
                self.stats["errors"] += 1
                return None

        self.stats["errors"] += 1
        return None


# --- NCAA ----------------------------------------------------------------

async def ncaa_members(client: ApiClient, divisions: list[str]) -> list[dict]:
    """Member institutions for the given divisions (``["I", "II", "III"]``)."""
    out: list[dict] = []
    for division in divisions:
        payload = await client.get_json(
            NCAA_MEMBER_LIST, {"type": "12", "division": division}
        )
        if not payload:
            log.warning("NCAA directory returned nothing for division %s", division)
            continue
        for record in payload:
            if record.get("deactive") == "Y":
                continue
            out.append(record)
        log.info("NCAA division %s: %d member(s)", division, len(payload))
    return out


def ncaa_division(record: dict) -> str | None:
    roman = (record.get("divisionRoman") or "").strip()
    if roman in DIVISION_CODES:
        return DIVISION_CODES[roman]
    return DIVISION_CODES.get(str(record.get("division") or "").strip())


def ncaa_state(record: dict) -> str | None:
    address = record.get("memberOrgAddress") or {}
    return (address.get("state") or "").strip() or None


def ncaa_domain(record: dict) -> str | None:
    """The athletics host, which is what the ``coaches`` source scrapes."""
    raw = (record.get("athleticWebUrl") or "").strip().lower()
    if not raw:
        return None
    raw = re.sub(r"^https?://", "", raw).split("/")[0]
    return raw.removeprefix("www.") or None


# --- NAIA ----------------------------------------------------------------

async def naia_members(client: ApiClient, pdf_url: str | None = None) -> list[dict]:
    """NAIA member institutions: school, state, conference.

    Returned in the shape the NCAA helpers produce, so the schools source can
    treat both associations identically.
    """
    url = pdf_url or NAIA_PDF_URL
    raw = await client.get_bytes(url)
    if not raw:
        log.warning(
            "could not download the NAIA member list from %s — if the season "
            "rolled over, pass the new URL with --naia-pdf",
            url,
        )
        return []

    members = parse_naia_pdf(raw)
    log.info("NAIA: %d member(s) from %s", len(members), url)
    return members


# "Baker University – KS  HAAC". The separator is an en dash in almost every
# row and a plain hyphen in at least one, so accept both.
_NAIA_ROW = re.compile(
    r"^(?P<name>.+?)\s*[–-]\s*(?P<state>[A-Z]{2})\s+(?P<conference>\S.*?)\s*$"
)
_NAIA_TOTAL = re.compile(r"Total Schools\s*\((\d+)\)")


def parse_naia_pdf(raw: bytes) -> list[dict]:
    """Parse the member roster out of the NAIA's institutions PDF."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - declared in pyproject
        log.warning("pypdf is not installed; run: pip install -e .")
        return []

    import io

    try:
        reader = PdfReader(io.BytesIO(raw))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        log.warning("could not read the NAIA PDF: %s", exc)
        return []

    members: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line in (raw_line.strip() for raw_line in text.splitlines()):
        if not line or "NATIONAL ASSOCIATION" in line or line.startswith("Last Modified"):
            continue
        match = _NAIA_ROW.match(line)
        if not match:
            continue
        name = match.group("name").strip().rstrip("*").strip()
        # Several names repeat across states — there are Concordias in NE, MI
        # and OR, and two Bethels — so the state is part of the identity.
        identity = (name, match.group("state"))
        if not name or identity in seen:
            continue
        seen.add(identity)

        code = match.group("conference").strip()
        members.append(
            {
                "nameOfficial": _naia_name(name),
                "conferenceName": NAIA_CONFERENCES.get(code, code),
                "memberOrgAddress": {"state": match.group("state")},
                "divisionRoman": "NAIA",
                "association": "NAIA",
            }
        )

    stated = _NAIA_TOTAL.search(text)
    if stated and len(members) != int(stated.group(1)):
        # Worth knowing about: the PDF's own count is the ground truth, so a
        # mismatch means a row shape we don't handle yet.
        log.warning(
            "parsed %d NAIA schools but the PDF states %s — some rows were skipped",
            len(members),
            stated.group(1),
        )
    return members


def _naia_name(name: str) -> str:
    """``"British Columbia, University of"`` -> ``"University of British Columbia"``.

    The PDF inverts a handful of names for alphabetical sorting, and is
    inconsistent about keeping the trailing "of" ("Saint Francis, University").
    Undo the inversion so the name matches Scorecard and the origin database.
    """
    match = re.match(r"^(.*),\s*(University|College)(?:\s+of)?$", name, re.I)
    if match:
        return f"{match.group(2)} of {match.group(1)}"
    return name


# --- NJCAA (junior colleges) ---------------------------------------------

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"

# njcaa.org sits behind bot protection and returns 403 to any non-browser
# client, robots.txt notwithstanding, so its own directory is not available to
# us. These per-division articles are the workable public source; each states
# its own member count, which the parser checks itself against.
NJCAA_ARTICLES = {
    "NJCAA DI": "List of NJCAA Division I schools",
    "NJCAA DII": "List of NJCAA Division II schools",
    "NJCAA DIII": "List of NJCAA Division III schools",
}

# Division I links the city, II and III write it as plain text:
#   "*[[Bevill State Community College]] Bears in [[Sumiton, Alabama|Sumiton]]"
#   "*[[Reid State Community College]] Lions in Evergreen"
_NJCAA_ENTRY = re.compile(r"^\*\s*\[\[([^\]]+)\]\]\s*(.*)$")
_NJCAA_CITY = re.compile(
    r"\bin\s+(?:\[\[([^\]|]+)(?:\|([^\]]+))?\]\]|([^\[\]]+?))\s*$"
)
_NJCAA_HEADING = re.compile(r"^===\s*([^=]+?)\s*===$")
_NJCAA_TOTAL = re.compile(r"There are (\d+)")
_NJCAA_REFERENCE = re.compile(r"^\*\s*\[?http")
_NJCAA_PLAIN = re.compile(r"^\*\s*([^\[\]]+?)\s+in\s+[^\[\]]+$")


def _link_text(target: str) -> str:
    """``"University of Connecticut#Avery Point campus|UConn Avery Point"``
    -> the display half; a plain target keeps its text minus any anchor."""
    if "|" in target:
        return target.split("|", 1)[1].strip()
    return target.split("#", 1)[0].strip()


async def njcaa_members(client: ApiClient, divisions: list[str] | None = None) -> list[dict]:
    """NJCAA junior-college members, in the shape the NCAA helpers produce."""
    wanted = divisions or list(NJCAA_ARTICLES)
    out: list[dict] = []
    for index, division in enumerate(wanted):
        article = NJCAA_ARTICLES.get(division)
        if not article:
            continue
        if index:
            # Wikimedia throttles bursts; three articles is not worth rushing.
            await asyncio.sleep(2)
        payload = await client.get_json(
            WIKIPEDIA_API,
            {
                "action": "parse",
                "page": article,
                "prop": "wikitext",
                "format": "json",
                "formatversion": 2,
            },
        )
        wikitext = ((payload or {}).get("parse") or {}).get("wikitext")
        if not wikitext:
            log.warning("could not read the %s member list", division)
            continue
        members = parse_njcaa_wikitext(wikitext, division)
        log.info("%s: %d member(s)", division, len(members))
        out.extend(members)
    return out


def parse_njcaa_wikitext(wikitext: str, division: str) -> list[dict]:
    """Parse the state-by-state bullet list of member colleges."""
    members: list[dict] = []
    seen: set[tuple[str, str]] = set()
    unlinked: list[str] = []
    state: str | None = None

    for line in wikitext.splitlines():
        line = line.strip()
        heading = _NJCAA_HEADING.match(line)
        if heading:
            state = heading.group(1).strip()
            continue
        if not line.startswith("*") or state is None:
            continue
        # The External links section is bullets too; those are references.
        if _NJCAA_REFERENCE.match(line):
            continue

        entry = _NJCAA_ENTRY.match(line)
        if not entry:
            # A handful of members have no Wikipedia article, so the bullet is
            # "*Name Nickname in City" with nothing to delimit name from
            # nickname. Record the whole phrase rather than guess a split.
            plain = _NJCAA_PLAIN.match(line)
            if plain:
                unlinked.append(plain.group(1).strip())
            continue

        name = _link_text(entry.group(1))
        if not name:
            continue
        code = usregions.state_code(state)
        identity = (name, code or state)
        if identity in seen:
            continue
        seen.add(identity)

        city = None
        city_match = _NJCAA_CITY.search(entry.group(2))
        if city_match:
            raw = city_match.group(2) or city_match.group(1) or city_match.group(3) or ""
            city = raw.split(",")[0].strip() or None

        members.append(
            {
                "nameOfficial": name,
                "city": city,
                "memberOrgAddress": {"state": code or state},
                "divisionRoman": division,
                "association": "NJCAA",
            }
        )

    if unlinked:
        # Never drop rows silently — say which ones and why.
        log.info(
            "%s: %d member(s) have no linked article so their name could not be "
            "separated from their nickname, and were skipped: %s",
            division,
            len(unlinked),
            "; ".join(unlinked[:3]),
        )

    stated = _NJCAA_TOTAL.search(wikitext)
    if stated and len(members) != int(stated.group(1)):
        # The article's prose count is maintained by hand and drifts from the
        # list it introduces, so this is a flag to check, not a failure.
        log.info(
            "%s: parsed %d schools; the article's prose says %s",
            division,
            len(members),
            stated.group(1),
        )
    return members


# --- College Scorecard ---------------------------------------------------

async def scorecard_lookup(client: ApiClient, name: str, state: str | None) -> dict | None:
    """Best match for ``name`` in Scorecard, or None.

    Institution names rarely match across the two sources — the NCAA has
    "Fairleigh Dickinson University, Florham" where Scorecard has "Fairleigh
    Dickinson University-Florham Campus" — so this searches on the leading
    words and picks the closest result by similarity, within the state.
    """
    params = {
        "api_key": scorecard_key(),
        "fields": ",".join(SCORECARD_FIELDS),
        "per_page": 20,
        "school.name": _search_term(name),
    }
    if state:
        params["school.state"] = state

    payload = await client.get_json(SCORECARD_URL, params)
    results = (payload or {}).get("results") or []
    if not results:
        return None

    target = _normalize(name)
    best, best_score = None, 0.0
    for result in results:
        score = difflib.SequenceMatcher(
            None, target, _normalize(result.get("school.name") or "")
        ).ratio()
        if score > best_score:
            best, best_score = result, score
    # Below this the "match" is usually a different campus of the same system.
    return best if best_score >= 0.6 else None


def _search_term(name: str) -> str:
    """Scorecard's name search is a prefix match, so send the distinctive head
    of the name without punctuation or campus qualifiers."""
    head = re.split(r"[,(]", name)[0]
    head = re.sub(r"\b(?:the|university|college|of|at)\b", " ", head, flags=re.I)
    head = re.sub(r"[^\w\s'-]", " ", head)
    words = [w for w in head.split() if w]
    return " ".join(words[:3]) or name


def _normalize(name: str) -> str:
    name = name.lower()
    name = re.sub(r"\b(?:the|university|college|campus|of|at|and)\b", " ", name)
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


# --- shaping -------------------------------------------------------------

def _money(value) -> str | None:
    return f"${value:,.0f}" if isinstance(value, (int, float)) and value > 0 else None


def total_yearly_cost(result: dict | None) -> str | None:
    """``"$57,360"`` for private, ``"$19,106/$26,330"`` for public.

    Cost of attendance, not tuition alone. Scorecard publishes one
    academic-year figure (in-state basis); the out-of-state figure adds the
    tuition differential to it, which is how the two-number public form in the
    origin database is built.
    """
    if not result:
        return None
    attendance = result.get("latest.cost.attendance.academic_year")
    in_state = result.get("latest.cost.tuition.in_state")
    out_state = result.get("latest.cost.tuition.out_of_state")
    if not isinstance(attendance, (int, float)) or attendance <= 0:
        return None

    public = result.get("school.ownership") == 1
    if public and isinstance(in_state, (int, float)) and isinstance(out_state, (int, float)):
        differential = max(0, out_state - in_state)
        if differential:
            return f"{_money(attendance)}/{_money(attendance + differential)}"
    return _money(attendance)


def _range(low, high) -> str | None:
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        return f"{int(low)}-{int(high)}"
    return None


def academic_data(result: dict | None) -> dict[str, str]:
    """The ``academicData`` block. ``averageGPA`` is absent by design — see the
    README; no free authoritative source publishes it."""
    if not result:
        return {}
    data = {
        "SATMath": _range(
            result.get("latest.admissions.sat_scores.25th_percentile.math"),
            result.get("latest.admissions.sat_scores.75th_percentile.math"),
        ),
        "ACTComposite": _range(
            result.get("latest.admissions.act_scores.25th_percentile.cumulative"),
            result.get("latest.admissions.act_scores.75th_percentile.cumulative"),
        ),
        "SATReady": _range(
            result.get("latest.admissions.sat_scores.25th_percentile.critical_reading"),
            result.get("latest.admissions.sat_scores.75th_percentile.critical_reading"),
        ),
    }
    return {k: v for k, v in data.items() if v}


def private_public(result: dict | None, ncaa_record: dict | None = None) -> str | None:
    ownership = (result or {}).get("school.ownership")
    if ownership in OWNERSHIP:
        return OWNERSHIP[ownership]
    flag = (ncaa_record or {}).get("privateFlag")
    if flag == "Y":
        return "Private (not-for-profit)"
    if flag == "N":
        return "Public"
    return None
