"""Runtime settings for scrapbot.

Everything here has a sane default, can be overridden by an environment
variable (prefix ``SCRAPBOT_``), and can be overridden again by a CLI flag.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_USER_AGENT = (
    "scrapbot/0.1 (+NextUp Recruitment lead research; "
    "contact: hello@nextuprecruitment.example)"
)


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

    # --- rendering --------------------------------------------------------
    render: str = field(default_factory=lambda: _env_str("RENDER", "auto"))
    """One of ``never``, ``auto`` (Playwright only when static HTML looks empty), ``always``."""
    headless: bool = field(default_factory=lambda: _env_bool("HEADLESS", True))

    # --- output -----------------------------------------------------------
    data_dir: Path = field(
        default_factory=lambda: Path(_env_str("DATA_DIR", str(PROJECT_ROOT / "data")))
    )

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

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
