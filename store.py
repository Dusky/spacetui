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

-- append-only time series (the snapshot table above keeps only latest prices)
CREATE TABLE IF NOT EXISTS price_history (
    waypoint       TEXT NOT NULL,
    system         TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    purchase_price INTEGER,
    sell_price     INTEGER,
    supply         TEXT,
    activity       TEXT,
    observed_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history ON price_history (symbol, observed_at);

CREATE TABLE IF NOT EXISTS credit_history (
    observed_at REAL NOT NULL,
    credits     INTEGER NOT NULL,
    ship_count  INTEGER
);

CREATE TABLE IF NOT EXISTS trades (
    observed_at    REAL NOT NULL,
    ship           TEXT,
    waypoint       TEXT,
    system         TEXT,
    action         TEXT NOT NULL,   -- 'buy' or 'sell'
    symbol         TEXT NOT NULL,
    units          INTEGER NOT NULL,
    price_per_unit INTEGER,
    total          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades (observed_at);
CREATE INDEX IF NOT EXISTS idx_trades_ship ON trades (ship, observed_at);

-- the shared world model persists here so a restarted process starts warm
CREATE TABLE IF NOT EXISTS waypoints (
    symbol      TEXT PRIMARY KEY,
    system      TEXT NOT NULL,
    type        TEXT,
    x           INTEGER,
    y           INTEGER,
    traits      TEXT,            -- comma-joined trait symbols
    observed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_waypoints_system ON waypoints (system);

-- which role a ship was put to work in, and when (feeds the ROI view)
CREATE TABLE IF NOT EXISTS ship_assignments (
    observed_at REAL NOT NULL,
    ship        TEXT NOT NULL,
    role        TEXT,
    route       TEXT
);
CREATE INDEX IF NOT EXISTS idx_assign_ship ON ship_assignments (ship, observed_at);
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
    history = [
        (waypoint, system, g.get("symbol", ""), g.get("purchasePrice"),
         g.get("sellPrice"), g.get("supply"), g.get("activity"), now)
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
        c.executemany(
            """INSERT INTO price_history
                 (waypoint, system, symbol, purchase_price, sell_price,
                  supply, activity, observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            history,
        )
        c.commit()
    return len(rows)


def record_credits(
    credits: int, ship_count: int = 0, conn: sqlite3.Connection | None = None
) -> bool:
    """Append a net-worth data point, de-duped so a stable balance doesn't spam
    the series (skips if the last point is <30s old and unchanged)."""
    c = conn or connect()
    now = time.time()
    with _lock:
        row = c.execute(
            "SELECT observed_at, credits FROM credit_history ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()
        if row and row["credits"] == credits and now - row["observed_at"] < 30:
            return False
        c.execute(
            "INSERT INTO credit_history (observed_at, credits, ship_count) VALUES (?, ?, ?)",
            (now, credits, ship_count),
        )
        c.commit()
    return True


def record_trade(
    action: str,
    symbol: str,
    units: int,
    price_per_unit: int,
    total: int,
    *,
    ship: str = "",
    waypoint: str = "",
    conn: sqlite3.Connection | None = None,
) -> None:
    c = conn or connect()
    with _lock:
        c.execute(
            """INSERT INTO trades
                 (observed_at, ship, waypoint, system, action, symbol, units,
                  price_per_unit, total)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), ship, waypoint, _system_of(waypoint) if waypoint else "",
             action, symbol, int(units), int(price_per_unit), int(total)),
        )
        c.commit()


# -- analytics queries -----------------------------------------------------
def credit_series(limit: int = 300, conn: sqlite3.Connection | None = None) -> list[dict]:
    c = conn or connect()
    with _lock:
        rows = c.execute(
            "SELECT observed_at, credits, ship_count FROM credit_history "
            "ORDER BY observed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def price_series(
    symbol: str,
    waypoint: str | None = None,
    limit: int = 300,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    c = conn or connect()
    sql = "SELECT observed_at, purchase_price, sell_price, waypoint FROM price_history WHERE symbol = ?"
    params: list = [symbol]
    if waypoint:
        sql += " AND waypoint = ?"
        params.append(waypoint)
    sql += " ORDER BY observed_at DESC LIMIT ?"
    params.append(limit)
    with _lock:
        rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in reversed(rows)]


def tracked_goods(limit: int = 12, conn: sqlite3.Connection | None = None) -> list[str]:
    """Goods with the most price observations (best chart candidates)."""
    c = conn or connect()
    with _lock:
        rows = c.execute(
            "SELECT symbol, COUNT(*) n FROM price_history GROUP BY symbol "
            "ORDER BY n DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [r["symbol"] for r in rows]


def pnl_summary(conn: sqlite3.Connection | None = None) -> dict:
    c = conn or connect()
    with _lock:
        row = c.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN action='buy'  THEN total END), 0) spent,
                 COALESCE(SUM(CASE WHEN action='sell' THEN total END), 0) earned,
                 COUNT(*) trades
               FROM trades"""
        ).fetchone()
    spent, earned = row["spent"], row["earned"]
    return {"spent": spent, "earned": earned, "net": earned - spent, "trades": row["trades"]}


def pnl_by_good(limit: int = 8, conn: sqlite3.Connection | None = None) -> list[dict]:
    c = conn or connect()
    with _lock:
        rows = c.execute(
            """SELECT symbol,
                 COALESCE(SUM(CASE WHEN action='buy'  THEN total END), 0) spent,
                 COALESCE(SUM(CASE WHEN action='sell' THEN total END), 0) earned
               FROM trades GROUP BY symbol"""
        ).fetchall()
    out = [
        {"symbol": r["symbol"], "spent": r["spent"], "earned": r["earned"],
         "net": r["earned"] - r["spent"]}
        for r in rows
    ]
    out.sort(key=lambda d: d["net"], reverse=True)
    return out[:limit]


def ship_pnl(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Per-ship spent/earned totals (feeds ROI-per-ship)."""
    c = conn or connect()
    with _lock:
        rows = c.execute(
            """SELECT ship,
                 COALESCE(SUM(CASE WHEN action='buy'  THEN total END), 0) spent,
                 COALESCE(SUM(CASE WHEN action='sell' THEN total END), 0) earned
               FROM trades WHERE ship IS NOT NULL AND ship != ''
               GROUP BY ship"""
        ).fetchall()
    return [dict(r) for r in rows]


def recent_trades(limit: int = 12, conn: sqlite3.Connection | None = None) -> list[dict]:
    c = conn or connect()
    with _lock:
        rows = c.execute(
            "SELECT * FROM trades ORDER BY observed_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def activity_breakdown(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Count of latest market goods by activity level (e.g. WEAK/GROWING/STRONG)."""
    c = conn or connect()
    with _lock:
        rows = c.execute(
            "SELECT COALESCE(activity,'UNKNOWN') a, COUNT(*) n FROM market_observations "
            "GROUP BY a ORDER BY n DESC"
        ).fetchall()
    return {r["a"]: r["n"] for r in rows}


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


# -- world model persistence -----------------------------------------------
def record_waypoints(
    system: str, waypoints: list[dict], conn: sqlite3.Connection | None = None
) -> int:
    """Upsert normalized waypoint dicts ``{symbol,type,x,y,traits:[...]}`` for a
    system so the world model can rehydrate them across process restarts."""
    if not waypoints:
        return 0
    now = time.time()
    rows = [
        (w.get("symbol", ""), system, w.get("type"),
         w.get("x", 0), w.get("y", 0),
         ",".join(w.get("traits", []) or []), now)
        for w in waypoints if w.get("symbol")
    ]
    if not rows:
        return 0
    c = conn or connect()
    with _lock:
        c.executemany(
            """INSERT INTO waypoints (symbol, system, type, x, y, traits, observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                   system=excluded.system, type=excluded.type,
                   x=excluded.x, y=excluded.y, traits=excluded.traits,
                   observed_at=excluded.observed_at""",
            rows,
        )
        c.commit()
    return len(rows)


def waypoints_of(
    system: str, max_age_s: float | None = None, conn: sqlite3.Connection | None = None
) -> list[dict]:
    """Return normalized waypoint dicts for a system (traits split back to a
    list), optionally only those seen within ``max_age_s``."""
    c = conn or connect()
    sql = "SELECT symbol, type, x, y, traits FROM waypoints WHERE system = ?"
    params: list = [system]
    if max_age_s is not None:
        sql += " AND observed_at >= ?"
        params.append(time.time() - max_age_s)
    with _lock:
        rows = c.execute(sql, params).fetchall()
    return [
        {"symbol": r["symbol"], "type": r["type"], "x": r["x"], "y": r["y"],
         "traits": [t for t in (r["traits"] or "").split(",") if t]}
        for r in rows
    ]


def record_ship_assignment(
    ship: str, role: str, route: str = "", conn: sqlite3.Connection | None = None
) -> None:
    """Append a ship→role assignment so per-ship history survives a restart."""
    c = conn or connect()
    with _lock:
        c.execute(
            "INSERT INTO ship_assignments (observed_at, ship, role, route) VALUES (?, ?, ?, ?)",
            (time.time(), ship, role, route),
        )
        c.commit()


def ship_assignments(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Latest role per ship (most recent assignment wins; rowid breaks ties)."""
    c = conn or connect()
    with _lock:
        rows = c.execute(
            """SELECT ship, role, route, observed_at FROM ship_assignments
               WHERE rowid IN (SELECT MAX(rowid) FROM ship_assignments GROUP BY ship)
               ORDER BY ship"""
        ).fetchall()
    return [dict(r) for r in rows]


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
