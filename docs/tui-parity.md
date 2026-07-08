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

- [ ] **Orchestrator configuration.** Both UIs currently start the orchestrator
  with defaults. When the web UI grows controls for `--expand` ship type,
  `--credit-buffer`, `--max-ships`, and `--cross-system`, mirror them in the TUI
  Automation pane's orchestrate control.

## Notes

- Backend modules (`api`, `store`, `arbitrage`, `routing`, `fleet`,
  `orchestrator`, `claims`, `onboarding`) are shared by both UIs — parity work is
  almost always just view/control wiring, not new logic.
- The TUI Analytics pane is currently *ahead* of the web Analytics view (it also
  shows a market-activity breakdown and a top-routes table). If the web Analytics
  view should match, that's a **web** todo, tracked separately when it comes up.
