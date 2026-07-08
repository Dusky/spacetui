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


def sustainable_units(trade_volume, supply, default_mult: int = 2) -> int:
    """Units you can move here in one visit before the price degrades badly."""
    tv = trade_volume or 1
    return int(tv) * _ABSORPTION.get((supply or "").upper(), default_mult)


def price_floor(buy_price: int, min_margin: int) -> int:
    """Lowest sell price still worth taking (keeps at least ``min_margin``/unit)."""
    return buy_price + max(0, min_margin)


def price_ceiling(sell_price: int, min_margin: int) -> int:
    """Highest buy price still worth paying (keeps at least ``min_margin``/unit)."""
    return sell_price - max(0, min_margin)


def _route(good, buy_sys, sell_sys, buy, sell, volume, hops, hop_penalty):
    profit = sell["sell_price"] - buy["purchase_price"]
    return {
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

    routes.sort(key=lambda r: r["profit"], reverse=True)
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

    routes.sort(key=lambda r: (-r["score"], r["hops"]))
    return routes
