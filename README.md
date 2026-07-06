# spacetui

A terminal UI + CLI automation suite for playing [SpaceTraders](https://spacetraders.io/) v2.

Run autonomous bots across your whole fleet — mining, arbitrage trading, contract
running, market scanning — from a custom-themed Textual TUI ("Nebula HUD") or a
plain argparse CLI. Every ship that visits a market feeds a shared SQLite price
ledger that the traders and contract runners plan against.

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

## TUI

```bash
.venv/bin/python -m tui
```

Panes: **1** Agent · **2** Fleet · **3** Contracts · **4** Markets (live prices +
best known trade routes) · **5** Automate. On the Automate pane, `◇` cycles a
ship's bot type (mine / trade / contract / probe) and `▶ Start` launches it;
logs stream into the bot console.

## CLI

```bash
# manual play
.venv/bin/python st.py agent
.venv/bin/python st.py ships -v
.venv/bin/python st.py navigate MDOE-1 X1-N85-B9
.venv/bin/python st.py market X1-N85-A1
.venv/bin/python st.py deliver <CONTRACT> MDOE-1 COPPER_ORE 40
.venv/bin/python st.py buy-ship SHIP_MINING_DRONE X1-N85-A1

# market intelligence (fed by your bots' travels)
.venv/bin/python st.py prices            # recorded prices in your HQ system
.venv/bin/python st.py routes            # best buy-low/sell-high routes

# single-ship bots
.venv/bin/python st.py bot mine MDOE-2 --contract <ID>
.venv/bin/python st.py bot trade MDOE-1 --min-margin 3
.venv/bin/python st.py bot contract MDOE-1
.venv/bin/python st.py bot probe MDOE-3

# the whole fleet at once: assigns a bot per ship by role,
# restarts crashed bots, optionally reinvests profits into new ships
.venv/bin/python st.py fleet --autobuy SHIP_MINING_DRONE --reserve 60000
```

## Bots

| Bot | Ships | What it does |
| --- | --- | --- |
| `mine` | excavators | survey-aware mining; delivers contract goods via the contract API, auto-fulfills, sells the rest at the best-paying market |
| `trade` | haulers | plans buy-low/sell-high routes from the price ledger; scouts unpriced markets when data is thin |
| `contract` | command ships | negotiate → accept → procure (buy if cheaper, else mine) → deliver → fulfill, forever |
| `probe` | satellites | tours every marketplace recording prices for the fleet |

All bots share one API client with a client-side rate limiter (the API allows
~2 req/s), refuel opportunistically at the nearest market, and fall back to
DRIFT mode when the tank can't cover a leg.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

The suite drives the bots end-to-end against an in-memory fake of the
SpaceTraders API — no token or network needed.

## Layout

```
st.py            CLI (manual commands, bots, fleet commander)
api.py           SpaceTraders v2 API client + rate limiter
navigation.py    distance/fuel/time math, waypoint cache, fuel-aware navigator
market.py        SQLite price ledger + trade route planner
automation/      MinerBot, TraderBot, ContractBot, ProbeBot, FleetCommander
tui/             Textual TUI (app, widgets, views, theme)
tests/           unit tests + fake API world
config.py        env-based config
```
