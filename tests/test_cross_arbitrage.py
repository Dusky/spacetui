import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbitrage import cross_system_scan
from routing import build_graph


def obs(waypoint, symbol, buy, sell, system, volume=100):
    return {"waypoint": waypoint, "system": system, "symbol": symbol,
            "purchase_price": buy, "sell_price": sell, "trade_volume": volume}


def edge(fs, ts):
    return {"from_system": fs, "from_gate": f"{fs}-G", "to_system": ts, "to_gate": f"{ts}-G"}


# A -> B -> C
ADJ, _ = build_graph([edge("X1-A", "X1-B"), edge("X1-B", "X1-C")])


def test_cross_system_route_found():
    observations = [
        obs("X1-A-1", "IRON", buy=100, sell=90, system="X1-A"),   # buy cheap in A
        obs("X1-B-1", "IRON", buy=400, sell=380, system="X1-B"),  # sell dear in B
    ]
    routes = cross_system_scan(observations, ADJ, current_system="X1-A",
                               min_profit=1, max_hops=2)
    assert len(routes) == 1
    r = routes[0]
    assert r["buy_system"] == "X1-A" and r["sell_system"] == "X1-B"
    assert r["profit"] == 280
    assert r["hops"] == 1  # A(0) -> buy A -> sell B (1 hop)


def test_max_hops_excludes_far_systems():
    observations = [
        obs("X1-A-1", "IRON", buy=100, sell=90, system="X1-A"),
        obs("X1-C-1", "IRON", buy=500, sell=480, system="X1-C"),  # C is 2 hops away
    ]
    # within 1 hop, C is unreachable -> no route
    assert cross_system_scan(observations, ADJ, current_system="X1-A",
                             min_profit=1, max_hops=1) == []
    # within 2 hops, the C route appears
    routes = cross_system_scan(observations, ADJ, current_system="X1-A",
                               min_profit=1, max_hops=2)
    assert len(routes) == 1
    assert routes[0]["sell_system"] == "X1-C"
    assert routes[0]["hops"] == 2


def test_hop_penalty_prefers_closer_route():
    observations = [
        obs("X1-A-1", "IRON", buy=100, sell=90, system="X1-A"),
        obs("X1-B-1", "IRON", buy=300, sell=280, system="X1-B"),  # +180, 1 hop
        obs("X1-A-2", "GOLD", buy=100, sell=90, system="X1-A"),
        obs("X1-C-1", "GOLD", buy=320, sell=300, system="X1-C"),  # +200, 2 hops
    ]
    # With a stiff per-hop penalty, the nearer IRON route should win despite
    # lower raw profit: IRON 180-1*50=130 vs GOLD 200-2*50=100.
    routes = cross_system_scan(observations, ADJ, current_system="X1-A",
                               min_profit=1, max_hops=2, hop_penalty=50)
    assert routes[0]["good"] == "IRON"


def test_reduces_to_same_system_at_zero_hops():
    observations = [
        obs("X1-A-1", "IRON", buy=100, sell=90, system="X1-A"),
        obs("X1-A-2", "IRON", buy=250, sell=240, system="X1-A"),
        obs("X1-B-1", "IRON", buy=999, sell=980, system="X1-B"),
    ]
    routes = cross_system_scan(observations, ADJ, current_system="X1-A",
                               min_profit=1, max_hops=0)
    assert len(routes) == 1
    assert routes[0]["sell_system"] == "X1-A"  # only same-system pair considered
