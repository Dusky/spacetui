# spacetui

A terminal UI + CLI for playing [SpaceTraders](https://spacetraders.io/) v2.

Automate a fleet, manage contracts, mine, trade, and run autonomous bots — from a
custom-themed Textual TUI ("Nebula HUD") or a plain argparse CLI.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Create a `.env` (gitignored) with your tokens:

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
