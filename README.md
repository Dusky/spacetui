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
# Web dashboard (browser UI)
.venv/bin/python st.py web            # http://127.0.0.1:8000

# TUI
.venv/bin/python -m tui

# CLI
.venv/bin/python st.py agent
.venv/bin/python st.py ships -v
.venv/bin/python st.py autopilot <SHIP> --contract <ID> --sell
.venv/bin/python st.py trade <SHIP> --cross-system     # arbitrage trader
.venv/bin/python st.py deals                           # best known routes
.venv/bin/python st.py stats                           # net worth / P&L dashboard
```

## Fleet Orchestrator

One command runs the whole fleet: it classifies each ship and deploys the right
bot, auto-deploys bots to ships bought mid-run, and (optionally) reinvests
profit into more ships.

```bash
# deploy a bot to every ship and keep them working
.venv/bin/python st.py orchestrate

# ...and reinvest profit into mining drones, keeping a 100k reserve
.venv/bin/python st.py orchestrate --expand SHIP_MINING_DRONE --credit-buffer 100000 --max-ships 12
```

Ship roles are chosen automatically:

- **miner** — has a mining laser / surveyor mount
- **trader** — has cargo capacity (buys low, sells high; `--cross-system` to range across jump gates)
- **scout** — a bare probe; tours markets keeping prices fresh for the traders

In the TUI, the **Automate** pane has an *Orchestrate Fleet* button that does the same.

## Web dashboard

`st.py web` serves a browser dashboard (Flask) that mirrors the TUI — Overview,
Fleet, Contracts, Markets, Automation, and Analytics — with live controls
(start/stop the orchestrator and per-ship bots, run fleet actions). A single
background poller refreshes a cached snapshot, so opening it in a browser adds no
SpaceTraders API traffic; every call still funnels through the shared rate
limiter. State and logs stream to the browser over Server-Sent Events (with a
polling fallback), so the dashboard updates instantly instead of on a timer. Binds to `127.0.0.1` by default (`--host` / `--port` to change).

**First run:** if there's no agent token yet, the dashboard opens a setup screen
— paste an agent token or register a new agent from your account token, and it
writes `.env` for you (no terminal needed).

**Phone / LAN access:** `st.py web --host 0.0.0.0` exposes it on your network.
A token is required for non-local hosts (auto-generated and printed, or set your
own with `--token`); open the printed `http://<ip>:8000/?token=...` link once and
it sets a cookie for that device. Localhost needs no token.

## Layout

```
st.py           CLI (fleet ops, trading, expansion, analytics)
api.py          SpaceTraders v2 API client (shared rate limiter)
store.py        SQLite: market prices, jump gates, trades, net-worth history
arbitrage.py    pure same/cross-system route scanner
routing.py      pure jump-gate graph + pathfinding
fleet.py        ship purchasing + FleetManager
orchestrator.py Fleet Orchestrator (classify → deploy → reinvest)
claims.py       route claims so traders don't stampede one deal
onboarding.py   first-run setup wizard
tui/            Textual TUI (app, widgets, views, bots, charts, theme)
web/            Flask web dashboard (server, hub, static SPA)
```
