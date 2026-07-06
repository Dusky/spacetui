from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    _env_path = Path(__file__).resolve().parent / ".env"
    if _env_path.exists():
        for _line in _env_path.read_text().splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())


BASE_URL = os.environ.get("ST_BASE_URL", "https://api.spacetraders.io/v2")
AGENT_SYMBOL = os.environ.get("ST_AGENT_SYMBOL", "")
AGENT_TOKEN = os.environ.get("ST_AGENT_TOKEN", "")
ACCOUNT_TOKEN = os.environ.get("ST_ACCOUNT_TOKEN", "")
HQ = os.environ.get("ST_HQ", "")


def require_agent_token() -> str:
    if not AGENT_TOKEN:
        raise SystemExit(
            "No ST_AGENT_TOKEN found. Put your agent token in .env "
            "(register one with `python st.py register`)."
        )
    return AGENT_TOKEN
