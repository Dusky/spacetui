import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbitrage import arbitrage_scan


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
