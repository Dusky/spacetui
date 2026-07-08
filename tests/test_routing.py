import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routing import bfs_dist, build_graph, shortest_path, system_of


def edge(fs, fg, ts, tg):
    return {"from_system": fs, "from_gate": fg, "to_system": ts, "to_gate": tg}


# A -> B -> C  (linear), plus A -> D
EDGES = [
    edge("X1-A", "X1-A-GATE", "X1-B", "X1-B-GATE"),
    edge("X1-B", "X1-B-GATE", "X1-C", "X1-C-GATE"),
    edge("X1-A", "X1-A-GATE", "X1-D", "X1-D-GATE"),
]


def test_system_of():
    assert system_of("X1-N85-A1") == "X1-N85"
    assert system_of("X1-A-GATE") == "X1-A"


def test_build_graph_adjacency_and_gates():
    adj, gate_of = build_graph(EDGES)
    assert adj["X1-A"] == {"X1-B", "X1-D"}
    assert adj["X1-B"] == {"X1-C"}
    assert adj["X1-C"] == set()  # leaf still present
    assert gate_of["X1-A"] == "X1-A-GATE"
    assert gate_of["X1-C"] == "X1-C-GATE"  # learned from an incoming edge


def test_bfs_dist():
    adj, _ = build_graph(EDGES)
    dist = bfs_dist(adj, "X1-A")
    assert dist == {"X1-A": 0, "X1-B": 1, "X1-D": 1, "X1-C": 2}


def test_shortest_path():
    adj, _ = build_graph(EDGES)
    assert shortest_path(adj, "X1-A", "X1-C") == ["X1-A", "X1-B", "X1-C"]
    assert shortest_path(adj, "X1-A", "X1-A") == ["X1-A"]
    assert shortest_path(adj, "X1-C", "X1-A") is None  # directed: no back-edge
