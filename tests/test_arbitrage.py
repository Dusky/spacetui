import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbitrage import (
    arbitrage_scan,
    demand_factor,
    estimate_route_rate,
    route_units,
)


def obs(waypoint, symbol, buy, sell, system="X1-A", volume=100):
    return {
        "waypoint": waypoint,
        "system": system,
        "symbol": symbol,
        "purchase_price": buy,
        "sell_price": sell,
        "trade_volume": volume,
    }


def test_finds_best_route_and_profit_math():
    observations = [
        obs("X1-A-1", "IRON", buy=100, sell=90),   # cheap to buy here
        obs("X1-A-2", "IRON", buy=200, sell=180),  # expensive; best place to sell
    ]
    routes = arbitrage_scan(observations, min_profit=1)
    assert len(routes) == 1
    r = routes[0]
    assert r["good"] == "IRON"
    assert r["buy_wp"] == "X1-A-1"
    assert r["sell_wp"] == "X1-A-2"
    assert r["buy"] == 100
    assert r["sell"] == 180
    assert r["profit"] == 80


def test_min_profit_filter():
    observations = [
        obs("X1-A-1", "COPPER", buy=100, sell=95),
        obs("X1-A-2", "COPPER", buy=130, sell=120),  # profit 20
    ]
    assert arbitrage_scan(observations, min_profit=10)  # 20 >= 10 keeps it
    assert arbitrage_scan(observations, min_profit=50) == []  # 20 < 50 drops it


def test_single_market_good_ignored():
    # A good seen at only one waypoint can't form a buy->sell pair.
    observations = [obs("X1-A-1", "GOLD", buy=100, sell=300)]
    assert arbitrage_scan(observations, min_profit=1) == []


def test_ranked_by_profit_descending():
    observations = [
        obs("X1-A-1", "IRON", buy=100, sell=90),
        obs("X1-A-2", "IRON", buy=150, sell=140),   # iron profit 40
        obs("X1-A-1", "FUEL", buy=50, sell=45),
        obs("X1-A-3", "FUEL", buy=400, sell=380),   # fuel profit 330
    ]
    routes = arbitrage_scan(observations, min_profit=1)
    assert [r["good"] for r in routes] == ["FUEL", "IRON"]


def test_system_filter():
    observations = [
        obs("X1-A-1", "IRON", buy=100, sell=90, system="X1-A"),
        obs("X1-A-2", "IRON", buy=200, sell=180, system="X1-A"),
        obs("X1-B-1", "IRON", buy=100, sell=90, system="X1-B"),
        obs("X1-B-2", "IRON", buy=999, sell=990, system="X1-B"),
    ]
    routes = arbitrage_scan(observations, min_profit=1, system="X1-A")
    assert len(routes) == 1
    assert routes[0]["system"] == "X1-A"


def test_same_waypoint_not_a_route():
    # Best buy and best sell at the same waypoint is not a tradeable route.
    observations = [
        obs("X1-A-1", "IRON", buy=100, sell=300),
        obs("X1-A-2", "IRON", buy=500, sell=50),
    ]
    # best buy = X1-A-1 (100), best sell = X1-A-1 (300) -> same wp -> skip
    assert arbitrage_scan(observations, min_profit=1) == []


def test_route_rate_prefers_nearer_at_equal_margin():
    # same per-unit margin and throughput, but one is 3 jumps away
    near = {"profit": 100, "volume": 50, "hops": 0}
    far = {"profit": 100, "volume": 50, "hops": 3}
    assert estimate_route_rate(near) > estimate_route_rate(far)


def test_route_rate_prefers_more_throughput():
    thin = {"profit": 100, "volume": 5, "hops": 0}
    fat = {"profit": 100, "volume": 50, "hops": 0}
    assert estimate_route_rate(fat) > estimate_route_rate(thin)


def test_route_units_bounded_by_cargo_and_supply():
    # tiny hold caps it regardless of a fat market
    assert route_units({"volume": 100, "buy_supply": "ABUNDANT"}, cargo_capacity=10) == 10
    # a scarce sell market caps absorption below the hold
    r = {"volume": 5, "buy_supply": "ABUNDANT", "sell_supply": "SCARCE"}
    assert route_units(r, cargo_capacity=40) == 5


def test_scan_ranks_by_rate_not_raw_margin():
    # WIDGET has a fatter per-unit margin but only sells in tiny volume; GADGET
    # has a slimmer margin at high volume, so it earns more per hour.
    observations = [
        obs("X1-A-1", "WIDGET", buy=100, sell=90, volume=1),
        obs("X1-A-2", "WIDGET", buy=100, sell=400, volume=1),   # +300/u, vol 1
        obs("X1-A-1", "GADGET", buy=100, sell=90, volume=100),
        obs("X1-A-3", "GADGET", buy=100, sell=200, volume=100),  # +100/u, vol 100
    ]
    routes = arbitrage_scan(observations, min_profit=1)
    assert routes[0]["good"] == "GADGET"


def test_demand_factor_eases_on_falling_prices():
    assert demand_factor([100, 100, 100]) == 1.0     # flat -> full size
    assert demand_factor([100, 120]) == 1.0          # rising -> full size
    assert demand_factor([]) == 1.0                  # no data -> full size
    assert demand_factor([100, 80]) == 0.8           # 20% drop -> 0.8x
    assert demand_factor([100, 10]) == 0.25          # crash -> floored at 0.25x
