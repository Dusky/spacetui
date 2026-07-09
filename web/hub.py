"""Shared state for the web server.

The Hub is the single owner of live objects — the API client, the background
poller's cached snapshot, per-ship bots, the orchestrator, and a log ring
buffer — mirroring what ``tui/app.py`` does for the TUI. Browsers read the
cached snapshot (one poller refreshes it), so extra web clients add no
SpaceTraders traffic; every real call still funnels through the shared limiter.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque

import config
import store
import world as world_mod
from api import ApiError, Client
from orchestrator import Orchestrator, classify_ship
from tui.bots import MinerBot, ScoutBot, TraderBot

_BOT_CLASSES = {"mine": MinerBot, "trade": TraderBot, "scout": ScoutBot}


class Hub:
    def __init__(self, client: Client):
        self.c = client
        self.hq = config.HQ
        # one shared world model for the poller, the bots and the orchestrator
        self.world = world_mod.bind(client)
        self._lock = threading.Lock()
        self._agent: dict = {}
        self._ships: list[dict] = []
        self._contracts: list[dict] = []
        self._last_poll = ""
        self._poll_ok = True
        self._poll_err = ""
        self.bots: dict[str, object] = {}
        self.orchestrator: Orchestrator | None = None
        self._log: deque = deque(maxlen=300)
        self._poller: threading.Thread | None = None
        self._stop = threading.Event()
        self._subs: set = set()  # SSE subscriber queues

    # -- ship types for sale (feeds the reinvest dropdown) -----------------
    def ship_types(self, system: str | None = None) -> list[dict]:
        system = system or "-".join((self.hq or "").split("-")[:2])
        if not system:
            return []
        return self.world.ship_types(system)

    # -- system waypoints (the map view reads these) -----------------------
    def system_waypoints(self, system: str) -> list[dict]:
        return self.world.get_waypoints(system)

    # -- pub/sub (Server-Sent Events) --------------------------------------
    def subscribe(self) -> "queue.Queue":
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            self._subs.discard(q)

    def _publish(self, event: str, data) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait({"event": event, "data": data})
            except queue.Full:
                pass

    def _push_state(self) -> None:
        self._publish("state", self.snapshot())

    # -- logging -----------------------------------------------------------
    def log(self, msg: str) -> None:
        line = {"t": time.strftime("%H:%M:%S"), "msg": msg}
        with self._lock:
            self._log.append(line)
        self._publish("log", line)

    def log_lines(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(self._log)[-limit:]

    # -- polling -----------------------------------------------------------
    def refresh(self) -> None:
        """One poll cycle: refresh cached agent/ships/contracts (like the TUI)."""
        try:
            agent = self.c.my_agent()
            ships = self.c.ships()
            contracts = self.c.contracts()
        except Exception as e:  # noqa - keep serving the last good snapshot
            with self._lock:
                self._poll_ok = False
                self._poll_err = str(e)
                self._last_poll = time.strftime("%H:%M:%S")
            return
        with self._lock:
            self._agent, self._ships, self._contracts = agent, ships, contracts
            self._poll_ok = True
            self._poll_err = ""
            self._last_poll = time.strftime("%H:%M:%S")
        try:
            store.record_credits(agent.get("credits", 0), agent.get("shipCount", 0))
        except Exception:
            pass
        self._push_state()

    def start_poller(self, interval: float = 5.0) -> None:
        if self._poller and self._poller.is_alive():
            return

        def _loop():
            while not self._stop.is_set():
                self.refresh()
                self._stop.wait(interval)

        self._poller = threading.Thread(target=_loop, daemon=True, name="web-poller")
        self._poller.start()

    def shutdown(self) -> None:
        self._stop.set()
        self.stop_orch()
        for sym in list(self.bots):
            self.stop_bot(sym)

    # -- snapshot ----------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            orch = self.orchestrator
            bots = {
                sym: {"role": getattr(b, "_role", getattr(b, "kind", "bot")),
                      "running": not b.cancelled}
                for sym, b in self.bots.items()
            }
            return {
                "agent": self._agent,
                "ships": self._ships,
                "contracts": self._contracts,
                "last_poll": self._last_poll,
                "poll_ok": self._poll_ok,
                "poll_err": self._poll_err,
                "hq": self.hq,
                "orchestrator": {
                    "running": bool(orch and orch.running),
                    "roster": orch.roster() if orch else {},
                    "config": {
                        "expand": orch.expand_ship_type,
                        "credit_buffer": orch.credit_buffer,
                        "max_ships": orch.max_ships,
                        "cross_system": orch.cross_system,
                        "auto_contracts": orch.auto_contracts,
                        "goal": orch.goal,
                        "construct_waypoint": orch.construct_waypoint,
                    } if orch else {},
                },
                "bots": bots,
            }

    # -- per-ship bots -----------------------------------------------------
    def start_bot(self, ship: str, kind: str) -> None:
        if ship in self.bots:
            return
        cls = _BOT_CLASSES.get(kind, TraderBot)
        bot = cls(self.c, ship, world=self.world, on_log=lambda m: self.log(m))
        bot.kind = kind
        self.bots[ship] = bot
        threading.Thread(target=bot.run, daemon=True, name=f"web-bot-{ship}").start()
        try:
            store.record_ship_assignment(ship, kind)
        except Exception:  # noqa - bookkeeping must never block a start
            pass
        self.log(f"{ship} {kind} bot started")
        self._push_state()

    def stop_bot(self, ship: str) -> None:
        bot = self.bots.pop(ship, None)
        if bot:
            bot.stop()
            self.log(f"{ship} bot stopped")
        self._push_state()

    # -- orchestrator ------------------------------------------------------
    def start_orch(self, opts: dict) -> None:
        if self.orchestrator and self.orchestrator.running:
            return
        self.orchestrator = Orchestrator(
            self.c,
            credit_buffer=int(opts.get("credit_buffer") or 100000),
            expand_ship_type=opts.get("expand") or None,
            max_ships=int(opts["max_ships"]) if opts.get("max_ships") else None,
            cross_system=bool(opts.get("cross_system")),
            auto_contracts=bool(opts.get("auto_contracts")),
            goal=opts.get("goal") or "grow",
            construct_waypoint=opts.get("construct_waypoint") or None,
            world=self.world,
            on_log=lambda m: self.log(m),
        )
        self.orchestrator.start()
        self.log("orchestrator started")
        self._push_state()

    def stop_orch(self) -> None:
        if self.orchestrator:
            self.orchestrator.stop()
            self.log("orchestrator stopped")
        self._push_state()

    # -- one-shot actions --------------------------------------------------
    def fleet_action(self, ship: str, kind: str, waypoint: str = "") -> dict:
        try:
            if kind == "orbit":
                self.c.orbit(ship)
            elif kind == "dock":
                self.c.dock(ship)
            elif kind == "refuel":
                self.c.refuel(ship)
            elif kind == "extract":
                self.c.orbit(ship)
                data = self.c.extract(ship)
                y = data.get("extraction", {}).get("yield", {})
                self.log(f"{ship} +{y.get('units', 0)} {y.get('symbol', '')}")
            elif kind == "sell":
                for it in self.c.cargo(ship).get("inventory", []):
                    d = self.c.sell(ship, it["symbol"], it["units"])
                    t = d.get("transaction", {})
                    self.log(f"{ship} sold {t.get('units')} {t.get('symbol')} = {t.get('totalPrice')}c")
            elif kind == "navigate":
                if not waypoint:
                    return {"ok": False, "error": "navigate needs a waypoint"}
                self.c.navigate(ship, waypoint)
                self.log(f"{ship} → {waypoint}")
            else:
                return {"ok": False, "error": f"unknown action {kind}"}
        except ApiError as e:
            self.log(f"{ship} {kind} failed: {e.message}")
            return {"ok": False, "error": e.message}
        self.refresh()
        return {"ok": True}

    def accept_contract(self, cid: str) -> dict:
        try:
            self.c.accept_contract(cid)
            self.log(f"contract {cid[:8]} accepted")
        except ApiError as e:
            return {"ok": False, "error": e.message}
        self.refresh()
        return {"ok": True}
