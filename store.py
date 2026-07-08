"""SQLite persistence for market observations.

The v2 API only returns live prices (``tradeGoods`` with buy/sell) for markets
a ship is *currently present at*. To do arbitrage we therefore accumulate every
market we see into a local DB and scan the latest snapshot.

Stdlib ``sqlite3`` only; the connection is shared across bot threads
(``check_same_thread=False``) and guarded by a module lock, since writes come
from multiple worker threads.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

from arbitrage import arbitrage_scan, cross_system_scan
from routing import build_graph, system_of as _sys_of

DB_PATH = os.environ.get(
    "ST_DB_PATH", str(Path(__file__).resolve().parent / "spacetui.db")
)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_observations (
    waypoint       TEXT NOT NULL,
    system         TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    type           TEXT,
    purchase_price INTEGER,
    sell_price     INTEGER,
    trade_volume   INTEGER,
    supply         TEXT,
    activity       TEXT,
    observed_at    REAL NOT NULL,
    PRIMARY KEY (waypoint, symbol)
);

CREATE TABLE IF NOT EXISTS jump_edges (
    from_system TEXT NOT NULL,
    from_gate   TEXT NOT NULL,
    to_system   TEXT NOT NULL,
    to_gate     TEXT NOT NULL,
    PRIMARY KEY (from_gate, to_gate)
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    """Return the shared connection, creating the schema on first use.

    Pass ``path`` (e.g. ``":memory:"``) to open a distinct connection; used by
    tests. With no argument the process-wide connection at ``DB_PATH`` is used.
    """
    global _conn
    if path is not None:
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        return conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(_SCHEMA)
        return _conn


def _system_of(waypoint: str) -> str:
    # X1-N85-A1 -> X1-N85
    parts = waypoint.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else waypoint


def record_market(market: dict, conn: sqlite3.Connection | None = None) -> int:
    """Upsert one row per live trade good from a ``Client.market()`` payload.

    Catalog-only markets (no ``tradeGoods``, i.e. no ship present) carry no
    prices and are skipped. Returns the number of goods recorded.
    """
    goods = market.get("tradeGoods") or []
    if not goods:
        return 0
    waypoint = market.get("symbol", "")
    system = _system_of(waypoint)
    now = time.time()
    rows = [
        (
            waypoint,
            system,
            g.get("symbol", ""),
            g.get("type"),
            g.get("purchasePrice"),
            g.get("sellPrice"),
            g.get("tradeVolume"),
            g.get("supply"),
            g.get("activity"),
            now,
        )
        for g in goods
    ]
    c = conn or connect()
    with _lock:
        c.executemany(
            """
            INSERT INTO market_observations
                (waypoint, system, symbol, type, purchase_price, sell_price,
                 trade_volume, supply, activity, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(waypoint, symbol) DO UPDATE SET
                system=excluded.system,
                type=excluded.type,
                purchase_price=excluded.purchase_price,
                sell_price=excluded.sell_price,
                trade_volume=excluded.trade_volume,
                supply=excluded.supply,
                activity=excluded.activity,
                observed_at=excluded.observed_at
            """,
            rows,
        )
        c.commit()
    return len(rows)


def record_jump_gate(gate: dict, conn: sqlite3.Connection | None = None) -> int:
    """Store the edges from a JumpGate payload ``{symbol, connections[]}``.

    Each connection is a gate waypoint in another system; the edge lets us later
    plan multi-hop routes. Returns the number of edges recorded.
    """
    from_gate = gate.get("symbol", "")
    connections = gate.get("connections") or []
    if not from_gate or not connections:
        return 0
    from_system = _sys_of(from_gate)
    rows = [
        (from_system, from_gate, _sys_of(to_gate), to_gate)
        for to_gate in connections
    ]
    c = conn or connect()
    with _lock:
        c.executemany(
            """
            INSERT INTO jump_edges (from_system, from_gate, to_system, to_gate)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(from_gate, to_gate) DO UPDATE SET
                from_system=excluded.from_system, to_system=excluded.to_system
            """,
            rows,
        )
        c.commit()
    return len(rows)


def jump_edges(conn: sqlite3.Connection | None = None) -> list[dict]:
    c = conn or connect()
    with _lock:
        cur = c.execute("SELECT * FROM jump_edges")
        return [dict(r) for r in cur.fetchall()]


def latest_prices(
    system: str | None = None,
    max_age_s: float | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Return observations (one row per waypoint+good), optionally filtered."""
    c = conn or connect()
    sql = "SELECT * FROM market_observations"
    clauses: list[str] = []
    params: list = []
    if system is not None:
        clauses.append("system = ?")
        params.append(system)
    if max_age_s is not None:
        clauses.append("observed_at >= ?")
        params.append(time.time() - max_age_s)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    with _lock:
        cur = c.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def best_routes(
    system: str | None = None,
    min_profit: int = 1,
    max_age_s: float = 3600.0,
    max_hops: int = 0,
    hop_penalty: int = 0,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Rank profitable routes from the freshest stored prices.

    With ``max_hops == 0`` this is same-system only. With ``max_hops > 0`` (and a
    ``system`` to start from) it also considers markets in systems reachable
    through the recorded jump-gate network, scored net of a per-hop penalty.
    """
    if max_hops and system:
        obs = latest_prices(max_age_s=max_age_s, conn=conn)
        adj, _ = build_graph(jump_edges(conn=conn))
        return cross_system_scan(
            obs, adj, current_system=system, min_profit=min_profit,
            max_hops=max_hops, hop_penalty=hop_penalty,
        )
    obs = latest_prices(system=system, max_age_s=max_age_s, conn=conn)
    return arbitrage_scan(obs, min_profit=min_profit, system=system)
