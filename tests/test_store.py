import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store


def market(waypoint, goods):
    return {"symbol": waypoint, "tradeGoods": goods}


def good(symbol, buy, sell, typ="EXCHANGE", volume=100):
    return {
        "symbol": symbol,
        "type": typ,
        "purchasePrice": buy,
        "sellPrice": sell,
        "tradeVolume": volume,
    }


def test_record_and_latest_prices():
    conn = store.connect(":memory:")
    n = store.record_market(
        market("X1-A-1", [good("IRON", 100, 90), good("FUEL", 50, 45)]), conn=conn
    )
    assert n == 2
    rows = store.latest_prices(conn=conn)
    assert {r["symbol"] for r in rows} == {"IRON", "FUEL"}
    iron = next(r for r in rows if r["symbol"] == "IRON")
    assert iron["waypoint"] == "X1-A-1"
    assert iron["system"] == "X1-A"
    assert iron["purchase_price"] == 100


def test_catalog_only_market_skipped():
    conn = store.connect(":memory:")
    assert store.record_market({"symbol": "X1-A-9", "imports": ["IRON"]}, conn=conn) == 0
    assert store.latest_prices(conn=conn) == []


def test_upsert_keeps_one_row_per_waypoint_good():
    conn = store.connect(":memory:")
    store.record_market(market("X1-A-1", [good("IRON", 100, 90)]), conn=conn)
    store.record_market(market("X1-A-1", [good("IRON", 120, 110)]), conn=conn)
    rows = [r for r in store.latest_prices(conn=conn) if r["symbol"] == "IRON"]
    assert len(rows) == 1
    assert rows[0]["purchase_price"] == 120  # latest observation wins


def test_best_routes_end_to_end():
    conn = store.connect(":memory:")
    store.record_market(market("X1-A-1", [good("IRON", 100, 90)]), conn=conn)
    store.record_market(market("X1-A-2", [good("IRON", 250, 240)]), conn=conn)
    routes = store.best_routes(system="X1-A", min_profit=50, conn=conn)
    assert len(routes) == 1
    assert routes[0]["profit"] == 140
    assert routes[0]["buy_wp"] == "X1-A-1"
    assert routes[0]["sell_wp"] == "X1-A-2"


def test_record_jump_gate_and_edges():
    conn = store.connect(":memory:")
    n = store.record_jump_gate(
        {"symbol": "X1-A-GATE", "connections": ["X1-B-GATE", "X1-C-GATE"]}, conn=conn
    )
    assert n == 2
    edges = store.jump_edges(conn=conn)
    assert {(e["from_system"], e["to_system"]) for e in edges} == {("X1-A", "X1-B"), ("X1-A", "X1-C")}


def test_best_routes_cross_system():
    conn = store.connect(":memory:")
    store.record_market(market("X1-A-1", [good("IRON", 100, 90)]), conn=conn)
    store.record_market(market("X1-B-1", [good("IRON", 400, 380)]), conn=conn)
    store.record_jump_gate({"symbol": "X1-A-GATE", "connections": ["X1-B-GATE"]}, conn=conn)
    # same-system only: nothing (IRON seen once per system)
    assert store.best_routes(system="X1-A", min_profit=50, conn=conn) == []
    # cross-system: the A->B route surfaces
    routes = store.best_routes(system="X1-A", min_profit=50, max_hops=2, conn=conn)
    assert len(routes) == 1
    assert routes[0]["buy_system"] == "X1-A" and routes[0]["sell_system"] == "X1-B"
    assert routes[0]["hops"] == 1
