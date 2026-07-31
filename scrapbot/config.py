"""Runtime settings for scrapbot.

Everything here has a sane default, can be overridden by ``.env`` in the
project root, overridden again by a real environment variable (prefix
``SCRAPBOT_``), and overridden once more by a CLI flag.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("scrapbot.config")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_USER_AGENT = (
    "scrapbot/0.1 (+NextUp Recruitment lead research; "
    "contact: hello@nextuprecruitment.example)"
)


def _load_dotenv() -> None:
    """Read ``.env`` from the project root, if it is there.

    Done at import so every entry point gets it — the CLI, the web app and
    the tests all reach settings through this module. A real environment
    variable always wins, which is what ``override=False`` buys us: an
    ``$env:SCRAPBOT_CONCURRENCY`` set for one run still beats the file.
    """
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        # The file exists and the user clearly means it to be read, so a
        # missing dependency is worth saying out loud rather than silently
        # falling back to defaults.
        log.warning(
            "%s exists but python-dotenv is not installed, so it is being "
            "ignored — run: pip install -e .",
            env_file,
        )
        return
    load_dotenv(env_file, override=False)


_load_dotenv()


def _env_str(name: str, default: str) -> str:
    return os.environ.get(f"SCRAPBOT_{name}", default)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(f"SCRAPBOT_{name}")
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(f"SCRAPBOT_{name}")
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- politeness -------------------------------------------------------
    user_agent: str = field(default_factory=lambda: _env_str("USER_AGENT", DEFAULT_USER_AGENT))
    respect_robots: bool = field(default_factory=lambda: _env_bool("RESPECT_ROBOTS", True))
    delay: float = field(default_factory=lambda: _env_float("DELAY", 1.5))
    """Minimum seconds between two requests to the *same* host."""
    concurrency: int = field(default_factory=lambda: _env_int("CONCURRENCY", 4))
    """How many different sites are worked on at once."""
    timeout: float = field(default_factory=lambda: _env_float("TIMEOUT", 20.0))
    max_retries: int = field(default_factory=lambda: _env_int("MAX_RETRIES", 2))

    # --- crawl shape ------------------------------------------------------
    max_pages_per_site: int = field(default_factory=lambda: _env_int("MAX_PAGES_PER_SITE", 6))
    max_bytes_per_page: int = field(default_factory=lambda: _env_int("MAX_BYTES_PER_PAGE", 3_000_000))
    max_sport_pages: int = field(default_factory=lambda: _env_int("MAX_SPORT_PAGES", 40))
    """Budget for the one-page-per-sport layout, separate from the site budget.

    PrestoSports publishes no combined staff directory at all: the coaches live
    at ``/sports/<sport>/coaches/index``, one page per team, and a college
    fields 15-25 teams. Charged against ``max_pages_per_site`` (6) that layout
    can only ever be read a quarter of the way through, so it gets its own
    allowance. These are cheap, targeted fetches — one page per team, no
    crawling — and the per-host delay still paces them.
    """

    # --- rendering --------------------------------------------------------
    render: str = field(default_factory=lambda: _env_str("RENDER", "auto"))
    """One of ``never``, ``auto`` (Playwright only when static HTML looks empty), ``always``."""
    headless: bool = field(default_factory=lambda: _env_bool("HEADLESS", True))

    # --- human-mimicry timing ---------------------------------------------
    jitter_min: float = field(default_factory=lambda: _env_float("JITTER_MIN", 0.2))
    jitter_max: float = field(default_factory=lambda: _env_float("JITTER_MAX", 0.8))

    # --- caching ----------------------------------------------------------
    cache_ttl: float = field(default_factory=lambda: _env_float("CACHE_TTL", 604_800.0))
    """How long a fetched page stays reusable, in seconds; 0 disables the cache.

    The default is a week. Coaching directories turn over on the scale of a
    season, and not re-fetching a page we already have is the single largest
    reduction in request volume available — which is what actually decides
    whether a host starts challenging us.
    """

    # --- adaptive backoff -------------------------------------------------
    backoff_factor: float = field(default_factory=lambda: _env_float("BACKOFF_FACTOR", 2.0))
    """How hard to stretch a host's delay each time it pushes back."""
    max_backoff: float = field(default_factory=lambda: _env_float("MAX_BACKOFF", 16.0))
    """Ceiling on that stretch, as a multiple of the base delay."""

    # --- durability -------------------------------------------------------
    checkpoint_secs: float = field(default_factory=lambda: _env_float("CHECKPOINT_SECS", 120.0))
    """Flush the merged store this often mid-run; 0 disables.

    A sweep of a couple of thousand schools runs for hours, and the store used
    to be written only after the last site. Losing an interrupted run meant
    losing every contact it had already found.
    """

    # --- output -----------------------------------------------------------
    data_dir: Path = field(
        default_factory=lambda: Path(_env_str("DATA_DIR", str(PROJECT_ROOT / "data")))
    )

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def cache_dir(self) -> Path:
        """Where fetched responses are kept between runs."""
        return self.data_dir / "cache"

    @property
    def store_path(self) -> Path:
        """Merged, de-duplicated store of every lead ever scraped."""
        return self.data_dir / "leads.json"

    @property
    def store_csv_path(self) -> Path:
        return self.data_dir / "leads.csv"

    @property
    def contacts_path(self) -> Path:
        """Merged, de-duplicated store of people (one record per person)."""
        return self.data_dir / "contacts.json"

    @property
    def contacts_csv_path(self) -> Path:
        return self.data_dir / "contacts.csv"

    @property
    def schools_path(self) -> Path:
        """Merged store of institutions, in the origin database's shape."""
        return self.data_dir / "schools.json"

    @property
    def schools_csv_path(self) -> Path:
        return self.data_dir / "schools.csv"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        if self.cache_ttl > 0:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
