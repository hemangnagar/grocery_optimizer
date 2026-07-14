"""Project paths and configuration.

All paths use pathlib (Windows-native). Secrets are read ONLY from the
per-project .env file, never from global Windows environment variables — this
machine also runs trading bots with API keys, and a global ANTHROPIC_API_KEY
would make Claude Code bill API rates instead of the subscription.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# src/grocery_optimizer/config.py -> project root is three parents up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = PROJECT_ROOT / "data"
BRONZE_DIR = DATA_DIR / "bronze"
DB_DIR = DATA_DIR / "duckdb"
DB_PATH = DB_DIR / "grocery.duckdb"

SQL_DIR = Path(__file__).resolve().parent / "sql"

# Load .env from the project root. override=False so an already-set process env
# still wins, but we never fall back to reading global user/system vars for
# secrets ourselves (see get_secret).
_ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(_ENV_PATH, override=False)


# User's home location. The store comparison set is constrained to stores within
# SEARCH_RADIUS_MILES of HOME_ZIP — comparing prices across the whole metro is
# meaningless if the user won't drive there. Personal, so overridable via .env.
HOME_ZIP = os.environ.get("HOME_ZIP", "22180")  # Vienna, VA
SEARCH_RADIUS_MILES = int(os.environ.get("SEARCH_RADIUS_MILES", "5"))

# Whole Foods store id for the home area (Vienna, VA). WFM's site resolves this
# from geolocation; we pin it in config for the deterministic pull.
WFM_STORE_ID = os.environ.get("WFM_STORE_ID", "10065")


def ensure_dirs() -> None:
    """Create the data directories if they do not exist."""
    BRONZE_DIR.mkdir(parents=True, exist_ok=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)


def _get(name: str) -> str | None:
    """Read a config value that originated from the project .env."""
    return os.environ.get(name)


def get_anthropic_api_key() -> str | None:
    return _get("ANTHROPIC_API_KEY")


def get_kroger_credentials() -> tuple[str | None, str | None]:
    return _get("KROGER_CLIENT_ID"), _get("KROGER_CLIENT_SECRET")
