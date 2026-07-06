"""Route math and fuel-aware navigation helpers shared by the CLI, TUI and bots."""

from __future__ import annotations

import math
import threading

from api import ApiError, Client

# Flight-mode fuel and travel-time multipliers, per the SpaceTraders docs.
FUEL_MULT = {"CRUISE": 1.0, "BURN": 2.0, "STEALTH": 1.0}
TIME_MULT = {"CRUISE": 25.0, "DRIFT": 250.0, "BURN": 12.5, "STEALTH": 30.0}


def system_of(waypoint: str) -> str:
    """X1-N85-A1 -> X1-N85 (works for any sector prefix, not just X1)."""
    parts = waypoint.split("-")
    return "-".join(parts[:2]) if len(parts) >= 3 else waypoint


def distance(a: dict, b: dict) -> float:
    """Euclidean distance between two waypoint dicts (with x/y keys)."""
    return math.hypot(a.get("x", 0) - b.get("x", 0), a.get("y", 0) - b.get("y", 0))


def fuel_cost(dist: float, mode: str = "CRUISE") -> int:
    if mode == "DRIFT":
        return 1
    return max(1, round(dist * FUEL_MULT.get(mode, 1.0)))


def travel_time(dist: float, speed: int, mode: str = "CRUISE") -> int:
    mult = TIME_MULT.get(mode, 25.0)
    return round(max(1.0, round(dist)) * mult / max(speed, 1) + 15)


class WaypointCache:
    """Caches per-system waypoint lists; waypoints rarely change mid-session."""

    def __init__(self, client: Client):
        self.c = client
        self._systems: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def waypoints(self, system: str) -> list[dict]:
        with self._lock:
            cached = self._systems.get(system)
        if cached is not None:
            return cached
        wps = self.c.waypoints(system)
        with self._lock:
            self._systems[system] = wps
        return wps

    def waypoint(self, system: str, symbol: str) -> dict | None:
        for w in self.waypoints(system):
            if w["symbol"] == symbol:
                return w
        return None

    def invalidate(self, system: str) -> None:
        with self._lock:
            self._systems.pop(system, None)

    def with_trait(self, system: str, trait: str) -> list[dict]:
        return [
            w
            for w in self.waypoints(system)
            if any(t["symbol"] == trait for t in w.get("traits", []))
        ]

    def markets(self, system: str) -> list[dict]:
        return self.with_trait(system, "MARKETPLACE")

    def shipyards(self, system: str) -> list[dict]:
        return self.with_trait(system, "SHIPYARD")

    def asteroids(self, system: str) -> list[dict]:
        rocks = {w["symbol"]: w for w in self.with_trait(system, "MINERAL_DEPOSITS")}
        for w in self.with_trait(system, "COMMON_METAL_DEPOSITS"):
            rocks.setdefault(w["symbol"], w)
        return list(rocks.values())

    def gas_giants(self, system: str) -> list[dict]:
        return [w for w in self.waypoints(system) if w.get("type") == "GAS_GIANT"]

    def nearest(self, system: str, origin_symbol: str, candidates: list[dict]) -> dict | None:
        origin = self.waypoint(system, origin_symbol)
        if not origin or not candidates:
            return candidates[0] if candidates else None
        return min(candidates, key=lambda w: distance(origin, w))

    def nearest_market(self, system: str, origin_symbol: str) -> dict | None:
        return self.nearest(system, origin_symbol, self.markets(system))


class Navigator:
    """Fuel-aware movement for one ship. Refuels opportunistically at the
    nearest marketplace instead of trekking back to HQ."""

    def __init__(self, client: Client, cache: WaypointCache | None = None):
        self.c = client
        self.cache = cache or WaypointCache(client)

    # -- state helpers -------------------------------------------------------
    def _orbit(self, ship: dict) -> dict:
        if ship.get("nav", {}).get("status") != "IN_ORBIT":
            ship["nav"] = self.c.orbit(ship["symbol"])["nav"]
        return ship

    def _dock(self, ship: dict) -> dict:
        if ship.get("nav", {}).get("status") != "DOCKED":
            ship["nav"] = self.c.dock(ship["symbol"])["nav"]
        return ship

    def refuel_if_possible(self, ship: dict, *, threshold: float = 0.999) -> bool:
        """Refuel at the current waypoint if docked-able, fuel below threshold
        and the waypoint has a marketplace. Returns True if fuel was bought."""
        fuel = ship.get("fuel", {})
        cap = fuel.get("capacity", 0)
        if not cap or fuel.get("current", 0) / cap >= threshold:
            return False
        nav = ship.get("nav", {})
        here = self.cache.waypoint(nav.get("systemSymbol", ""), nav.get("waypointSymbol", ""))
        if not here or not any(t["symbol"] == "MARKETPLACE" for t in here.get("traits", [])):
            return False
        try:
            self._dock(ship)
            data = self.c.refuel(ship["symbol"])
            ship["fuel"] = data.get("fuel", ship.get("fuel"))
            return True
        except ApiError:
            return False

    def goto(self, ship: dict, dest_symbol: str) -> dict:
        """Navigate to dest_symbol, topping up fuel first when possible and
        falling back to DRIFT when the tank can't cover the leg.

        Returns the navigate response data (or {} when already there)."""
        nav = ship.get("nav", {})
        if nav.get("waypointSymbol") == dest_symbol and nav.get("status") != "IN_TRANSIT":
            return {}
        system = nav.get("systemSymbol", "")
        origin = self.cache.waypoint(system, nav.get("waypointSymbol", ""))
        dest = self.cache.waypoint(system, dest_symbol)
        fuel = ship.get("fuel", {})
        cap = fuel.get("capacity", 0)

        need = 0
        if origin and dest:
            need = fuel_cost(distance(origin, dest), nav.get("flightMode", "CRUISE"))

        if cap and need and fuel.get("current", 0) < need:
            # top up here if we can, else drift
            self.refuel_if_possible(ship)
            fuel = ship.get("fuel", fuel)

        self._orbit(ship)
        mode = nav.get("flightMode", "CRUISE")
        if cap and need and fuel.get("current", 0) < need and mode != "DRIFT":
            self.c.set_flight_mode(ship["symbol"], "DRIFT")
            mode = "DRIFT"
        try:
            data = self.c.navigate(ship["symbol"], dest_symbol)
        except ApiError as e:
            if e.code == 4203 or "fuel" in e.message.lower():
                self.c.set_flight_mode(ship["symbol"], "DRIFT")
                data = self.c.navigate(ship["symbol"], dest_symbol)
            else:
                raise
        ship["nav"] = data.get("nav", ship.get("nav"))
        ship["fuel"] = data.get("fuel", ship.get("fuel"))
        return data

    def restore_cruise(self, ship: dict) -> None:
        if ship.get("nav", {}).get("flightMode") == "DRIFT":
            try:
                nav = self.c.set_flight_mode(ship["symbol"], "CRUISE")
                ship["nav"] = nav
            except ApiError:
                pass
