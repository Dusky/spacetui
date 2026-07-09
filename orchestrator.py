"""Fleet Orchestrator — the self-growing operation.

One controller supervises the whole fleet: it classifies each ship, deploys the
right bot (miner / trader / scout), automatically deploys bots to ships bought
mid-run, and — when enabled — reinvests profit into more ships. Miners feed the
market, scouts keep prices fresh, traders turn price gaps into credits, and the
credits buy more ships. Round and round.
"""

from __future__ import annotations

import threading
import time

import config
import store
import world as world_mod
from api import ApiError, Client
from contracts import ContractManager
from fleet import find_offer, plan_expansion
from routing import system_of
from tui.bots import MinerBot, ScoutBot, TraderBot


def classify_ship(ship: dict) -> str:
    """Pick a role for a ship from its mounts and cargo capacity.

    - a mining/surveyor mount  -> ``miner``
    - otherwise, cargo to haul -> ``trader``
    - otherwise (a bare probe) -> ``scout``
    """
    mounts = {m.get("symbol", "") for m in ship.get("mounts", [])}
    if any("MINING_LASER" in m or "SURVEYOR" in m for m in mounts):
        return "miner"
    if ship.get("cargo", {}).get("capacity", 0) > 0:
        return "trader"
    return "scout"


class Orchestrator:
    """Supervises the fleet in a background thread. Cancellable."""

    def __init__(
        self,
        client: Client,
        *,
        credit_buffer: int = 100000,
        expand_ship_type: str | None = None,
        max_ships: int | None = None,
        cross_system: bool = False,
        auto_contracts: bool = False,
        tick: int = 20,
        world=None,
        on_log=None,
        on_deploy=None,
        spawn=None,
    ):
        self.c = client
        self.credit_buffer = credit_buffer
        self.expand_ship_type = expand_ship_type.upper() if expand_ship_type else None
        self.max_ships = max_ships
        self.cross_system = cross_system
        self.auto_contracts = auto_contracts
        self.tick = tick
        # shared world model so every bot this controller deploys hits one cache
        self.world = world if world is not None else world_mod.WORLD
        self._contract_mgr: ContractManager | None = None
        self.on_log = on_log or (lambda m: None)
        self.on_deploy = on_deploy or (lambda sym, role: None)
        # how a bot is put to work; overridable for tests
        self._spawn = spawn or self._default_spawn
        self.bots: dict[str, object] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancel = threading.Event()
        self._sup: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._sup and self._sup.is_alive():
            return
        self._cancel.clear()
        self._sup = threading.Thread(target=self._run, daemon=True, name="orchestrator")
        self._sup.start()

    def stop(self) -> None:
        self._cancel.set()
        if self._contract_mgr:
            self._contract_mgr.stop()
        for bot in list(self.bots.values()):
            bot.stop()

    @property
    def running(self) -> bool:
        return bool(self._sup and self._sup.is_alive())

    def roster(self) -> dict[str, str]:
        """Current ship -> role assignments (live bots only)."""
        return {sym: getattr(b, "_role", "?") for sym, b in self.bots.items()}

    # -- internals ---------------------------------------------------------
    def _make_bot(self, ship_symbol: str, role: str):
        def log(msg, r=role):
            self.on_log(f"[{r}] {msg}")

        if role == "miner":
            bot = MinerBot(
                self.c, ship_symbol, world=self.world, on_log=log,
                get_contract=lambda: self._contract_mgr.active_contract_id if self._contract_mgr else None,
            )
        elif role == "trader":
            bot = TraderBot(self.c, ship_symbol, world=self.world,
                            cross_system=self.cross_system, on_log=log)
        else:
            bot = ScoutBot(self.c, ship_symbol, world=self.world,
                           cross_system=self.cross_system, on_log=log)
        bot._role = role
        return bot

    def _default_spawn(self, bot) -> None:
        t = threading.Thread(target=bot.run, daemon=True, name=f"orch-{bot.ship}")
        self._threads[bot.ship] = t
        t.start()

    def _start_contract_mgr(self, ship: str) -> None:
        self._contract_mgr = ContractManager(
            self.c, ship, on_log=lambda m: self.on_log(f"[contract] {m}"))
        threading.Thread(target=self._contract_mgr.run, daemon=True, name="orch-contracts").start()
        self.on_log(f"contract manager engaged on {ship}")

    def _reap_dead(self) -> None:
        """Redeploy bots whose thread died (a bot that halted on some error).

        Forgetting the bot here lets the next supervisor tick re-deploy a fresh
        one, so a single failure never permanently sidelines a ship.
        """
        for sym in list(self.bots):
            t = self._threads.get(sym)
            bot = self.bots[sym]
            if t is not None and not t.is_alive() and not bot.cancelled:
                self.on_log(f"{sym} bot stopped unexpectedly; will redeploy")
                self.bots.pop(sym, None)
                self._threads.pop(sym, None)

    def _deploy(self, ship: dict) -> None:
        sym = ship["symbol"]
        if sym in self.bots:
            return
        role = classify_ship(ship)
        bot = self._make_bot(sym, role)
        self.bots[sym] = bot
        self._spawn(bot)
        try:
            store.record_ship_assignment(sym, role)
        except Exception:  # noqa - bookkeeping must never block a deploy
            pass
        self.on_log(f"deployed {role} → {sym}")
        self.on_deploy(sym, role)

    def _reap(self, ship_symbols: set[str]) -> None:
        """Drop bookkeeping for ships that vanished (scrapped/sold)."""
        for sym in list(self.bots):
            if sym not in ship_symbols:
                self.bots[sym].stop()
                self.bots.pop(sym, None)
                self._threads.pop(sym, None)

    def _maybe_expand(self, ship_count: int) -> None:
        if not self.expand_ship_type:
            return
        try:
            credits = self.c.my_agent().get("credits", 0)
            system = system_of(config.HQ)
            wp, price = find_offer(self.c, system, self.expand_ship_type)
            if not wp:
                return
            n = plan_expansion(
                credits, ship_count, unit_price=price if price else 1,
                credit_buffer=self.credit_buffer, max_ships=self.max_ships,
            )
            if n <= 0:
                return
            data = self.c.purchase_ship(self.expand_ship_type, wp)
            new = data.get("ship", {}).get("symbol", "?")
            self.on_log(f"reinvested → bought {new} ({self.expand_ship_type})")
        except ApiError as e:
            self.on_log(f"expand failed: {e.message}")

    def _sleep(self, secs: int) -> None:
        end = time.time() + secs
        while time.time() < end and not self._cancel.is_set():
            time.sleep(min(0.5, max(0.0, end - time.time())))

    def _run(self) -> None:
        self.on_log("orchestrator online")
        try:
            while not self._cancel.is_set():
                try:
                    ships = self.c.ships()
                    symbols = {s["symbol"] for s in ships}
                    self._reap(symbols)
                    self._reap_dead()
                    if self.auto_contracts and self._contract_mgr is None and ships:
                        self._start_contract_mgr(ships[0]["symbol"])
                    for ship in ships:
                        self._deploy(ship)
                    self._maybe_expand(len(ships))
                except Exception as e:  # keep supervising despite transient errors
                    self.on_log(f"supervisor error: {e!r}")
                self._sleep(self.tick)
        finally:
            self.on_log("orchestrator offline")
