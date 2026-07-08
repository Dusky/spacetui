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
        routes.append(
            {
                "good": good,
                "system": sys_sym,
                "buy_wp": buy["waypoint"],
                "sell_wp": sell["waypoint"],
                "buy": buy["purchase_price"],
                "sell": sell["sell_price"],
                "profit": profit,
                "volume": volume,
            }
        )

    routes.sort(key=lambda r: r["profit"], reverse=True)
    return routes
