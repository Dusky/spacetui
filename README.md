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

# ...reinvest profit into mining drones and keep contracts flowing
.venv/bin/python st.py orchestrate --expand SHIP_MINING_DRONE --max-ships 12 --auto-contracts

# reinvest into whatever ship the fleet's bottleneck needs
.venv/bin/python st.py orchestrate --expand AUTO

# steer the fleet toward a goal (grow | contracts | construct | explore)
.venv/bin/python st.py orchestrate --goal construct --construct <JUMP_GATE_WAYPOINT>

# just keep procurement contracts flowing on one ship (negotiate/accept loop)
.venv/bin/python st.py autocontract <SHIP>
```

With `--auto-contracts`, the orchestrator negotiates and accepts procurement
contracts (declining unprofitable ones) and the miners automatically adopt and
fulfill the active one.

Ship roles are chosen automatically:

- **miner** — has a mining laser / surveyor mount
- **trader** — has cargo capacity (buys low, sells high; `--cross-system` to range across jump gates)
- **scout** — a bare probe; tours markets keeping prices fresh (charts uncharted waypoints under `--goal explore`)

**Goals** give the controller a long-horizon objective: `grow` reinvests profit,
`contracts` keeps procurement flowing, `construct <waypoint>` turns a hauler into
a construction supplier for the endgame jump gate while the rest of the fleet
funds it, and `explore` sends scouts out to chart the galaxy. Routes are ranked
by estimated **credits per hour** (not just per-unit margin), and traders ease
off a market that their own fills are depressing.

One shared **world model** caches waypoints/markets/shipyards once per process
(persisted, so a restart starts warm) — so N bots cost roughly one bot's worth of
discovery traffic instead of N.

`st.py metrics` prints mission KPIs (credits/hour, ROI per ship, API budget) and
`st.py metrics`'s companion `st.py alerts` surfaces stranded/idle ships, contract
deadlines and rate-limit pressure. `st.py construct <waypoint> <ship>` runs a
single construction supplier.

In the TUI, the **Automate** pane has an *Orchestrate Fleet* button that does the same.

## Web dashboard

`st.py web` serves a browser dashboard (Flask) that mirrors the TUI — Overview, a
**Mission** control console (KPI cards, a live alert feed, ROI per ship, and the
goal-driven orchestrator), Fleet, Contracts, Markets, an interactive system
**Map**, Automation, and Analytics (with price-history drill-down and a hoverable
net-worth chart) — with live controls
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
st.py           CLI (fleet ops, trading, expansion, analytics, metrics, goals)
api.py          SpaceTraders v2 API client (shared rate limiter)
store.py        SQLite: prices, jump gates, trades, net-worth, waypoints, assignments
world.py        shared cached world model (waypoints/markets/shipyards, single-flight)
arbitrage.py    pure route scanner + credits/hour ranking + demand damping
routing.py      pure jump-gate graph + pathfinding
fleet.py        ship purchasing + FleetManager + bottleneck-aware expansion
orchestrator.py Fleet Orchestrator (classify → deploy → reinvest → goals)
construction.py pure construction-site planning (jump-gate supply chain)
contracts.py    contract lifecycle (accept/negotiate/fulfill, decline unwinnable)
metrics.py      pure KPI/ROI/alert aggregation for mission control
claims.py       route claims so traders don't stampede one deal
onboarding.py   first-run setup wizard
tui/            Textual TUI (app, widgets, views, bots, charts, theme)
web/            Flask web dashboard (server, hub, static SPA)
```
