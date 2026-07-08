# spacetui

A terminal UI + CLI for playing [SpaceTraders](https://spacetraders.io/) v2.

Automate a fleet, manage contracts, mine, trade, and run autonomous bots — from a
custom-themed Textual TUI ("Nebula HUD") or a plain argparse CLI.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### First run

Just launch the app (or the CLI) — a first-run wizard walks you through getting
an agent token and writes `.env` for you:

```bash
.venv/bin/python -m tui        # onboarding runs automatically on first launch
.venv/bin/python st.py setup   # or run the wizard directly from the CLI
```

The wizard lets you paste an existing agent token, or register a brand-new agent
from your [spacetraders.io](https://spacetraders.io) account token. It validates
the token against the live API and fills in your callsign and HQ automatically.

Prefer to do it by hand? Create a `.env` (gitignored) yourself:

```
ST_AGENT_SYMBOL=MDOE
ST_HQ=X1-N85-A1
ST_ACCOUNT_TOKEN=<account token from spacetraders.io>
ST_AGENT_TOKEN=<agent token, obtained after /register>
```

## Run

```bash
# TUI
.venv/bin/python -m tui

# CLI
.venv/bin/python st.py agent
.venv/bin/python st.py ships -v
.venv/bin/python st.py autopilot <SHIP> --contract <ID> --sell
```

## Layout

```
st.py          CLI + autopilot bot
api.py         SpaceTraders v2 API client
config.py      env-based config
tui/           Textual TUI (app, widgets, views, bots, theme)
```
