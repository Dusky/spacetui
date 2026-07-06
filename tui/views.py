from __future__ import annotations

import datetime as dt

from rich.text import Text
from textual.containers import Container, Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Button, DataTable, Input, RichLog, Static

from .theme import PAL, NAV_STATUS, ratio_color
from .widgets import ContractCard, Gauge, Panel, Pill, ShipCard, Stat

CHEAP, GOOD, DEAR = PAL.success, PAL.primary, PAL.danger


class FleetAction(Message):
    def __init__(self, kind: str, ship: str, waypoint: str = "") -> None:
        self.kind = kind
        self.ship = ship
        self.waypoint = waypoint
        super().__init__()


class ContractAction(Message):
    def __init__(self, kind: str, cid: str) -> None:
        self.kind = kind
        self.cid = cid
        super().__init__()


class Pane(VerticalScroll):
    DEFAULT_CSS = "Pane { padding: 0 1; }"
    title = "PANE"
    sub = ""

    def compose(self):
        yield Static(self.title, classes="pane-title")
        if self.sub:
            yield Static(self.sub, classes="pane-sub")
        yield from self.body()

    def body(self):
        return []


# ---------------- Agent ----------------------------------------------------
class AgentPane(Pane):
    title = "◈  AGENT OVERVIEW"
    sub = "live ledger · holdings · fleet status"

    def body(self):
        with Container(classes="stat-grid") as g:
            yield Stat("CREDITS", "—", accent="--green", id="st-credits")
            yield Stat("SHIPS", "—", accent="--gold", id="st-ships")
            yield Stat("FACTION", "—", accent="--pink", id="st-faction")
            yield Stat("HQ", "—", id="st-hq")
            yield Stat("CONTRACTS", "—", id="st-contracts")
            yield Stat("AVG FUEL", "—", id="st-fuel")
        self.event_log = RichLog(id="event-log", wrap=True, markup=True)
        yield Panel("RECENT EVENTS", self.event_log, subtitle="auto")

    def refresh_state(self, app) -> None:
        a = app.agent or {}
        ships = app.ships or []
        self.query_one("#st-credits", Stat).set_value(f"{a.get('credits', 0):,}")
        self.query_one("#st-ships", Stat).set_value(str(a.get("shipCount", len(ships))))
        self.query_one("#st-faction", Stat).set_value(a.get("startingFaction", "—"))
        self.query_one("#st-hq", Stat).set_value(a.get("headquarters", "—"))
        n_ct = len(app.contracts or [])
        self.query_one("#st-contracts", Stat).set_value(str(n_ct))
        fuel_cap = sum(s.get("fuel", {}).get("capacity", 0) for s in ships)
        fuel_cur = sum(s.get("fuel", {}).get("current", 0) for s in ships)
        self.query_one("#st-fuel", Stat).set_value(
            f"{int(fuel_cur / fuel_cap * 100)}%" if fuel_cap else "—"
        )

    def log(self, msg: str) -> None:
        ts = dt.datetime.now().strftime("%H:%M:%S")
        self.event_log.write(f"[dim]{ts}[/]  {msg}")


# ---------------- Fleet ----------------------------------------------------
class FleetPane(Pane):
    title = "⊳  FLEET COMMAND"
    sub = "j/k focus  ·  enter actions on the focused ship"

    def __init__(self):
        super().__init__(id="fleet")
        self.cards: dict[str, ShipCard] = {}
        self.order: list[str] = []
        self.focus = ""

    def body(self):
        yield Container(classes="ship-grid", id="ship-grid")
        self.detail_head = Static("", classes="ship-name")
        self.detail_meta = Static("", classes="meta-line")
        self.fuel_g = Gauge("FUEL", 0, 100)
        self.cargo_g = Gauge("CARGO", 0, 100, color=PAL.primary)
        self.inv = Static("ready", classes="meta-line")
        self.wp_input = Input(placeholder="waypoint e.g. X1-N85-B9", id="wp-input")
        buttons = [
            Button("Orbit", id="act-orbit", classes="btn"),
            Button("Dock", id="act-dock", classes="btn"),
            Button("Refuel", id="act-refuel", classes="btn --primary"),
            Button("Extract", id="act-extract", classes="btn --primary"),
            Button("Sell All", id="act-sell", classes="btn --gold"),
            Button("Go →", id="act-navigate", classes="btn"),
        ]
        yield Panel(
            "FOCUSED SHIP",
            self.detail_head,
            self.detail_meta,
            self.fuel_g,
            self.cargo_g,
            self.inv,
            Static("navigate to:", classes="meta-line"),
            self.wp_input,
            Horizontal(*buttons, classes="actions"),
            subtitle="j/k to cycle",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not event.button.id or not event.button.id.startswith("act-"):
            return
        kind = event.button.id.removeprefix("act-")
        if not self.focus:
            return
        self.post_message(FleetAction(kind, self.focus, self.wp_input.value.strip()))

    def _ensure_cards(self, ships: list[dict]) -> None:
        grid = self.query_one("#ship-grid", Container)
        seen = {s["symbol"] for s in ships}
        for sym, card in list(self.cards.items()):
            if sym not in seen:
                card.remove()
                del self.cards[sym]
        for s in ships:
            sym = s["symbol"]
            if sym not in self.cards:
                card = ShipCard(s)
                self.cards[sym] = card
                grid.mount(card)
        self.order = [s["symbol"] for s in ships]
        if not self.order:
            self.focus = ""
        elif self.focus not in self.order:
            self.focus = self.order[0]

    def refresh_state(self, app) -> None:
        ships = app.ships or []
        self._ensure_cards(ships)
        for s in ships:
            card = self.cards.get(s["symbol"])
            if card:
                card.update(s)
                card.select(s["symbol"] == self.focus)
        self._refresh_detail(app)

    def _refresh_detail(self, app) -> None:
        if not self.focus or self.focus not in self.cards:
            self.detail_head.update(Text("no ship focused", style=PAL.text_muted))
            return
        ship = next((s for s in app.ships if s["symbol"] == self.focus), None)
        if not ship:
            return
        nav = ship.get("nav", {})
        reg = ship.get("registration", {})
        self.detail_head.update(
            Text.assemble(
                (self.focus, PAL.text),
                ("  ·  ", PAL.text_muted),
                (ship.get("frame", {}).get("name", "?"), PAL.text_dim),
            )
        )
        status = nav.get("status", "")
        status_col = {
            "DOCKED": PAL.primary,
            "IN_ORBIT": PAL.accent,
            "IN_TRANSIT": PAL.secondary,
        }.get(status, PAL.text)
        self.detail_meta.update(
            Text.assemble(
                (status, status_col),
                (" @ ", PAL.text_muted),
                (nav.get("waypointSymbol", ""), PAL.text),
                (" · role ", PAL.text_muted),
                (reg.get("role", "?").title(), PAL.accent),
                (" · mode ", PAL.text_muted),
                (nav.get("flightMode", ""), PAL.text_dim),
            )
        )
        fuel = ship.get("fuel", {})
        self.fuel_g.set(fuel.get("current", 0), fuel.get("capacity", 1))
        cargo = ship.get("cargo", {})
        self.cargo_g.set(cargo.get("units", 0), cargo.get("capacity", 1))
        inv = cargo.get("inventory", [])
        if inv:
            self.inv.update(
                Text(" · ".join(f"{i['units']} {i['symbol']}" for i in inv), style=PAL.text_dim)
            )
        else:
            self.inv.update(Text("cargo empty", style=PAL.text_muted))

    def cycle(self, delta: int) -> None:
        if not self.order:
            return
        if self.focus not in self.order:
            self.focus = self.order[0]
        else:
            i = self.order.index(self.focus)
            self.focus = self.order[(i + delta) % len(self.order)]
        for sym, card in self.cards.items():
            card.select(sym == self.focus)

    def focus_ship(self, symbol: str) -> None:
        if symbol in self.order:
            self.focus = symbol
            for sym, card in self.cards.items():
                card.select(sym == self.focus)


# ---------------- Contracts -----------------------------------------------
class ContractsPane(Pane):
    title = "§  CONTRACTS"
    sub = "accept procurements · deliver goods · bank the payout"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.cards: dict[str, ContractCard] = {}
        self.buttons: dict[str, Button] = {}

    def body(self):
        yield Container(id="contract-list")

    def _sync_button(self, btn: Button, c: dict) -> None:
        if c.get("fulfilled"):
            btn.label = "✓ Fulfilled"
            btn.disabled = True
        elif c.get("accepted"):
            btn.label = "◌ In progress"
            btn.disabled = True
        else:
            btn.label = "Accept"
            btn.disabled = False
        btn.set_class(btn.disabled, "--ghost")
        btn.set_class(not btn.disabled, "--primary")

    def refresh_state(self, app) -> None:
        lst = self.query_one("#contract-list", Container)
        seen = {c["id"] for c in (app.contracts or [])}
        for cid, card in list(self.cards.items()):
            if cid not in seen:
                card.parent.remove()
                del self.cards[cid]
                self.buttons.pop(cid, None)
        for c in app.contracts or []:
            cid = c["id"]
            if cid not in self.cards:
                card = ContractCard(c)
                btn = Button("Accept", id=f"cta-{cid}", classes="btn --primary")
                self._sync_button(btn, c)
                wrap = Container(card, btn, classes="contract-wrap")
                self.cards[cid] = card
                self.buttons[cid] = btn
                lst.mount(wrap)
            else:
                self.cards[cid].update(c)
                self._sync_button(self.buttons[cid], c)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id and event.button.id.startswith("cta-"):
            cid = event.button.id.removeprefix("cta-")
            self.post_message(ContractAction("accept", cid))


# ---------------- Markets -------------------------------------------------
class MarketsPane(Pane):
    """Live market at the focused ship's waypoint + best routes from the
    shared price ledger. All API calls happen off the UI thread."""

    title = "$  MARKETS"
    sub = "focused ship's market · best known trade routes"

    def body(self):
        yield Panel("FOCUSED WAYPOINT", Static("no focused ship", id="m-focus-name"), classes="m-panel")
        yield DataTable(id="m-focus-table", classes="market-table")
        yield Panel(
            "BEST KNOWN ROUTES",
            Static("gathered from every market your ships visit", id="m-routes-name"),
            classes="m-panel",
        )
        yield DataTable(id="m-routes-table", classes="market-table")

    def refresh_state(self, app) -> None:
        focus_wp = ""
        if app.fleet_pane and app.fleet_pane.focus:
            ship = next((s for s in (app.ships or []) if s["symbol"] == app.fleet_pane.focus), None)
            if ship:
                focus_wp = ship.get("nav", {}).get("waypointSymbol", "")
        if not focus_wp and app.hq:
            focus_wp = app.hq
        app.run_worker(
            lambda: self._fetch(app, focus_wp),
            thread=True,
            exclusive=True,
            group="markets",
        )

    # runs in a worker thread
    def _fetch(self, app, focus_wp: str) -> None:
        from navigation import system_of

        market = None
        if focus_wp:
            try:
                market = app.client.market(system_of(focus_wp), focus_wp)
                app.db.record_market(market)
            except Exception:  # noqa: BLE001 - waypoint may have no market
                market = None
        routes = []
        try:
            from market import best_routes

            system = system_of(focus_wp or app.hq)
            if system:
                routes = best_routes(app.db, app.cache, system)[:12]
        except Exception:  # noqa: BLE001
            routes = []
        app.call_from_thread(self._fill, focus_wp, market, routes)

    # back on the UI thread
    def _fill(self, focus_wp: str, market: dict | None, routes: list) -> None:
        table = self.query_one("#m-focus-table", DataTable)
        if not table.columns:
            table.add_columns("Good", "Type", "Buy", "Sell", "Vol", "Supply")
        table.clear()
        name = self.query_one("#m-focus-name", Static)
        if market is None:
            name.update(Text(f"{focus_wp or '—'} · no market here", style=PAL.text_muted))
        else:
            name.update(
                Text.assemble((market.get("symbol", ""), PAL.text), ("  · live prices", PAL.text_muted))
            )
            goods = sorted(
                market.get("tradeGoods", []),
                key=lambda g: (g.get("type", ""), -(g.get("purchasePrice") or 0)),
            )
            for g in goods:
                typ = g.get("type", "")
                tcol = {"IMPORT": PAL.warning, "EXPORT": PAL.success, "EXCHANGE": PAL.primary}.get(
                    typ, PAL.text_dim
                )
                table.add_row(
                    Text(g.get("symbol", ""), style=PAL.text),
                    Text(typ, style=tcol),
                    Text(str(g.get("purchasePrice", "-")), style=PAL.text),
                    Text(str(g.get("sellPrice", "-")), style=PAL.text_dim),
                    Text(str(g.get("tradeVolume", "-")), style=PAL.text_muted),
                    Text(g.get("supply", ""), style=PAL.text_muted),
                )

        rtable = self.query_one("#m-routes-table", DataTable)
        if not rtable.columns:
            rtable.add_columns("Good", "Buy @", "", "Sell @", "", "+/unit", "Dist")
        rtable.clear()
        for r in routes:
            rtable.add_row(
                Text(r.good, style=PAL.text),
                Text(r.buy_waypoint, style=PAL.text_dim),
                Text(str(r.buy_price), style=PAL.success),
                Text(r.sell_waypoint, style=PAL.text_dim),
                Text(str(r.sell_price), style=PAL.warning),
                Text(f"+{r.margin}", style=PAL.secondary),
                Text(f"{r.dist:.0f}", style=PAL.text_muted),
            )
        if not routes:
            self.query_one("#m-routes-name", Static).update(
                Text("no routes yet — run a probe/trader bot to gather prices", style=PAL.text_muted)
            )


# ---------------- Automation ----------------------------------------------
class AutomationPane(Pane):
    title = "⚙  AUTOMATION"
    sub = "◇ cycles the bot type (mine / trade / contract / probe) · ▶ launches it"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.rows: dict[str, object] = {}

    def body(self):
        yield Container(classes="auto-grid", id="bot-grid")
        self.bot_log = RichLog(id="botlog", wrap=True, markup=True)
        yield Panel("BOT CONSOLE", self.bot_log, subtitle="live")

    def refresh_state(self, app) -> None:
        grid = self.query_one("#bot-grid", Container)
        seen = {s["symbol"] for s in (app.ships or [])}
        for sym, row in list(self.rows.items()):
            if sym not in seen:
                row.remove()
                del self.rows[sym]
        for s in app.ships or []:
            sym = s["symbol"]
            if sym not in self.rows:
                from automation import BOT_TYPES, default_bot_for

                from .widgets import BotRow

                cls = default_bot_for(s)
                kind = next((k for k, v in BOT_TYPES.items() if v is cls), "mine")
                row = BotRow(s, kind=kind)
                self.rows[sym] = row
                grid.mount(row)

    def log(self, msg: str) -> None:
        ts = dt.datetime.now().strftime("%H:%M:%S")
        self.bot_log.write(f"[dim]{ts}[/]  {msg}")

    def set_bot_state(self, sym, running, last="", mode="") -> None:
        row = self.rows.get(sym)
        if row:
            row.set_state(running, last=last, mode=mode)
