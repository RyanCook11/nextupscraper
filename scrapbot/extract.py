"""Heuristics that turn a company web page into structured lead fields."""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)

# Deliberately conservative. Free text is only mined for numbers that are
# *grouped* like a phone number ("(02) 9876 5432", "+1 555-123-4567") or are an
# unambiguous bare run ("+61298765432", "0412345678"). Without the grouping
# requirement, any long digit string on the page reads as a phone number.
PHONE_RE = re.compile(
    r"""
    (?<![\d+/])
    (?:
        (?:\+\d{1,3}[\s.\-]?)?          # optional country code
        (?:\(\d{1,5}\)|\d{1,5})         # area code, bracketed or not
        (?:[\s.\-]\d{2,4}){2,4}         # further groups — separator required
      |
        \+\d{8,14}                      # bare international
      |
        0\d{8,10}                       # bare national trunk form
    )
    (?![\d])
    """,
    re.X,
)

# Free-text numbers only count as a phone if a phone-ish word introduces them.
# Without this, any grouped digit sequence on the page qualifies — python.org
# offered up "8 13 21 34 55" from a Fibonacci code sample.
PHONE_CONTEXT_RE = re.compile(
    r"\b(?:phone|telephone|tel|mobile|mob|cell|fax|freecall|toll[\s\-]?free|"
    r"call\s+us(?:\s+on)?|ph)\b[\s:.\-]{0,3}",
    re.I,
)

SOCIAL_HOSTS = {
    "linkedin": ("linkedin.com/company", "linkedin.com/in"),
    "twitter": ("twitter.com", "x.com"),
    "facebook": ("facebook.com",),
    "instagram": ("instagram.com",),
    "youtube": ("youtube.com", "youtu.be"),
    "github": ("github.com",),
}

# Emails that are almost never a real company contact.
EMAIL_NOISE = re.compile(
    r"(?:example\.(?:com|org)|sentry\.io|wixpress|godaddy|@2x|\.(?:png|jpe?g|gif|webp|svg|css|js)$)",
    re.I,
)

INDUSTRY_KEYWORDS = {
    "construction": ("construction", "builder", "civil works", "scaffolding"),
    "healthcare": ("healthcare", "clinic", "nursing", "aged care", "medical centre"),
    "technology": ("software", "saas", "it services", "cloud", "developer", "cyber"),
    "engineering": ("engineering", "mechanical", "electrical contractor", "fabrication"),
    "logistics": ("logistics", "freight", "warehouse", "supply chain", "transport"),
    "hospitality": ("hospitality", "restaurant", "hotel", "catering", "cafe"),
    "finance": ("accounting", "financial services", "bookkeeping", "insurance", "mortgage"),
    "education": ("school", "training", "rto", "education", "academy"),
    "manufacturing": ("manufacturing", "factory", "production line", "assembly"),
    "mining": ("mining", "drilling", "fifo", "resources sector"),
    "retail": ("retail", "store", "ecommerce", "shopfront"),
    "recruitment": ("recruitment", "staffing", "labour hire", "talent acquisition"),
}

# Link text / hrefs worth following on a company site, best first.
PAGE_HINTS = [
    ("contact", 100),
    ("contact-us", 100),
    ("about", 80),
    ("about-us", 80),
    ("careers", 70),
    ("jobs", 70),
    ("work-with-us", 65),
    ("team", 50),
    ("people", 45),
    ("locations", 40),
    ("our-company", 35),
]

OPEN_ROLE_MARKERS = (
    "apply now",
    "current vacancies",
    "current openings",
    "open positions",
    "we're hiring",
    "we are hiring",
    "join our team",
    "view job",
)


def parse(html: str) -> HTMLParser:
    return HTMLParser(html)


# Text whose *immediate* parent is one of these is markup, not page copy.
# Checked at the direct parent only, deliberately: a page with an unbalanced
# <script> or <noscript> gets its real DOM mis-nested underneath that tag by
# the parser, and an ancestor check would then discard the whole document.
# (Duke's 2.2 MB staff directory does exactly this — it collapsed to 12
# characters under the old decompose-and-read approach.)
NON_VISIBLE_TAGS = {"script", "style", "noscript", "template"}

# These are excluded even when the text sits deeper, because code samples wrap
# their content in spans. Their digit runs masquerade as phone numbers.
NON_VISIBLE_SUBTREES = {"pre", "code", "samp", "kbd", "svg"}


def visible_text(tree: HTMLParser) -> str:
    """Page copy with markup and code samples removed.

    Non-destructive: it selects text nodes rather than deleting subtrees, so a
    malformed page yields less text instead of none.
    """
    body = tree.body
    if body is None:
        return ""
    chunks: list[str] = []
    for node in body.traverse(include_text=True):
        if node.tag != "-text":
            continue
        parent = node.parent
        if parent is None or parent.tag in NON_VISIBLE_TAGS:
            continue
        if _within(parent, NON_VISIBLE_SUBTREES):
            continue
        piece = node.text()
        if piece and piece.strip():
            chunks.append(piece)
    return re.sub(r"\s+", " ", " ".join(chunks)).strip()


def _within(node, tags: set[str], max_depth: int = 40) -> bool:
    current = node
    for _ in range(max_depth):
        if current is None:
            return False
        if current.tag in tags:
            return True
        current = current.parent
    return False


def company_name(tree: HTMLParser, domain: str) -> str | None:
    for selector, attr in (
        ('meta[property="og:site_name"]', "content"),
        ('meta[name="application-name"]', "content"),
    ):
        node = tree.css_first(selector)
        if node and node.attributes.get(attr):
            name = _clean(node.attributes[attr])
            if name:
                return name

    for org in _json_ld(tree):
        types = org.get("@type")
        types = [types] if isinstance(types, str) else (types or [])
        if any(str(t).lower() in {"organization", "localbusiness", "corporation"} for t in types):
            name = _clean(org.get("name"))
            if name:
                return name

    title = tree.css_first("title")
    if title:
        raw = _clean(title.text())
        if raw:
            # "Acme Plumbing | Home" -> "Acme Plumbing"
            parts = re.split(r"\s*[|–—\-·:]\s*", raw)
            parts = [p for p in parts if p and not _is_generic(p)]
            if parts:
                return max(parts[:2], key=len) if len(parts) > 1 else parts[0]
            return raw

    root = domain.removeprefix("www.").split(".")[0]
    return root.replace("-", " ").title() or None


def description(tree: HTMLParser) -> str | None:
    for selector in ('meta[name="description"]', 'meta[property="og:description"]'):
        node = tree.css_first(selector)
        if node and node.attributes.get("content"):
            text = _clean(node.attributes["content"])
            if text and len(text) > 20:
                return text[:500]
    for org in _json_ld(tree):
        text = _clean(org.get("description"))
        if text and len(text) > 20:
            return text[:500]
    return None


def emails(html: str, tree: HTMLParser, domain: str) -> list[str]:
    found: dict[str, None] = {}

    for node in tree.css('a[href^="mailto:"]'):
        href = node.attributes.get("href") or ""
        addr = href[len("mailto:") :].split("?")[0].strip()
        if _email_ok(addr):
            found.setdefault(addr.lower(), None)

    for match in EMAIL_RE.finditer(html):
        addr = match.group(0)
        if _email_ok(addr):
            found.setdefault(addr.lower(), None)

    root = domain.removeprefix("www.").lower()

    def rank(addr: str) -> tuple[int, int]:
        local, _, host = addr.partition("@")
        on_domain = 0 if host.endswith(root) else 1
        generic = 0 if local in {"info", "contact", "hello", "enquiries", "admin", "hr", "careers", "jobs"} else 1
        return (on_domain, generic)

    return sorted(found, key=rank)[:10]


def phones(tree: HTMLParser, text: str) -> list[str]:
    found: dict[str, None] = {}

    # A tel: link is an explicit declaration — trust it without needing context.
    for node in tree.css('a[href^="tel:"]'):
        href = (node.attributes.get("href") or "")[len("tel:") :]
        cleaned = clean_phone(href.split("?")[0])
        if cleaned:
            found.setdefault(cleaned, None)

    for context in PHONE_CONTEXT_RE.finditer(text):
        window = text[context.end() : context.end() + 40]
        match = PHONE_RE.search(window)
        # Must sit right after the label, not merely somewhere nearby.
        if match is None or match.start() > 6:
            continue
        cleaned = clean_phone(match.group(0))
        if cleaned:
            found.setdefault(cleaned, None)

    return list(found)[:5]


def socials(tree: HTMLParser, base_url: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.css("a[href]"):
        href = node.attributes.get("href") or ""
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base_url, href)
        lowered = absolute.lower()
        for platform, needles in SOCIAL_HOSTS.items():
            if platform in out:
                continue
            if any(needle in lowered for needle in needles):
                out[platform] = absolute.split("?")[0]
    return out


def location(tree: HTMLParser, text: str) -> str | None:
    for org in _json_ld(tree):
        addr = org.get("address")
        if isinstance(addr, dict):
            parts = [
                addr.get("streetAddress"),
                addr.get("addressLocality"),
                addr.get("addressRegion"),
                addr.get("postalCode"),
                addr.get("addressCountry") if isinstance(addr.get("addressCountry"), str) else None,
            ]
            joined = ", ".join(_clean(p) or "" for p in parts if _clean(p))
            if joined:
                return joined[:200]
        elif isinstance(addr, str) and _clean(addr):
            return _clean(addr)[:200]

    node = tree.css_first('[itemprop="address"], address')
    if node:
        candidate = re.sub(r"\s+", " ", node.text(separator=" ") or "").strip()
        if 10 < len(candidate) < 200:
            return candidate

    # Fallback: "<street> <SUBURB> <STATE> <postcode>" as seen on AU sites.
    match = re.search(
        r"\d{1,5}[\w\s./'\-]{3,60},?\s+[A-Za-z\s]{3,30},?\s+"
        r"(?:NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\s+\d{4}",
        text,
    )
    return match.group(0).strip() if match else None


def industry_hints(text: str, description_text: str | None) -> list[str]:
    haystack = f"{description_text or ''} {text[:6000]}".lower()
    hits = [
        industry
        for industry, needles in INDUSTRY_KEYWORDS.items()
        if any(needle in haystack for needle in needles)
    ]
    return hits[:5]


def clean_phone(raw: str) -> str | None:
    """Normalize a phone-ish string, or reject it as digit soup.

    Public because per-person sources read phones out of a labelled table cell
    rather than mining free text, so they need the validator without the
    surrounding context heuristics.
    """
    candidate = re.sub(r"[^\d+]", " ", raw).strip()
    candidate = re.sub(r"\s+", " ", candidate)
    digits = re.sub(r"\D", "", candidate)
    if not 8 <= len(digits) <= 15:
        return None
    # Version strings, ids and padded numbers reach here as digit soup.
    if len(set(digits)) <= 2:
        return None
    if re.search(r"\d{12,}", candidate):
        # 12+ digits with no break is an id, not a dialable number, unless the
        # source explicitly marked it international.
        if not candidate.startswith("+"):
            return None
    return format_phone(candidate)


def format_phone(candidate: str) -> str:
    """Put a North American number into one shape: ``+1 231 555 0199``.

    Directories publish the same number a dozen ways — "443 454 5206" from a
    table cell, "+14324663753" from the ``tel:`` link beside it, "(805)
    922-6966 ext. 3227" in running text. Stored as-is they are different
    strings, so the same person's number fails to de-duplicate and a caller has
    to reformat every row by hand.

    Only genuine NANP numbers are touched, judged on the rules the plan
    actually has: a 10-digit number, or 11 beginning with the country code 1,
    whose area code and exchange both start 2-9. Anything else — a nine-digit
    fragment, an international number, digit soup that survived the checks
    above — is handed back untouched rather than forced into a shape it does
    not fit.

    Trailing digits past the number are an extension, not part of it: Allan
    Hancock lists six staff on 805 922 6966 with a different extension each.
    They are kept as ``x3227`` so the switchboard number still de-duplicates.
    """
    digits = re.sub(r"\D", "", candidate)
    # A leading "+" on anything but country code 1 is the source stating its
    # own country, and it is not ours to reinterpret. Without this, the
    # Australian "+61 2 9876 5432" reads as eleven grouped digits and comes out
    # as "+1 612 987 6543 x2" — a plausible-looking Michigan number that does
    # not exist. scrapbot's other sources are pointed at .com.au sites, so this
    # is not hypothetical.
    if candidate.lstrip().startswith("+") and not digits.startswith("1"):
        return candidate
    # Whether the source itself broke the number up. This is what separates
    # "401 232 6828 1" — a number with a one-digit extension — from
    # "+81397466225", a Japanese number that happens to be the same length.
    # Only the first was written as separate groups, so only the first may have
    # its tail read as an extension.
    grouped = bool(re.search(r"\d\D+\d", candidate))

    if len(digits) == 10:
        base, extension = digits, ""
    elif len(digits) == 11 and digits.startswith("1"):
        base, extension = digits[1:], ""
    elif len(digits) > 11 and digits.startswith("1"):
        base, extension = digits[1:11], digits[11:]
    elif len(digits) > 10 and grouped:
        base, extension = digits[:10], digits[10:]
    else:
        return candidate
    if len(extension) > 6:  # too long to be an extension — leave it alone
        return candidate

    if base[0] in "01" or base[3] in "01":
        return candidate  # not a dialable NANP number

    formatted = f"+1 {base[:3]} {base[3:6]} {base[6:]}"
    return f"{formatted} x{extension}" if extension else formatted


def has_open_roles(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in OPEN_ROLE_MARKERS)


def internal_links(tree: HTMLParser, base_url: str, domain: str) -> list[tuple[int, str]]:
    """Return ``(score, url)`` for same-site links worth crawling, best first."""
    root = domain.removeprefix("www.").lower()
    scored: dict[str, int] = {}

    for node in tree.css("a[href]"):
        href = node.attributes.get("href") or ""
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base_url, href).split("#")[0].rstrip("/")
        parts = urlparse(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        host = parts.netloc.removeprefix("www.").lower()
        if host != root and not host.endswith("." + root):
            continue
        if re.search(r"\.(?:pdf|jpe?g|png|gif|svg|zip|docx?|xlsx?|mp4)$", parts.path, re.I):
            continue

        haystack = f"{parts.path.lower()} {(node.text() or '').strip().lower()}"
        score = 0
        for hint, weight in PAGE_HINTS:
            if hint in haystack:
                score = max(score, weight)
        if score:
            scored[absolute] = max(scored.get(absolute, 0), score)

    return sorted(((s, u) for u, s in scored.items()), key=lambda pair: -pair[0])


# --- internals -----------------------------------------------------------

def _json_ld(tree: HTMLParser) -> list[dict]:
    out: list[dict] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        out.extend(_flatten_ld(data))
    return out


def _flatten_ld(data: object) -> list[dict]:
    if isinstance(data, dict):
        items = [data]
        for key in ("@graph", "itemListElement"):
            nested = data.get(key)
            if nested:
                items.extend(_flatten_ld(nested))
        return items
    if isinstance(data, list):
        out: list[dict] = []
        for item in data:
            out.extend(_flatten_ld(item))
        return out
    return []


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def _is_generic(part: str) -> bool:
    return part.strip().lower() in {
        "home", "homepage", "welcome", "index", "contact", "contact us", "about", "about us",
    }


def _email_ok(addr: str) -> bool:
    if not addr or not EMAIL_RE.fullmatch(addr):
        return False
    return not EMAIL_NOISE.search(addr)


