from __future__ import annotations

import threading
import time

from api import ApiError, Client
from market import MarketDB
from navigation import WaypointCache

from .base import BaseBot
from .contractor import ContractBot
from .miner import MinerBot
from .probe import ProbeBot
from .trader import TraderBot

ROLE_BOTS: dict[str, type[BaseBot]] = {
    "EXCAVATOR": MinerBot,
    "HAULER": TraderBot,
    "TRANSPORT": TraderBot,
    "SATELLITE": ProbeBot,
    "COMMAND": ContractBot,
}


def default_bot_for(ship: dict) -> type[BaseBot]:
    """Pick a sensible bot class from the ship's registration role."""
    role = ship.get("registration", {}).get("role", "")
    cls = ROLE_BOTS.get(role)
    if cls:
        return cls
    # anything with cargo space can mine; probes and drones without cargo scan
    if ship.get("cargo", {}).get("capacity", 0) > 0:
        return MinerBot
    return ProbeBot


class FleetCommander:
    """Assigns a bot to every ship by role, restarts crashed bots, and can
    optionally reinvest profits into new mining drones."""

    def __init__(
        self,
        client: Client,
        *,
        autobuy: str | None = None,
        autobuy_reserve: int = 50_000,
        max_ships: int = 10,
        overrides: dict[str, type[BaseBot]] | None = None,
        on_log=None,
    ):
        self.c = client
        self.cache = WaypointCache(client)
        self.db = MarketDB()
        self.autobuy = autobuy  # e.g. "SHIP_MINING_DRONE"
        self.autobuy_reserve = autobuy_reserve
        self.max_ships = max_ships
        self.overrides = overrides or {}
        self.on_log = on_log or print
        self.bots: dict[str, BaseBot] = {}
        self.threads: dict[str, threading.Thread] = {}
        self._cancel = threading.Event()

    def _log(self, msg: str) -> None:
        self.on_log(f"fleet  {msg}")

    def stop(self) -> None:
        self._cancel.set()
        for bot in self.bots.values():
            bot.stop()

    # -- bot management ---------------------------------------------------------
    def _spawn(self, ship: dict) -> None:
        sym = ship["symbol"]
        cls = self.overrides.get(sym) or default_bot_for(ship)
        bot = cls(
            self.c,
            sym,
            cache=self.cache,
            db=self.db,
            on_log=self.on_log,
        )
        self.bots[sym] = bot
        t = threading.Thread(target=bot.run, name=f"bot-{sym}", daemon=True)
        self.threads[sym] = t
        t.start()
        self._log(f"{sym} → {bot.name}")

    def _reap_and_respawn(self, ships: list[dict]) -> None:
        for ship in ships:
            sym = ship["symbol"]
            t = self.threads.get(sym)
            if sym not in self.bots or (t is not None and not t.is_alive()):
                if self._cancel.is_set():
                    return
                if t is not None and not t.is_alive():
                    self._log(f"{sym} bot stopped; restarting in 10s")
                    time.sleep(10)
                self._spawn(ship)

    # -- reinvestment ---------------------------------------------------------
    def _maybe_buy_ship(self, ships: list[dict]) -> None:
        if not self.autobuy or len(ships) >= self.max_ships:
            return
        try:
            agent = self.c.my_agent()
        except ApiError:
            return
        credits = agent.get("credits", 0)
        if credits < self.autobuy_reserve:
            return
        system = ships[0]["nav"]["systemSymbol"] if ships else ""
        if not system:
            return
        for yard in self.cache.shipyards(system):
            try:
                listing = self.c.shipyard(system, yard["symbol"])
            except ApiError:
                continue
            for offer in listing.get("ships", []) or []:
                if offer.get("type") != self.autobuy:
                    continue
                price = offer.get("purchasePrice", 0)
                if credits - price < self.autobuy_reserve // 2:
                    return
                try:
                    data = self.c.purchase_ship(self.autobuy, yard["symbol"])
                    new = data.get("ship", {}).get("symbol", "?")
                    self._log(f"purchased {self.autobuy} → {new} for {price:,}c")
                except ApiError as e:
                    self._log(f"ship purchase failed: {e.message}")
                return
        # type not sold in this system: only try once per run
        self._log(f"{self.autobuy} not sold at any shipyard in {system}; autobuy off")
        self.autobuy = None

    # -- main loop ---------------------------------------------------------------
    def run(self) -> None:
        self._log("commander online")
        try:
            while not self._cancel.is_set():
                try:
                    ships = self.c.ships()
                except ApiError as e:
                    self._log(f"can't list ships: {e.message}")
                    time.sleep(15)
                    continue
                self._reap_and_respawn(ships)
                self._maybe_buy_ship(ships)
                for _ in range(60):
                    if self._cancel.is_set():
                        break
                    time.sleep(1)
        finally:
            self.stop()
            self._log("commander offline")
