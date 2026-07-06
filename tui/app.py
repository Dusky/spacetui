from __future__ import annotations

import time
import threading

from rich.text import Text

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, ContentSwitcher, Static
from textual import work

import config
from api import ApiError, Client

from .bots import MinerBot
from .theme import PAL
from .views import (
    AgentPane,
    AutomationPane,
    ContractAction,
    ContractsPane,
    FleetAction,
    FleetPane,
    MarketsPane,
)
from .widgets import NAV_LABELS


def nav_label(icon: str, name: str, key: str) -> Text:
    t = Text()
    t.append(f"{icon} ", style=PAL.primary)
    t.append(f"{name:<10}", style=PAL.text)
    t.append(key, style=PAL.text_muted)
    return t


class SpaceTradersApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "SpaceTraders · Nebula HUD"

    BINDINGS = [
        Binding("1", "switch('agent')", "Agent"),
        Binding("2", "switch('fleet')", "Fleet"),
        Binding("3", "switch('contracts')", "Contracts"),
        Binding("4", "switch('markets')", "Markets"),
        Binding("5", "switch('automation')", "Automate"),
        Binding("j", "fleet_cycle(1)", "next ship"),
        Binding("k", "fleet_cycle(-1)", "prev ship"),
        Binding("r", "refresh", "refresh"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self):
        super().__init__()
        self.client = Client(token=config.require_agent_token())
        self.hq = config.HQ
        self.agent: dict = {}
        self.ships: list[dict] = []
        self.contracts: list[dict] = []
        self.current_contract_id = ""
        self.bots: dict[str, MinerBot] = {}
        self.active_tab = "agent"
        self._poll_now = threading.Event()
        self._last_poll = ""
        self._poll_ok = True
        self._clock_on = False

        self.agent_pane = AgentPane(id="agent")
        self.fleet_pane = FleetPane()
        self.contracts_pane = ContractsPane(id="contracts")
        self.markets_pane = MarketsPane(id="markets")
        self.automation_pane = AutomationPane(id="automation")
        self._panes = {
            "agent": self.agent_pane,
            "fleet": self.fleet_pane,
            "contracts": self.contracts_pane,
            "markets": self.markets_pane,
            "automation": self.automation_pane,
        }

    # -- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="sidebar"):
            yield Static("◢◣  SPACETRADERS", classes="brand")
            yield Static("v2  ·  nebula hud", classes="brand-sub")
            yield Static(classes="brand-rule")
            with Vertical(classes="nav"):
                for tab, key, label, icon in NAV_LABELS:
                    yield Button(
                        nav_label(icon, label, key),
                        id=f"nav-{tab}",
                        classes="nav-item",
                    )
            with Vertical(classes="agent-mini"):
                yield Static("credits  —", classes="am-line", id="am-credits")
                yield Static("hq       —", classes="am-line", id="am-hq")
                yield Static("ships    —", classes="am-line", id="am-ships")
        with Vertical(id="main"):
            with ContentSwitcher(id="switcher", initial="agent"):
                yield self.agent_pane
                yield self.fleet_pane
                yield self.contracts_pane
                yield self.markets_pane
                yield self.automation_pane
        with Horizontal(id="statusbar"):
            yield Static(" MDOE ", classes="sb-item --hot", id="sb-agent")
            yield Static(" credits — ", classes="sb-item --green", id="sb-credits")
            yield Static(" — ", classes="sb-item", id="sb-hq")
            yield Static(" ● polling ", classes="sb-item --green", id="sb-poll")
            yield Static(" bots 0 ", classes="sb-item --gold --right", id="sb-bots")
            yield Static(" --:--:-- ", classes="sb-item --right", id="sb-clock")
        with Horizontal(id="hintbar"):
            yield Static(
                "  1-5 panes   j/k cycle ship   r refresh   enter clicks   q quit",
                id="hint-text",
            )

    def on_mount(self) -> None:
        self._set_nav_active("agent")
        self._poll_loop()
        self.set_interval(1, self._tick_clock)

    # -- polling -----------------------------------------------------------
    @work(thread=True, exclusive=True, group="poll")
    def _poll_loop(self):
        import time as _t

        while True:
            try:
                agent = self.client.my_agent()
                ships = self.client.ships()
                contracts = self.client.contracts()
                self.call_from_thread(self._apply_state, agent, ships, contracts, True)
            except Exception as e:  # noqa
                self.call_from_thread(self._apply_state, None, None, None, False, str(e))
            self._poll_now.clear()
            for _ in range(40):
                if self._poll_now.is_set():
                    break
                _t.sleep(0.1)

    def _apply_state(self, agent, ships, contracts, ok, err=None):
        self._last_poll = time.strftime("%H:%M:%S")
        self._poll_ok = ok
        if ok:
            self.agent = agent or {}
            self.ships = ships or []
            self.contracts = contracts or []
            if not self.current_contract_id:
                pending = [c for c in self.contracts if c.get("accepted") and not c.get("fulfilled")]
                if pending:
                    self.current_contract_id = pending[0]["id"]
                elif self.contracts:
                    self.current_contract_id = self.contracts[0]["id"]
        # refresh active pane
        try:
            self._panes[self.active_tab].refresh_state(self)
        except Exception as e:  # noqa
            pass
        # sidebar mini
        a = self.agent
        self._set_static("#am-credits", f"credits  {a.get('credits', 0):,}")
        self._set_static("#am-hq", f"hq       {a.get('headquarters', '—')}")
        self._set_static("#am-ships", f"ships    {a.get('shipCount', 0)}")
        # statusbar
        self._set_static("#sb-credits", f" {a.get('credits', 0):,}c ")
        self._set_static("#sb-hq", f" {a.get('headquarters', '—')} ")
        poll = self.query_one("#sb-poll", Static)
        if ok:
            poll.update(Text(f" ● live {self._last_poll} ", style=PAL.success))
        else:
            poll.update(Text(f" ✕ {err or 'error'} ", style=PAL.danger))
        n_bots = len(self.bots)
        self._set_static("#sb-bots", f" bots {n_bots} ")

    def _set_static(self, selector, text):
        try:
            self.query_one(selector, Static).update(text)
        except Exception:
            pass

    def _tick_clock(self):
        self._set_static("#sb-clock", " " + time.strftime("%H:%M:%S") + " ")

    # -- navigation --------------------------------------------------------
    def action_switch(self, tab: str) -> None:
        self.active_tab = tab
        self.query_one("#switcher", ContentSwitcher).current = tab
        self._set_nav_active(tab)
        try:
            self._panes[tab].refresh_state(self)
        except Exception:
            pass

    def _set_nav_active(self, tab: str) -> None:
        for t, *_ in NAV_LABELS:
            try:
                btn = self.query_one(f"#nav-{t}", Button)
                btn.set_class(t == tab, "--active")
            except Exception:
                pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("nav-"):
            self.action_switch(bid.removeprefix("nav-"))

    def action_fleet_cycle(self, delta: int) -> None:
        if self.active_tab != "fleet":
            self.action_switch("fleet")
        self.fleet_pane.cycle(delta)
        self.fleet_pane.refresh_state(self)

    def action_refresh(self) -> None:
        self._poll_now.set()

    # -- messages from panes ----------------------------------------------
    def on_fleet_action(self, msg: FleetAction) -> None:
        self.run_worker(self._do_fleet_action, msg, thread=True, exclusive=False)

    def on_contract_action(self, msg: ContractAction) -> None:
        self.run_worker(self._do_contract_action, msg, thread=True, exclusive=False)

    def on_bot_row_toggle(self, msg) -> None:
        from .widgets import BotRow

        sym = msg.symbol
        if sym in self.bots:
            self._stop_bot(sym)
        else:
            self._start_bot(sym)

    # -- bot management ----------------------------------------------------
    def _start_bot(self, ship: str) -> None:
        if ship in self.bots:
            return
        bot = MinerBot(
            self.client,
            ship,
            contract=self.current_contract_id or None,
            sell=True,
            on_log=lambda m: self.call_from_thread(self._bot_log, m),
            on_status=lambda **k: self.call_from_thread(self._bot_status, ship, **k),
        )
        self.bots[ship] = bot

        def _run():
            bot.run()

        self.run_worker(_run, thread=True, name=f"bot-{ship}", group=f"bot-{ship}")
        self._bot_log(f"{ship}  bot started (contract={self.current_contract_id or '-'})")
        self.automation_pane.set_bot_state(ship, True, last="starting")

    def _stop_bot(self, ship: str) -> None:
        bot = self.bots.pop(ship, None)
        if bot:
            bot.stop()
        self.automation_pane.set_bot_state(ship, False, last="stopped")

    def _bot_log(self, msg: str) -> None:
        try:
            self.automation_pane.log(msg)
        except Exception:
            pass
        try:
            self.agent_pane.log(msg)
        except Exception:
            pass

    def _bot_status(self, ship: str, **k) -> None:
        try:
            self.automation_pane.set_bot_state(
                ship, k.get("running", False), last=k.get("last", ""), mode=k.get("mode", "")
            )
        except Exception:
            pass

    # -- fleet actions (worker) -------------------------------------------
    def _do_fleet_action(self, msg: FleetAction) -> None:
        ship, kind, wp = msg.ship, msg.kind, msg.waypoint
        try:
            if kind == "orbit":
                self.client.orbit(ship)
            elif kind == "dock":
                self.client.dock(ship)
            elif kind == "refuel":
                self.client.refuel(ship)
            elif kind == "extract":
                self.client.orbit(ship)
                data = self.client.extract(ship)
                y = data.get("extraction", {}).get("yield", {})
                self.call_from_thread(
                    self._bot_log, f"{ship}  +{y.get('units', 0)} {y.get('symbol', '')}"
                )
            elif kind == "sell":
                inv = self.client.cargo(ship).get("inventory", [])
                for it in inv:
                    d = self.client.sell(ship, it["symbol"], it["units"])
                    t = d.get("transaction", {})
                    self.call_from_thread(
                        self._bot_log,
                        f"{ship}  sold {t.get('units')} {t.get('symbol')} = {t.get('totalPrice')}c",
                    )
            elif kind == "navigate":
                if not wp:
                    self.call_from_thread(self._bot_log, f"{ship}  navigate needs a waypoint")
                    return
                self.client.navigate(ship, wp)
                self.call_from_thread(self._bot_log, f"{ship}  → {wp}")
            self._poll_now.set()
        except ApiError as e:
            self.call_from_thread(self._bot_log, f"{ship}  {kind} failed: {e.message}")
        except Exception as e:
            self.call_from_thread(self._bot_log, f"{ship}  {kind} error: {e!r}")

    def _do_contract_action(self, msg: ContractAction) -> None:
        try:
            if msg.kind == "accept":
                self.client.accept_contract(msg.cid)
                self.call_from_thread(self._bot_log, f"contract {msg.cid[:8]} accepted")
                self._poll_now.set()
        except ApiError as e:
            self.call_from_thread(self._bot_log, f"contract action failed: {e.message}")
