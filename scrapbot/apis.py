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

log = logging.getLogger("scrapbot.apis")

NCAA_MEMBER_LIST = "https://web3.ncaa.org/directory/api/directory/memberList"
SCORECARD_URL = "https://api.data.gov/ed/collegescorecard/v1/schools"

# api.data.gov's shared demo key: ~30 requests/hour. Fine for a few schools,
# useless for a few hundred — get a free key at https://api.data.gov/signup/
DEMO_KEY = "DEMO_KEY"

DIVISION_CODES = {"1": "DI", "2": "DII", "3": "DIII", "I": "DI", "II": "DII", "III": "DIII"}

OWNERSHIP = {1: "Public", 2: "Private (not-for-profit)", 3: "Private (for-profit)"}

SCORECARD_FIELDS = [
    "id",
    "school.name",
    "school.city",
    "school.state",
    "school.ownership",
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
            headers={"User-Agent": "scrapbot/0.1 (+NextUp Recruitment)", "Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()

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
