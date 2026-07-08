from __future__ import annotations

import datetime as dt

from rich.text import Text
from textual.containers import Container, Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Button, DataTable, Input, RichLog, Static

import store
from .charts import BrailleChart, block_sparkline, hbar
from .theme import PAL, NAV_STATUS, ratio_color
from .widgets import ContractCard, Gauge, Panel, Pill, ShipCard, Stat

CHEAP, GOOD, DEAR = PAL.success, PAL.primary, PAL.danger


def _credits(n) -> str:
    return f"{int(n):,}c"


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
                ("   role ", PAL.text_muted),
                (reg.get("role", "?").title(), PAL.accent),
                ("   mode ", PAL.text_muted),
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


# ---------------- Contracts -----------------------------------------------
class ContractsPane(Pane):
    title = "§  CONTRACTS"
    sub = "accept procurements · deliver goods · bank the payout"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.cards: dict[str, ContractCard] = {}

    def body(self):
        yield Container(id="contract-list")

    def refresh_state(self, app) -> None:
        lst = self.query_one("#contract-list", Container)
        seen = {c["id"] for c in (app.contracts or [])}
        for cid, card in list(self.cards.items()):
            if cid not in seen:
                card.remove()
                del self.cards[cid]
        for c in app.contracts or []:
            cid = c["id"]
            if cid not in self.cards:
                card = ContractCard(c)
                accepted = c.get("accepted")
                fulfilled = c.get("fulfilled")
                label = "Fulfilled" if fulfilled else ("Abandon" if accepted else "Accept")
                style = "--ghost" if accepted else "--primary"
                btn = Button(label, id=f"cta-{cid}", classes=f"btn {style}")
                wrap = Container(card, btn, classes="contract-wrap")
                self.cards[cid] = card
                lst.mount(wrap)
                self.call_after_refresh(card.update, c)
            else:
                self.cards[cid].update(c)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id and event.button.id.startswith("cta-"):
            cid = event.button.id.removeprefix("cta-")
            self.post_message(ContractAction("accept", cid))


# ---------------- Markets -------------------------------------------------
class MarketsPane(Pane):
    title = "$  MARKETS"
    sub = "focused ship's waypoint · and your HQ"

    def body(self):
        yield Panel("FOCUSED WAYPOINT", Static("", id="m-focus-name"), classes="m-panel")
        yield DataTable(id="m-focus-table")
        yield Panel("HEADQUARTERS", Static("", id="m-hq-name"), classes="m-panel")
        yield DataTable(id="m-hq-table")

    def refresh_state(self, app) -> None:
        import re

        def system_of(wp):
            m = re.match(r"^(X1-[A-Z0-9]+)-", wp)
            return m.group(1) if m else wp

        def fill(table_id, name_id, wp):
            table = self.query_one(table_id, DataTable)
            if table.columns is None or len(table.columns) == 0:
                table.add_columns("Good", "Type", "Buy", "Sell")
            table.clear()
            try:
                m = app.client.market(system_of(wp), wp)
            except Exception:
                self.query_one(name_id, Static).update(Text(f"{wp} — no market data", style=PAL.text_muted))
                return
            self.query_one(name_id, Static).update(
                Text.assemble((wp, PAL.text), ("  ·  ", PAL.text_muted), (m.get("symbol", ""), PAL.text_dim))
            )
            goods = sorted(
                m.get("tradeGoods", []),
                key=lambda g: (g.get("type", ""), -g.get("purchasePrice", 0)),
            )
            for g in goods:
                typ = g.get("type", "")
                tcol = {"IMPORT": PAL.warning, "EXPORT": PAL.success, "EXCHANGE": PAL.primary}.get(typ, PAL.text_dim)
                table.add_row(
                    Text(g.get("symbol", ""), style=PAL.text),
                    Text(typ, style=tcol),
                    Text(str(g.get("purchasePrice", "-")), style=PAL.text),
                    Text(str(g.get("sellPrice", "-")), style=PAL.text_dim),
                )

        focus_wp = ""
        if app.fleet_pane and app.fleet_pane.focus:
            ship = next((s for s in (app.ships or []) if s["symbol"] == app.fleet_pane.focus), None)
            if ship:
                focus_wp = ship.get("nav", {}).get("waypointSymbol", "")
        if focus_wp:
            fill("#m-focus-table", "#m-focus-name", focus_wp)
        else:
            self.query_one("#m-focus-name", Static).update(Text("no focused ship", style=PAL.text_muted))
            self.query_one("#m-focus-table", DataTable).clear()
        hq = app.hq
        fill("#m-hq-table", "#m-hq-name", hq)


# ---------------- Automation ----------------------------------------------
class AutomationPane(Pane):
    title = "⚙  AUTOMATION"
    sub = "launch autonomous miners · watch their logs"

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
                from .widgets import BotRow

                row = BotRow(s)
                self.rows[sym] = row
                grid.mount(row)

    def log(self, msg: str) -> None:
        ts = dt.datetime.now().strftime("%H:%M:%S")
        self.bot_log.write(f"[dim]{ts}[/]  {msg}")

    def set_bot_state(self, sym, running, last="", mode="") -> None:
        row = self.rows.get(sym)
        if row:
            row.set_state(running, last=last, mode=mode)


# ---------------- Analytics -----------------------------------------------
class AnalyticsPane(Pane):
    title = "📈  ANALYTICS"
    sub = "net worth · realized P&L · price history · market intel"

    def body(self):
        with Container(classes="stat-grid"):
            yield Stat("NET WORTH", "—", accent="--green", id="an-worth")
            yield Stat("REALIZED NET", "—", accent="--gold", id="an-net")
            yield Stat("TRADES", "—", id="an-trades")
            yield Stat("MARKETS", "—", accent="--pink", id="an-markets")

        self.worth_chart = BrailleChart(height_cells=7, unit="c", id="an-worth-chart")
        yield Panel("NET WORTH OVER TIME", self.worth_chart, subtitle="credits")

        self.watchlist = DataTable(id="an-watchlist")
        yield Panel("GOODS WATCHLIST", self.watchlist, subtitle="sell price trend")

        self.pnl_bars = Static("", id="an-pnl")
        yield Panel("REALIZED P&L BY GOOD", self.pnl_bars, subtitle="net credits")

        self.routes_tbl = DataTable(id="an-routes")
        yield Panel("TOP ROUTES", self.routes_tbl, subtitle="from stored prices")

        self.activity_bars = Static("", id="an-activity")
        yield Panel("MARKET ACTIVITY", self.activity_bars, subtitle="live goods by activity")

    def refresh_state(self, app) -> None:
        self._fill_stats(app)
        self._fill_worth()
        self._fill_watchlist()
        self._fill_pnl()
        self._fill_routes(app)
        self._fill_activity()

    # -- sections ----------------------------------------------------------
    def _fill_stats(self, app) -> None:
        credits = (app.agent or {}).get("credits", 0)
        pnl = store.pnl_summary()
        self.query_one("#an-worth", Stat).set_value(f"{credits:,}")
        net = pnl["net"]
        net_w = self.query_one("#an-net", Stat)
        net_w.set_value(f"{net:+,}")
        self.query_one("#an-trades", Stat).set_value(str(pnl["trades"]))
        self.query_one("#an-markets", Stat).set_value(str(len(store.latest_prices())))

    def _fill_worth(self) -> None:
        series = [r["credits"] for r in store.credit_series(limit=400)]
        self.worth_chart.set_data(series)

    def _fill_watchlist(self) -> None:
        tbl = self.watchlist
        if not tbl.columns:
            tbl.add_columns("Good", "Sell", "Δ", "Trend")
        tbl.clear()
        for sym in store.tracked_goods(limit=12):
            series = store.price_series(sym, limit=60)
            sells = [r["sell_price"] for r in series if r["sell_price"] is not None]
            if not sells:
                continue
            last = sells[-1]
            delta = last - sells[0]
            dcol = PAL.success if delta >= 0 else PAL.danger
            spark = block_sparkline(sells, width=24)
            tbl.add_row(
                Text(sym, style=PAL.text),
                Text(f"{last:,}", style=PAL.text),
                Text(f"{delta:+,}", style=dcol),
                Text(spark, style=dcol),
            )

    def _fill_pnl(self) -> None:
        rows = store.pnl_by_good(limit=8)
        if not rows:
            self.pnl_bars.update(Text("no trades recorded yet", style=PAL.text_muted))
            return
        peak = max((abs(r["net"]) for r in rows), default=1) or 1
        t = Text()
        for r in rows:
            col = PAL.success if r["net"] >= 0 else PAL.danger
            t.append(f"{r['symbol']:<20} ", style=PAL.text_dim)
            t.append(f"{hbar(r['net'], peak, 22):<22} ", style=col)
            t.append(f"{r['net']:+,}c\n", style=col)
        self.pnl_bars.update(t)

    def _fill_routes(self, app) -> None:
        tbl = self.routes_tbl
        if not tbl.columns:
            tbl.add_columns("Good", "Buy @", "Sell @", "Profit", "Hops")
        tbl.clear()
        system = None
        if app.fleet_pane and app.fleet_pane.focus:
            ship = next((s for s in (app.ships or []) if s["symbol"] == app.fleet_pane.focus), None)
            if ship:
                system = ship.get("nav", {}).get("systemSymbol")
        routes = store.best_routes(system=system, min_profit=1, max_hops=2 if system else 0)
        for r in routes[:10]:
            t = r.get("hops", 0)
            tbl.add_row(
                Text(r["good"], style=PAL.text),
                Text(r["buy_wp"], style=PAL.text_dim),
                Text(r["sell_wp"], style=PAL.text_dim),
                Text(f"+{r['profit']:,}", style=PAL.success),
                Text(str(t), style=PAL.warning if t else PAL.text_muted),
            )

    def _fill_activity(self) -> None:
        breakdown = store.activity_breakdown()
        if not breakdown:
            self.activity_bars.update(Text("no market data yet", style=PAL.text_muted))
            return
        peak = max(breakdown.values()) or 1
        palette = {
            "STRONG": PAL.success, "GROWING": PAL.primary, "WEAK": PAL.warning,
            "RESTRICTED": PAL.danger, "UNKNOWN": PAL.text_muted,
        }
        t = Text()
        for name, n in breakdown.items():
            col = palette.get(name, PAL.accent)
            t.append(f"{name:<12} ", style=PAL.text_dim)
            t.append(f"{hbar(n, peak, 22):<22} ", style=col)
            t.append(f"{n}\n", style=PAL.text)
        self.activity_bars.update(t)
