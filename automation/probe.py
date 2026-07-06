from __future__ import annotations

from .base import BaseBot


class ProbeBot(BaseBot):
    """Tours every marketplace in the system, recording prices into the shared
    ledger so traders and contractors can plan. Ideal for satellites/probes."""

    name = "probe"

    def __init__(self, client, ship, *, dwell: int = 90, **kw):
        super().__init__(client, ship, **kw)
        self.dwell = dwell
        self._route: list[str] = []

    def _build_route(self, system: str, here: str) -> list[str]:
        markets = self.cache.markets(system)
        # greedy nearest-neighbour tour starting from the current position
        route: list[str] = []
        pos = here
        remaining = {w["symbol"]: w for w in markets}
        remaining.pop(here, None)
        while remaining:
            nxt = self.cache.nearest(system, pos, list(remaining.values()))
            if not nxt:
                break
            route.append(nxt["symbol"])
            pos = nxt["symbol"]
            remaining.pop(nxt["symbol"], None)
        return route

    def loop(self) -> None:
        s = self.await_arrival()
        nav = s.get("nav", {})
        system = nav.get("systemSymbol", "")
        here = nav.get("waypointSymbol", "")

        # record wherever we are, if it has a market
        s = self.dock(s) if self._at_market(system, here) else s
        self.record_market_here(s)
        self.refuel_here(s)

        if not self._route:
            self._route = self._build_route(system, here)
            if not self._route:
                self._log(f"no marketplaces in {system}; idling {self.dwell}s")
                self._sleep(self.dwell)
                return
            self._log(f"survey tour: {len(self._route)} markets")

        target = self._route.pop(0)
        self._status(mode="scan", last=f"→ {target}")
        self.goto(s, target)

    def _at_market(self, system: str, here: str) -> bool:
        wp = self.cache.waypoint(system, here)
        return bool(wp) and any(t["symbol"] == "MARKETPLACE" for t in wp.get("traits", []))
