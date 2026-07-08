"""First-run onboarding.

Shared by the TUI (`python -m tui`) and CLI (`st.py`). On first launch there's
no agent token, so instead of a hard exit we walk the user through getting one:
paste an existing agent token, register a fresh agent from an account token, or
head to the website first. Whatever path they take, we validate the token
against the live API and persist it (plus their callsign + HQ) to ``.env``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import config
from api import ApiError, Client

ENV_PATH = Path(__file__).resolve().parent / ".env"

BANNER = r"""
   ___  ___  ___  ___ ___ _____ _   _ ___
  / __||  \/  | _ \/ __|__ |_   _| | | |_ _|   spacetui
  \__ \| |\/| |  _/ (__ / /  | | | |_| || |    SpaceTraders v2 · Nebula HUD
  |___/|_|  |_|_|  \___/_/   |_|  \___/|___|

  Welcome, commander. Let's get you flying.
"""

WEBSITE = "https://spacetraders.io"


def is_configured() -> bool:
    return bool(config.AGENT_TOKEN)


def write_env(updates: dict[str, str]) -> None:
    """Merge ``updates`` into ``.env``, preserving comments and other keys."""
    existing = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    out: list[str] = []
    seen: set[str] = set()
    for line in existing:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key = s.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out).rstrip("\n") + "\n")


def _apply_runtime(token: str, symbol: str = "", hq: str = "", account: str = "") -> None:
    """Update the live process so the app sees the new credentials immediately."""
    os.environ["ST_AGENT_TOKEN"] = token
    config.AGENT_TOKEN = token
    if symbol:
        os.environ["ST_AGENT_SYMBOL"] = symbol
        config.AGENT_SYMBOL = symbol
    if hq:
        os.environ["ST_HQ"] = hq
        config.HQ = hq
    if account:
        os.environ["ST_ACCOUNT_TOKEN"] = account
        config.ACCOUNT_TOKEN = account


def _validate_and_save(
    token: str, output, *, account: str = "", client_factory=Client
) -> str | None:
    """Confirm the token works, auto-fill callsign/HQ from the agent, persist."""
    output("  checking token…")
    try:
        agent = client_factory(token=token).my_agent()
    except ApiError as e:
        output(f"  ✗ the API rejected that token: {e.message}")
        return None
    except Exception as e:  # network / TLS / etc.
        output(f"  ✗ couldn't reach the API: {e}")
        return None
    symbol = agent.get("symbol", "")
    hq = agent.get("headquarters", "")
    updates = {"ST_AGENT_TOKEN": token}
    if symbol:
        updates["ST_AGENT_SYMBOL"] = symbol
    if hq:
        updates["ST_HQ"] = hq
    if account:
        updates["ST_ACCOUNT_TOKEN"] = account
    write_env(updates)
    _apply_runtime(token, symbol, hq, account)
    output(
        f"  ✓ signed in as {symbol}  ·  HQ {hq}  ·  {agent.get('credits', 0):,}c"
        f"  ·  {agent.get('shipCount', 0)} ship(s)"
    )
    output(f"  saved credentials to {ENV_PATH}")
    return token


def save_agent_token(token: str, *, account: str = "", client_factory=Client) -> dict:
    """Validate an agent token, persist it (+ callsign/HQ) to .env, apply at
    runtime, and return the agent dict. Raises ApiError if the token is rejected.
    Non-interactive — used by the web setup flow."""
    agent = client_factory(token=token).my_agent()
    updates = {"ST_AGENT_TOKEN": token}
    if agent.get("symbol"):
        updates["ST_AGENT_SYMBOL"] = agent["symbol"]
    if agent.get("headquarters"):
        updates["ST_HQ"] = agent["headquarters"]
    if account:
        updates["ST_ACCOUNT_TOKEN"] = account
    write_env(updates)
    _apply_runtime(token, agent.get("symbol", ""), agent.get("headquarters", ""), account)
    return agent


def register_agent(account_token: str, callsign: str, faction: str = "COSMIC",
                   *, client_factory=Client) -> dict:
    """Register a new agent and persist its token. Returns the agent dict."""
    data = client_factory.register(callsign.upper(), (faction or "COSMIC").upper(),
                                   account_token=account_token)
    return save_agent_token(data["token"], account=account_token, client_factory=client_factory)


def _valid_callsign(s: str) -> bool:
    return 3 <= len(s) <= 14 and s.replace("_", "").replace("-", "").isalnum()


def _register_flow(input_fn, output, *, client_factory=Client) -> str | None:
    account = config.ACCOUNT_TOKEN or input_fn("  Paste your ACCOUNT token: ").strip()
    if not account:
        output("  (no account token — get one from your account page at the website)")
        return None
    for _ in range(3):
        callsign = input_fn("  Choose a callsign (3-14 letters/numbers): ").strip().upper()
        if _valid_callsign(callsign):
            break
        output("  callsign must be 3-14 alphanumeric characters.")
    else:
        return None
    faction = (input_fn("  Faction [COSMIC]: ").strip() or "COSMIC").upper()
    output(f"  registering {callsign} with {faction}…")
    try:
        data = client_factory.register(callsign, faction, account_token=account)
    except ApiError as e:
        output(f"  ✗ registration failed: {e.message}")
        return None
    except Exception as e:
        output(f"  ✗ couldn't reach the API: {e}")
        return None
    return _validate_and_save(
        data["token"], output, account=account, client_factory=client_factory
    )


def run_wizard(
    *, input_fn=input, output=print, client_factory=Client
) -> str | None:
    """Interactive first-run wizard. Returns an agent token, or None if aborted."""
    output(BANNER)
    if is_configured():
        output(f"  Already configured as {config.AGENT_SYMBOL or '?'}.")
        if input_fn("  Reconfigure anyway? [y/N]: ").strip().lower() not in ("y", "yes"):
            return config.AGENT_TOKEN
    output("  How would you like to get started?\n")
    output("    [1] I have an AGENT token — paste it")
    output("    [2] I have an ACCOUNT token — register a new agent now")
    output(f"    [3] I have neither — take me to {WEBSITE}")
    output("    [q] Quit\n")
    choice = input_fn("  > ").strip().lower()

    if choice == "1":
        token = input_fn("  Paste your AGENT token: ").strip()
        if not token:
            output("  (nothing pasted)")
            return None
        return _validate_and_save(token, output, client_factory=client_factory)
    if choice == "2":
        return _register_flow(input_fn, output, client_factory=client_factory)
    if choice == "3":
        output(
            f"\n  Open {WEBSITE} and create a free account, copy your ACCOUNT token,\n"
            "  then run this again and choose [2] to register your first agent.\n"
        )
        return None
    output("  Setup cancelled.")
    return None


def ensure_onboarded(interactive: bool | None = None) -> str:
    """Return a usable agent token, running the wizard on first use.

    Raises SystemExit with guidance when no token is available and we can't
    prompt (e.g. non-interactive stdin).
    """
    if is_configured():
        return config.AGENT_TOKEN
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        raise SystemExit(
            "No ST_AGENT_TOKEN found. Run `python st.py setup` to get started, "
            f"or add a token to {ENV_PATH}."
        )
    token = run_wizard()
    if not token:
        raise SystemExit("Setup incomplete — no agent token configured.")
    return token
