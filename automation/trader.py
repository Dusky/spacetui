from __future__ import annotations

from api import ApiError
from market import TradeRoute, best_routes

from .base import BaseBot

# Prices older than this are treated as unknown when planning (seconds).
PRICE_TTL = 30 * 60


class TraderBot(BaseBot):
    """Buy-low/sell-high arbitrage within the ship's current system.

    Plans routes from the shared price ledger; when coverage is thin it tours
    unvisited marketplaces to gather prices (doubling as a scanner).
    """

    name = "trader"

    def __init__(
        self,
        client,
        ship,
        *,
        min_margin: int = 2,
        reserve_credits: int = 5_000,
        **kw,
    ):
        super().__init__(client, ship, **kw)
        self.min_margin = min_margin
        self.reserve_credits = reserve_credits
        self.route: TradeRoute | None = None

    # -- planning ----------------------------------------------------------------
    def _plan(self, system: str) -> TradeRoute | None:
        routes = best_routes(self.db, self.cache, system, min_margin=self.min_margin)
        return routes[0] if routes else None

    def _next_unscanned_market(self, system: str, here: str) -> str | None:
        seen = self.db.coverage(system)
        unseen = [w for w in self.cache.markets(system) if w["symbol"] not in seen]
        wp = self.cache.nearest(system, here, unseen)
        return wp["symbol"] if wp else None

    def _sell_leg(self, s: dict) -> dict:
        """Dump whatever cargo we hold at its best-known market."""
        inv = s.get("cargo", {}).get("inventory", [])
        if not inv:
            return s
        system = s["nav"]["systemSymbol"]
        bulk = max(inv, key=lambda i: i["units"])
        best = self.db.best_sell(system, bulk["symbol"])
        dest = best.waypoint if best else None
        if not dest:
            near = self.cache.nearest_market(system, s["nav"]["waypointSymbol"])
            dest = near["symbol"] if near else None
        if not dest:
            self._log("no market to sell at; idling 30s")
            self._sleep(30)
            return s
        self._status(mode="sell", last=f"→ {dest}")
        s = self.goto(s, dest)
        s = self.dock(s)
        self.record_market_here(s)
        earned = self.sell_inventory(s)
        if earned:
            self._log(f"sell leg complete: +{earned:,}c")
        self.refuel_here(s)
        return s

    # -- main loop ---------------------------------------------------------------
    def loop(self) -> None:
        s = self.await_arrival()
        nav = s.get("nav", {})
        system = nav.get("systemSymbol", "")
        here = nav.get("waypointSymbol", "")

        # 1) holding cargo? sell it first
        if s.get("cargo", {}).get("units", 0) > 0:
            self._sell_leg(s)
            return

        # 2) plan the best route from known prices
        route = self._plan(system)
        if route is None:
            # not enough data — go scan a market we haven't priced yet
            target = self._next_unscanned_market(system, here)
            if not target:
                self._log("no profitable route and all markets scanned; rescanning in 60s")
                self._sleep(60)
                # refresh the nearest market's prices so stale data ages out
                near = self.cache.nearest_market(system, here)
                if near:
                    s = self.goto(s, near["symbol"])
                    s = self.dock(s)
                    self.record_market_here(s)
                    self.refuel_here(s)
                return
            self._log(f"scouting prices @ {target}")
            self._status(mode="scan", last=f"→ {target}")
            s = self.goto(s, target)
            s = self.dock(s)
            self.record_market_here(s)
            self.refuel_here(s)
            return

        self.route = route
        self._log(
            f"route {route.good}: buy @{route.buy_waypoint} {route.buy_price}c → "
            f"sell @{route.sell_waypoint} {route.sell_price}c (+{route.margin}/u)"
        )

        # 3) buy leg
        s = self.goto(s, route.buy_waypoint)
        s = self.dock(s)
        self.record_market_here(s)
        self.refuel_here(s)

        agent = self.c.my_agent()
        credits = agent.get("credits", 0)
        cargo = s.get("cargo", {})
        space = cargo.get("capacity", 0) - cargo.get("units", 0)
        # re-check the live price after recording (it may have moved)
        live = self.db.best_buy(system, route.good)
        price = live.buy if live and live.waypoint == route.buy_waypoint else route.buy_price
        affordable = max(0, (credits - self.reserve_credits) // max(price, 1))
        units = min(space, affordable)
        if units <= 0:
            self._log(
                f"can't afford {route.good} (credits {credits:,}, reserve "
                f"{self.reserve_credits:,}); idling 60s"
            )
            self._sleep(60)
            return
        # markets cap units per transaction at tradeVolume — buy in chunks
        volume = live.volume if live and live.volume else units
        bought = 0
        while bought < units and not self.cancelled:
            chunk = min(units - bought, max(volume, 1))
            try:
                data = self.c.purchase(self.ship, route.good, chunk)
            except ApiError as e:
                self._log(f"buy stopped: {e.message}")
                break
            t = data.get("transaction", {})
            bought += t.get("units", chunk)
            if "cargo" in data:
                s["cargo"] = data["cargo"]
            self._log(
                f"bought {t.get('units', chunk)} {route.good} @ {t.get('pricePerUnit')} "
                f"= {t.get('totalPrice')}c"
            )
        if bought == 0:
            self._sleep(30)
            return

        # 4) sell leg
        s = self.goto(s, route.sell_waypoint)
        s = self.dock(s)
        self.record_market_here(s)
        earned = self.sell_inventory(s)
        self._log(f"round trip done: +{earned:,}c revenue on {route.good}")
        self.refuel_here(s)
