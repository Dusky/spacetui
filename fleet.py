"""Autonomous fleet growth.

``plan_expansion`` is a pure decision function (unit-tested); ``FleetManager``
drives it against the live API, buying ships while credits and the fleet cap
allow. Kept deliberately explicit — it spends in-game credits, so it only runs
when invoked and always keeps a configurable credit buffer in reserve.
"""

from __future__ import annotations

import threading

import config
from api import ApiError, Client


def plan_expansion(
    credits: int,
    ship_count: int,
    *,
    unit_price: int,
    credit_buffer: int = 0,
    max_ships: int | None = None,
) -> int:
    """How many ships we can afford to buy right now without dipping below the
    reserve, respecting an optional fleet-size cap."""
    if not unit_price or unit_price <= 0:
        return 0
    if max_ships is not None and ship_count >= max_ships:
        return 0
    spendable = credits - credit_buffer
    if spendable < unit_price:
        return 0
    by_budget = spendable // unit_price
    if max_ships is not None:
        by_budget = min(by_budget, max_ships - ship_count)
    return max(0, int(by_budget))


def ship_type_role(ship_type: str) -> str:
    """Map a purchasable ship type to the role it fills (mirrors
    ``orchestrator.classify_ship`` but off the type name, pre-purchase)."""
    t = (ship_type or "").upper()
    if "PROBE" in t or "SATELLITE" in t:
        return "scout"
    if "MINING" in t or "DRONE" in t or "EXTRACTOR" in t or "SIPHON" in t:
        return "miner"
    return "trader"  # haulers, shuttles, freighters, command frigates


def pick_expansion_type(
    roster: dict[str, str],
    ship_types: list[dict],
    *,
    haulers_per_miner: float = 0.5,
    want_scout: bool = True,
) -> str | None:
    """Choose which ship type to buy next to relieve the fleet's bottleneck.

    ``roster`` is ship→role; ``ship_types`` is ``[{"type","price"}]`` the local
    shipyards sell. The heuristic keeps miners fed by haulers and the price
    store fresh with a scout:

    - no scout yet and one is for sale → buy the scout (cheap eyes first);
    - too few haulers for the miners we have → buy a trader;
    - otherwise raise raw throughput with a miner;
    - fall back to whatever role has an offer.

    Returns the cheapest ship type for the chosen role, or ``None`` if the
    shipyards sell nothing useful.
    """
    by_role: dict[str, list[dict]] = {}
    for st in ship_types:
        role = ship_type_role(st.get("type", ""))
        by_role.setdefault(role, []).append(st)

    def cheapest(role: str) -> str | None:
        offers = by_role.get(role)
        if not offers:
            return None
        offers = sorted(offers, key=lambda s: (s.get("price") is None, s.get("price") or 0))
        return offers[0].get("type")

    counts = {"miner": 0, "trader": 0, "scout": 0}
    for role in roster.values():
        if role in counts:
            counts[role] += 1

    order: list[str] = []
    if want_scout and counts["scout"] == 0:
        order.append("scout")
    if counts["trader"] < counts["miner"] * haulers_per_miner:
        order.append("trader")
    order += ["miner", "trader", "scout"]

    for role in order:
        pick = cheapest(role)
        if pick:
            return pick
    return None


def _system_of(waypoint: str) -> str:
    parts = waypoint.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else waypoint


def find_offer(
    client: Client,
    system: str,
    ship_type: str,
    waypoint: str | None = None,
) -> tuple[str | None, int | None]:
    """Locate a shipyard selling ``ship_type``.

    Returns ``(waypoint, price)``. ``price`` is ``None`` when no ship is present
    at the yard (the API only exposes live prices then), but the waypoint is
    still returned as a fallback so the purchase can be attempted.
    """
    candidates = (
        [waypoint]
        if waypoint
        else [w["symbol"] for w in client.waypoints(system, filters={"traits": "SHIPYARD"})]
    )
    fallback: str | None = None
    for wp in candidates:
        if not wp:
            continue
        try:
            yard = client.shipyard(_system_of(wp), wp)
        except ApiError:
            continue
        for offer in yard.get("ships", []):  # live listings incl. price
            if offer.get("type") == ship_type:
                return wp, offer.get("purchasePrice")
        types = {t.get("type") if isinstance(t, dict) else t for t in yard.get("shipTypes", [])}
        if ship_type in types and fallback is None:
            fallback = wp
    return fallback, None


class FleetManager:
    """Buys ``ship_type`` while budget and fleet cap allow, then stops.

    ``on_buy(symbol)`` fires per purchase so a caller can auto-assign a bot to
    the new ship. Cancellable via ``stop()`` for TUI use.
    """

    def __init__(
        self,
        client: Client,
        ship_type: str,
        *,
        system: str | None = None,
        waypoint: str | None = None,
        credit_buffer: int = 50000,
        max_ships: int | None = None,
        max_price: int | None = None,
        loops: int | None = None,
        on_buy=None,
        on_log=None,
        on_status=None,
    ):
        self.c = client
        self.ship_type = ship_type.upper()
        self.system = system or _system_of(config.HQ)
        self.waypoint = waypoint
        self.credit_buffer = credit_buffer
        self.max_ships = max_ships
        self.max_price = max_price
        self.loops = loops
        self.on_buy = on_buy or (lambda sym: None)
        self.on_log = on_log or (lambda m: None)
        self.on_status = on_status or (lambda **k: None)
        self._cancel = threading.Event()

    def stop(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> int:
        self.on_log(
            f"fleet manager engaged: buy {self.ship_type}, keep {self.credit_buffer:,}c reserve"
        )
        bought = 0
        it = 0
        while not self._cancel.is_set():
            if self.loops is not None and it >= self.loops:
                break
            it += 1
            agent = self.c.my_agent()
            credits = agent.get("credits", 0)
            ship_count = agent.get("shipCount", 0)

            wp, price = find_offer(self.c, self.system, self.ship_type, self.waypoint)
            if not wp:
                self.on_log(f"no shipyard sells {self.ship_type} in {self.system}")
                break
            if self.max_price and price and price > self.max_price:
                self.on_log(
                    f"{self.ship_type} @ {price:,}c exceeds --max-price {self.max_price:,}c; stopping"
                )
                break

            # price may be unknown (no ship at the yard); assume affordable and
            # let the API reject if not, but still honour the buffer when known.
            n = plan_expansion(
                credits,
                ship_count,
                unit_price=price if price else 1,
                credit_buffer=self.credit_buffer,
                max_ships=self.max_ships,
            )
            if n <= 0:
                self.on_log(
                    f"holding: {credits:,}c, {ship_count} ships "
                    f"(reserve {self.credit_buffer:,}c / cap {self.max_ships})"
                )
                break

            self.on_status(running=True, last=f"buying {self.ship_type}")
            try:
                data = self.c.purchase_ship(self.ship_type, wp)
            except ApiError as e:
                self.on_log(f"purchase failed: {e.message}")
                break
            new = data.get("ship", {}).get("symbol", "?")
            tx = data.get("transaction", {})
            bought += 1
            self.on_log(
                f"bought {new} ({self.ship_type}) for {tx.get('price', price or 0):,}c at {wp}"
            )
            self.on_buy(new)

        self.on_log(f"fleet manager done — {bought} ship(s) purchased")
        self.on_status(running=False, last=f"{bought} bought")
        return bought
