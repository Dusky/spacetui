import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store


def market(waypoint, goods):
    return {"symbol": waypoint, "tradeGoods": goods}


def good(symbol, buy, sell, activity="STRONG"):
    return {"symbol": symbol, "type": "EXCHANGE", "purchasePrice": buy,
            "sellPrice": sell, "tradeVolume": 100, "activity": activity}


def test_price_history_appends_over_time():
    conn = store.connect(":memory:")
    store.record_market(market("X1-A-1", [good("IRON", 100, 90)]), conn=conn)
    store.record_market(market("X1-A-1", [good("IRON", 120, 110)]), conn=conn)
    series = store.price_series("IRON", conn=conn)
    assert len(series) == 2                          # history, not overwrite
    assert [r["sell_price"] for r in series] == [90, 110]


def test_record_trade_and_pnl():
    conn = store.connect(":memory:")
    store.record_trade("buy", "IRON", 40, 100, 4000, ship="S", waypoint="X1-A-1", conn=conn)
    store.record_trade("sell", "IRON", 40, 300, 12000, ship="S", waypoint="X1-B-1", conn=conn)
    store.record_trade("sell", "GOLD", 10, 50, 500, ship="S", waypoint="X1-A-1", conn=conn)
    summary = store.pnl_summary(conn=conn)
    assert summary == {"spent": 4000, "earned": 12500, "net": 8500, "trades": 3}
    by = store.pnl_by_good(conn=conn)
    assert by[0]["symbol"] == "IRON" and by[0]["net"] == 8000
    assert {r["symbol"] for r in by} == {"IRON", "GOLD"}


def test_credit_history_dedupes_unchanged():
    conn = store.connect(":memory:")
    assert store.record_credits(1000, 1, conn=conn) is True
    # same balance moments later -> skipped
    assert store.record_credits(1000, 1, conn=conn) is False
    # changed balance -> recorded
    assert store.record_credits(1500, 1, conn=conn) is True
    series = store.credit_series(conn=conn)
    assert [r["credits"] for r in series] == [1000, 1500]


def test_tracked_goods_and_activity():
    conn = store.connect(":memory:")
    store.record_market(market("X1-A-1", [good("IRON", 100, 90, "STRONG"),
                                          good("FUEL", 50, 45, "WEAK")]), conn=conn)
    store.record_market(market("X1-A-1", [good("IRON", 110, 100, "STRONG")]), conn=conn)
    assert store.tracked_goods(conn=conn)[0] == "IRON"   # most observations
    breakdown = store.activity_breakdown(conn=conn)
    assert breakdown.get("STRONG", 0) >= 1
