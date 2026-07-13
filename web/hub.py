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
import metrics as metrics_mod
import store
import world as world_mod
from api import ApiError, Client, is_invalid_token_error
from orchestrator import Orchestrator, classify_ship
from ratelimit import LIMITER
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
        self._last_alerts: set = set()  # alert msgs already pushed (de-dupe SSE)

    # -- active ships (have a running bot) ---------------------------------
    def _active_symbols(self) -> set[str]:
        active = {sym for sym, b in self.bots.items() if not b.cancelled}
        if self.orchestrator and self.orchestrator.running:
            active |= set(self.orchestrator.roster())
        return active

    # -- KPIs & alerts (mission control) -----------------------------------
    def metrics(self) -> dict:
        with self._lock:
            ships = list(self._ships)
        roi = metrics_mod.roi_per_ship(store.ship_pnl(), store.ship_assignments())
        active = self._active_symbols() & {s.get("symbol") for s in ships}
        return {
            "credits_per_hour": round(metrics_mod.profit_per_hour(store.credit_series(limit=500))),
            "roi": roi[:20],
            "by_role": metrics_mod.role_contribution(roi),
            "utilization": metrics_mod.fleet_utilization(len(ships), len(active)),
            "api": LIMITER.status(),
            "pnl": store.pnl_summary(),
        }

    def alerts(self) -> list[dict]:
        with self._lock:
            ships = list(self._ships)
            contracts = list(self._contracts)
        pph = metrics_mod.profit_per_hour(store.credit_series(limit=500))
        return metrics_mod.derive_alerts(
            ships, self._active_symbols(), pph, contracts,
            orch_running=bool(self.orchestrator and self.orchestrator.running),
            api_blocked_for=LIMITER.status()["blocked_for"],
        )

    def _push_alerts(self) -> None:
        """Publish only alerts we haven't already sent this cycle (avoid spam)."""
        try:
            current = self.alerts()
        except Exception:  # noqa - alerts must never break the poll
            return
        msgs = {a["msg"] for a in current}
        fresh = [a for a in current if a["msg"] not in self._last_alerts]
        self._last_alerts = msgs
        for a in fresh:
            self._publish("alert", a)

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
        except ApiError as e:
            with self._lock:
                self._poll_ok = False
                self._poll_err = str(e)
                self._last_poll = time.strftime("%H:%M:%S")
            if is_invalid_token_error(e):
                # unrecoverable -- polling every few seconds forever just
                # hammers the API with a doomed request. Stop clean; the
                # error stays visible in the cached snapshot for the UI.
                self.log(
                    f"FATAL: agent token is invalid ({e.message}). "
                    "Re-register the agent, update the token, and restart. "
                    "Polling stopped."
                )
                self.shutdown()
            return
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
        self._push_alerts()

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
        # don't let a manual bot fight the orchestrator for the same ship —
        # two controllers race and one issues a nav mid-flight (a [4214] crash)
        if self.orchestrator and self.orchestrator.running and ship in self.orchestrator.roster():
            self.log(f"{ship} is orchestrator-controlled; stop the orchestrator to drive it manually")
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
