# TUI parity backlog

The **web UI (`web/`) is the primary surface** for new features. The Textual TUI
(`tui/`) will catch up later. This file tracks features that exist in the web UI
(or backend) but are **not yet wired into the TUI**, so the TUI can be brought to
parity in a focused pass.

**Convention:** when a web-UI feature is added that the TUI lacks, append a
checklist item here in the same commit. Check it off when the TUI gains it.

## Backlog (web → TUI)

- [ ] **Scout bot control.** Web Automation has Mine/Trade/**Scout** per ship;
  the TUI `BotRow` (`tui/widgets.py`) only has Mine/Trade/Stop. Add a Scout
  button that starts a `ScoutBot`, wired through `tui/app.py::_start_bot`.

- [ ] **Market lookup by waypoint.** Web Markets has an input to fetch any
  waypoint's live market on demand (`GET /api/market/<wp>`). The TUI
  `MarketsPane` (`tui/views.py`) only shows the focused ship's waypoint + HQ.
  Add an input + fetch.

- [ ] **Top routes / deals in Markets.** Web Markets shows the best arbitrage
  routes (`store.best_routes`). The TUI surfaces routes only in Analytics; add a
  routes table to the TUI Markets pane too (or keep it Analytics-only — decide).

- [ ] **Orchestrator configuration.** The **web UI now** has form controls for
  reinvest ship type (`--expand`), credit reserve (`--credit-buffer`), max ships,
  cross-system, and **auto-contracts**. The TUI orchestrate button still starts
  with defaults — add the same options (an input row on the Automation pane, or a
  small modal). Backend supports them all already
  (`Orchestrator(..., auto_contracts=...)`).

- [ ] **In-app onboarding (optional).** The web UI has an in-app setup screen
  (paste token / register) via `POST /api/setup` → `onboarding.save_agent_token`
  / `register_agent`. The TUI runs the same wizard as a *pre-launch terminal*
  prompt (`ensure_onboarded`), which works fine — an in-TUI Textual screen would
  only be a polish upgrade. Low priority.

## Web-only (not TUI debt)

These are web-native rich views the TUI is not expected to match:

- **System map** — a canvas map of the current system (waypoints by x/y, ship
  positions + in-transit routes, click-to-send). Spatial; impractical in a TUI.
- **Price-history drill-down** — click a watchlist good for a buy/sell-over-time
  chart with hover tooltips.
- **Interactive net-worth chart** — hover crosshair + value/time tooltip.
- **Richer ship detail** — cargo manifest, mounts, route/ETA in one panel.

## Notes

- Backend modules (`api`, `store`, `arbitrage`, `routing`, `fleet`,
  `orchestrator`, `claims`, `onboarding`) are shared by both UIs — parity work is
  almost always just view/control wiring, not new logic.
- The TUI Analytics pane is currently *ahead* of the web Analytics view (it also
  shows a market-activity breakdown and a top-routes table). If the web Analytics
  view should match, that's a **web** todo, tracked separately when it comes up.
