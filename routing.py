"""Jump-gate network graph and pathfinding (pure, no I/O).

The universe's systems are connected by jump gates. Each JumpGate payload lists
``connections`` — the gate *waypoint* symbols it links to — and every gate lives
in exactly one system (the system prefix of its waypoint symbol). We model the
network as a directed graph of systems and BFS over it for routes.
"""

from __future__ import annotations

from collections import deque


def system_of(waypoint: str) -> str:
    parts = waypoint.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else waypoint


def build_graph(edges) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Build ``(adjacency, gate_of)`` from jump edges.

    ``edges`` is an iterable of mappings with ``from_system``/``from_gate`` and
    ``to_system``/``to_gate``. ``adjacency[a]`` is the set of systems reachable
    in one jump from ``a``; ``gate_of[s]`` is system ``s``'s own gate waypoint
    (the waypoint you must be at to jump out, and that neighbours jump *into*).
    """
    adj: dict[str, set[str]] = {}
    gate_of: dict[str, str] = {}
    for e in edges:
        fs, ts = e["from_system"], e["to_system"]
        adj.setdefault(fs, set()).add(ts)
        adj.setdefault(ts, set())
        if e.get("from_gate"):
            gate_of[fs] = e["from_gate"]
        if e.get("to_gate"):
            gate_of[ts] = e["to_gate"]
    return adj, gate_of


def bfs_dist(adj: dict[str, set[str]], start: str) -> dict[str, int]:
    """Hops from ``start`` to every reachable system (``start`` -> 0)."""
    dist = {start: 0}
    q = deque([start])
    while q:
        node = q.popleft()
        for nxt in adj.get(node, ()):  # noqa
            if nxt not in dist:
                dist[nxt] = dist[node] + 1
                q.append(nxt)
    return dist


def shortest_path(adj: dict[str, set[str]], start: str, goal: str) -> list[str] | None:
    """Fewest-hops system path ``[start, ..., goal]``, or None if unreachable."""
    if start == goal:
        return [start]
    prev = {start: None}
    q = deque([start])
    while q:
        node = q.popleft()
        for nxt in sorted(adj.get(node, ())):  # sorted -> deterministic
            if nxt not in prev:
                prev[nxt] = node
                if nxt == goal:
                    path = [goal]
                    while prev[path[-1]] is not None:
                        path.append(prev[path[-1]])
                    return list(reversed(path))
                q.append(nxt)
    return None
