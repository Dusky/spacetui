"""Pure arbitrage scanning over stored market observations.

Kept free of I/O so it can be unit-tested with synthetic data. ``store.py``
feeds it the latest observation per (waypoint, good); the TraderBot consumes
the ranked routes it returns.

An *observation* is a mapping with at least::

    {"waypoint", "system", "symbol",
     "purchase_price", "sell_price", "trade_volume"}

``purchase_price`` is what you pay to buy a unit at that market; ``sell_price``
is what you receive selling one there.
"""

from __future__ import annotations

from typing import Iterable

# How many trade-volume batches a market can absorb before its price moves too
# far, keyed by supply level. Buying from an ABUNDANT market or selling into a
# SCARCE (high-demand) one sustains more units than a thin market.
_ABSORPTION = {"ABUNDANT": 4, "HIGH": 3, "MODERATE": 2, "LIMITED": 1, "SCARCE": 1}

# Rough trip-time model for ranking routes by *rate* rather than raw margin. A
# short in-system hop still costs the fixed overhead (dock, buy, sell, refuel);
# each jump adds travel + cooldown. These are estimates — the trader sizes and
# times the real trip itself — but they let the scanner prefer a fast small
# margin over a slow fat one.
DEFAULT_CARGO = 40
HOP_SECONDS = 90.0
TRIP_OVERHEAD_SECONDS = 60.0


def sustainable_units(trade_volume, supply, default_mult: int = 2) -> int:
    """Units you can move here in one visit before the price degrades badly."""
    tv = trade_volume or 1
    return int(tv) * _ABSORPTION.get((supply or "").upper(), default_mult)


def route_units(route: dict, cargo_capacity: int = DEFAULT_CARGO) -> int:
    """Units a ship of ``cargo_capacity`` could actually move on this route,
    bounded by both markets' absorption."""
    return max(0, min(
        cargo_capacity,
        sustainable_units(route.get("volume"), route.get("buy_supply")),
        sustainable_units(route.get("volume"), route.get("sell_supply")),
    ))


def estimate_route_rate(
    route: dict,
    *,
    cargo_capacity: int = DEFAULT_CARGO,
    hop_seconds: float = HOP_SECONDS,
    overhead_seconds: float = TRIP_OVERHEAD_SECONDS,
) -> float:
    """Estimated **credits per hour** for one round of this route.

    ``profit_per_unit × units_moved ÷ trip_time``. Trip time is a fixed
    per-trip overhead plus a cost per jump hop, so at equal margin a nearer,
    higher-throughput route outranks a distant, thin one.
    """
    units = route_units(route, cargo_capacity)
    profit = route.get("profit", 0)
    trip = overhead_seconds + hop_seconds * max(0, route.get("hops", 0) or 0)
    if trip <= 0:
        trip = 1.0
    return (profit * units) / trip * 3600.0


def market_listings(market: dict | None) -> set[str]:
    """Good symbols a market trades at all — sells to a market fail unless the
    good is listed. Union of the ``imports``/``exports``/``exchange`` catalogs
    (present even with no ship at the waypoint) and live ``tradeGoods``."""
    if not market:
        return set()
    out: set[str] = set()
    for key in ("imports", "exports", "exchange", "tradeGoods"):
        for g in market.get(key) or []:
            sym = g.get("symbol") if isinstance(g, dict) else g
            if sym:
                out.add(sym)
    return out


def plan_sales(
    inventory: Iterable[dict], markets: dict[str, dict | None]
) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """Assign each cargo good to a market that actually lists it.

    ``inventory`` is ``[{symbol, units}]``; ``markets`` maps waypoint →
    market payload (or None). Returns ``(plan, unsellable)`` where ``plan`` is
    an ordered list of ``(waypoint, [goods])`` stops — greedy fewest-stops:
    each round picks the market covering the most remaining goods, ties broken
    by the summed live ``sellPrice`` of those goods — and ``unsellable`` is
    every good no known market lists.
    """
    remaining = {i.get("symbol") for i in inventory if i.get("symbol")}
    listings = {wp: market_listings(m) for wp, m in markets.items()}
    prices: dict[str, dict[str, int]] = {}
    for wp, m in markets.items():
        prices[wp] = {
            g.get("symbol"): g.get("sellPrice") or 0
            for g in (m or {}).get("tradeGoods") or []
        }

    plan: list[tuple[str, list[str]]] = []
    while remaining:
        best_wp, best_goods = None, set()
        for wp, listed in listings.items():
            goods = remaining & listed
            if not goods:
                continue
            if (len(goods), sum(prices[wp].get(g, 0) for g in goods)) > (
                len(best_goods), sum(prices.get(best_wp, {}).get(g, 0) for g in best_goods)
            ):
                best_wp, best_goods = wp, goods
        if best_wp is None:
            break  # nothing else is listed anywhere
        plan.append((best_wp, sorted(best_goods)))
        remaining -= best_goods
    return plan, sorted(remaining)


def demand_factor(sell_points) -> float:
    """A 0<f≤1 multiplier that shrinks order size when a market is being flooded.

    Given recent sell prices (oldest→newest) for a good at the sell waypoint, a
    falling trend means our own fills are depressing the price, so we ease off;
    a flat or rising trend leaves the size untouched (``1.0``).
    """
    pts = [p for p in sell_points if p]
    if len(pts) < 2:
        return 1.0
    first, last = pts[0], pts[-1]
    if first <= 0:
        return 1.0
    change = (last - first) / first  # negative when the price is sliding
    if change >= 0:
        return 1.0
    return max(0.25, 1.0 + change)


def price_floor(buy_price: int, min_margin: int) -> int:
    """Lowest sell price still worth taking (keeps at least ``min_margin``/unit)."""
    return buy_price + max(0, min_margin)


def price_ceiling(sell_price: int, min_margin: int) -> int:
    """Highest buy price still worth paying (keeps at least ``min_margin``/unit)."""
    return sell_price - max(0, min_margin)


def _route(good, buy_sys, sell_sys, buy, sell, volume, hops, hop_penalty):
    profit = sell["sell_price"] - buy["purchase_price"]
    r = {
        "good": good,
        "system": buy_sys,
        "buy_system": buy_sys,
        "sell_system": sell_sys,
        "buy_wp": buy["waypoint"],
        "sell_wp": sell["waypoint"],
        "buy": buy["purchase_price"],
        "sell": sell["sell_price"],
        "buy_supply": buy.get("supply"),
        "sell_supply": sell.get("supply"),
        "sell_activity": sell.get("activity"),
        "profit": profit,
        "volume": volume,
        "hops": hops,
        "score": profit - hop_penalty * hops,
    }
    r["rate"] = estimate_route_rate(r)  # est. credits/hour, for ranking
    return r


def arbitrage_scan(
    observations: Iterable[dict],
    *,
    min_profit: int = 1,
    min_volume: int = 1,
    system: str | None = None,
) -> list[dict]:
    """Rank same-system buy-low/sell-high routes, best margin first.

    For each good, buy where ``purchase_price`` is lowest and sell where
    ``sell_price`` is highest (a different waypoint). Routes with the buy and
    sell at the same waypoint, or profit/volume below the thresholds, are
    dropped.
    """
    # Group usable observations by (system, good).
    by_good: dict[tuple[str, str], list[dict]] = {}
    for o in observations:
        if system is not None and o.get("system") != system:
            continue
        buy = o.get("purchase_price")
        sell = o.get("sell_price")
        if buy is None and sell is None:
            continue
        key = (o.get("system", ""), o.get("symbol", ""))
        by_good.setdefault(key, []).append(o)

    routes: list[dict] = []
    for (sys_sym, good), obs in by_good.items():
        buys = [o for o in obs if o.get("purchase_price")]
        sells = [o for o in obs if o.get("sell_price")]
        if not buys or not sells:
            continue
        buy = min(buys, key=lambda o: o["purchase_price"])
        sell = max(sells, key=lambda o: o["sell_price"])
        if buy["waypoint"] == sell["waypoint"]:
            continue
        profit = sell["sell_price"] - buy["purchase_price"]
        volume = min(
            buy.get("trade_volume") or 1,
            sell.get("trade_volume") or 1,
        )
        if profit < min_profit or volume < min_volume:
            continue
        routes.append(_route(good, sys_sym, sys_sym, buy, sell, volume, 0, 0))

    # rank by estimated credits/hour; profit breaks ties (same-system => same trip)
    routes.sort(key=lambda r: (r["rate"], r["profit"]), reverse=True)
    return routes


def cross_system_scan(
    observations: Iterable[dict],
    adjacency: dict,
    *,
    current_system: str,
    min_profit: int = 1,
    min_volume: int = 1,
    max_hops: int = 2,
    hop_penalty: int = 0,
) -> list[dict]:
    """Rank buy-low/sell-high routes across the jump-gate network.

    Considers markets in systems within ``max_hops`` jumps of ``current_system``.
    For each good it greedily pairs the cheapest reachable buy with the dearest
    reachable sell (different waypoints), and scores the route as
    ``profit - hop_penalty * hops``, where ``hops`` is the total travel
    ``current -> buy_system -> sell_system``. Routes are ranked by score, then by
    fewest hops.
    """
    from routing import bfs_dist

    dist_from_current = bfs_dist(adjacency, current_system)
    in_range = {s: h for s, h in dist_from_current.items() if h <= max_hops}

    # cache BFS from candidate buy systems (for the buy->sell leg)
    _bfs_cache: dict[str, dict[str, int]] = {}

    def dist_between(a: str, b: str) -> int | None:
        if a not in _bfs_cache:
            _bfs_cache[a] = bfs_dist(adjacency, a)
        return _bfs_cache[a].get(b)

    by_good: dict[str, list[dict]] = {}
    for o in observations:
        if o.get("system") not in in_range:
            continue
        if o.get("purchase_price") is None and o.get("sell_price") is None:
            continue
        by_good.setdefault(o.get("symbol", ""), []).append(o)

    routes: list[dict] = []
    for good, obs in by_good.items():
        buys = [o for o in obs if o.get("purchase_price")]
        sells = [o for o in obs if o.get("sell_price")]
        if not buys or not sells:
            continue
        buy = min(buys, key=lambda o: o["purchase_price"])
        sell = max(sells, key=lambda o: o["sell_price"])
        if buy["waypoint"] == sell["waypoint"]:
            continue
        profit = sell["sell_price"] - buy["purchase_price"]
        if profit < min_profit:
            continue
        leg = dist_between(buy["system"], sell["system"])
        if leg is None:
            continue  # can't reach the sell system from the buy system
        hops = in_range[buy["system"]] + leg
        volume = min(buy.get("trade_volume") or 1, sell.get("trade_volume") or 1)
        if volume < min_volume:
            continue
        routes.append(_route(good, buy["system"], sell["system"], buy, sell, volume, hops, hop_penalty))

    # rank by estimated credits/hour (which already discounts distant hops),
    # then by fewest hops, then by the penalty-adjusted score
    routes.sort(key=lambda r: (-r["rate"], r["hops"], -r["score"]))
    return routes
