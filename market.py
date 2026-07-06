"""Market intelligence: a SQLite price ledger + trade route planner.

Every time any ship (or the TUI) fetches a market, call `record_market` so the
whole fleet shares one view of prices. Routes are planned from that ledger.
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import time
from pathlib import Path

from navigation import WaypointCache, distance, system_of

DATA_DIR = Path(os.environ.get("ST_DATA_DIR", Path(__file__).resolve().parent / "data"))
DB_PATH = DATA_DIR / "markets.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    waypoint TEXT NOT NULL,
    system   TEXT NOT NULL,
    good     TEXT NOT NULL,
    type     TEXT,
    buy      INTEGER,
    sell     INTEGER,
    volume   INTEGER,
    supply   TEXT,
    activity TEXT,
    ts       REAL NOT NULL,
    PRIMARY KEY (waypoint, good)
);
CREATE INDEX IF NOT EXISTS idx_prices_system_good ON prices (system, good);
"""


@dataclasses.dataclass
class PriceRow:
    waypoint: str
    system: str
    good: str
    type: str
    buy: int  # what we pay to purchase
    sell: int  # what we receive when selling
    volume: int
    supply: str
    activity: str
    ts: float


@dataclasses.dataclass
class TradeRoute:
    good: str
    buy_waypoint: str
    buy_price: int
    sell_waypoint: str
    sell_price: int
    dist: float

    @property
    def margin(self) -> int:
        return self.sell_price - self.buy_price

    @property
    def score(self) -> float:
        """Margin discounted by travel distance (a rough profit/time proxy)."""
        return self.margin / (1.0 + self.dist / 100.0)


class MarketDB:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        # one short-lived connection per call keeps this trivially thread-safe
        return sqlite3.connect(self.path, timeout=10)

    # -- writes --------------------------------------------------------------
    def record_market(self, market: dict) -> int:
        """Store live tradeGoods from a market payload. Returns rows written."""
        goods = market.get("tradeGoods") or []
        if not goods:
            return 0
        wp = market.get("symbol", "")
        system = system_of(wp)
        now = time.time()
        rows = [
            (
                wp,
                system,
                g.get("symbol", ""),
                g.get("type", ""),
                g.get("purchasePrice"),
                g.get("sellPrice"),
                g.get("tradeVolume"),
                g.get("supply", ""),
                g.get("activity", ""),
                now,
            )
            for g in goods
        ]
        with self._conn() as con:
            con.executemany(
                "INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?,?,?,?)", rows
            )
        return len(rows)

    # -- reads ---------------------------------------------------------------
    def prices(self, system: str, *, max_age: float | None = None) -> list[PriceRow]:
        q = "SELECT * FROM prices WHERE system = ?"
        args: list = [system]
        if max_age is not None:
            q += " AND ts >= ?"
            args.append(time.time() - max_age)
        with self._conn() as con:
            rows = con.execute(q, args).fetchall()
        return [PriceRow(*r) for r in rows]

    def best_sell(self, system: str, good: str) -> PriceRow | None:
        """Where does `good` fetch the highest sell price?"""
        with self._conn() as con:
            r = con.execute(
                "SELECT * FROM prices WHERE system=? AND good=? AND sell IS NOT NULL "
                "ORDER BY sell DESC LIMIT 1",
                (system, good),
            ).fetchone()
        return PriceRow(*r) if r else None

    def best_buy(self, system: str, good: str) -> PriceRow | None:
        """Where can we purchase `good` most cheaply?"""
        with self._conn() as con:
            r = con.execute(
                "SELECT * FROM prices WHERE system=? AND good=? AND buy IS NOT NULL "
                "ORDER BY buy ASC LIMIT 1",
                (system, good),
            ).fetchone()
        return PriceRow(*r) if r else None

    def coverage(self, system: str) -> set[str]:
        """Waypoints in `system` we have any price data for."""
        with self._conn() as con:
            rows = con.execute(
                "SELECT DISTINCT waypoint FROM prices WHERE system=?", (system,)
            ).fetchall()
        return {r[0] for r in rows}


def plan_routes(
    prices: list[PriceRow],
    waypoints: dict[str, dict] | None = None,
    *,
    min_margin: int = 1,
) -> list[TradeRoute]:
    """Cross every (buy here, sell there) pair per good; keep profitable ones,
    best score first. `waypoints` maps symbol -> waypoint dict for distances."""
    by_good: dict[str, list[PriceRow]] = {}
    for p in prices:
        by_good.setdefault(p.good, []).append(p)

    routes: list[TradeRoute] = []
    for good, rows in by_good.items():
        for src in rows:
            if src.buy is None:
                continue
            for dst in rows:
                if dst.sell is None or dst.waypoint == src.waypoint:
                    continue
                margin = dst.sell - src.buy
                if margin < min_margin:
                    continue
                dist = 0.0
                if waypoints and src.waypoint in waypoints and dst.waypoint in waypoints:
                    dist = distance(waypoints[src.waypoint], waypoints[dst.waypoint])
                routes.append(
                    TradeRoute(good, src.waypoint, src.buy, dst.waypoint, dst.sell, dist)
                )
    routes.sort(key=lambda r: r.score, reverse=True)
    return routes


def best_routes(
    db: MarketDB, cache: WaypointCache, system: str, *, min_margin: int = 1
) -> list[TradeRoute]:
    wps = {w["symbol"]: w for w in cache.waypoints(system)}
    return plan_routes(db.prices(system), wps, min_margin=min_margin)
