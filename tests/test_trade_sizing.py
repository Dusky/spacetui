import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbitrage import arbitrage_scan, price_ceiling, price_floor, sustainable_units


def test_sustainable_units_by_supply():
    assert sustainable_units(10, "ABUNDANT") == 40
    assert sustainable_units(10, "HIGH") == 30
    assert sustainable_units(10, "MODERATE") == 20
    assert sustainable_units(10, "LIMITED") == 10
    assert sustainable_units(10, "SCARCE") == 10


def test_sustainable_units_defaults():
    assert sustainable_units(10, None) == 20      # unknown supply -> default mult 2
    assert sustainable_units(None, "HIGH") == 3   # missing volume -> treat as 1


def test_price_floor_and_ceiling():
    assert price_floor(100, 20) == 120     # won't sell below cost + margin
    assert price_ceiling(300, 20) == 280   # won't buy above sell - margin
    assert price_floor(100, -5) == 100     # negative margin clamped to 0


def test_routes_carry_supply_for_sizing():
    obs = [
        {"waypoint": "A", "system": "S", "symbol": "IRON",
         "purchase_price": 100, "sell_price": 90, "trade_volume": 10, "supply": "ABUNDANT"},
        {"waypoint": "B", "system": "S", "symbol": "IRON",
         "purchase_price": 200, "sell_price": 180, "trade_volume": 10, "supply": "SCARCE"},
    ]
    r = arbitrage_scan(obs, min_profit=1)[0]
    assert r["buy_supply"] == "ABUNDANT"
    assert r["sell_supply"] == "SCARCE"
    # a trader would cap at min(cap, buy 40, sell 10) = 10 units this visit
    want = min(100, sustainable_units(r["volume"], r["buy_supply"]),
               sustainable_units(r["volume"], r["sell_supply"]))
    assert want == 10
