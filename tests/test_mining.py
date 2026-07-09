import math
import os
import sys
import tempfile
import threading

os.environ.setdefault("ST_HQ", "X1-AF2-A1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import store

store.DB_PATH = tempfile.mktemp(suffix=".db")
store._conn = None

from api import ApiError
from tui.bots import MinerBot


@pytest.fixture(autouse=True)
def isolated_store():
    """Give each test its own store DB so the miner's recorded trades don't leak
    into other modules' exact-count assertions (the store conn is process-wide)."""
    old_path, old_conn = store.DB_PATH, store._conn
    store.DB_PATH = tempfile.mktemp(suffix=".db")
    store._conn = None
    try:
        yield
    finally:
        store.DB_PATH, store._conn = old_path, old_conn

# HQ/market is A1; the only asteroid field B8 is far enough that a full tank
# (400) arrives at ~57 — below the old 50% refuel threshold, which used to make
# the miner divert back to HQ forever without ever extracting.
COORDS = {"X1-AF2-A1": (10, 5), "X1-AF2-B8": (353, 5)}


class MineFake:
    def __init__(self):
        self.wp = "X1-AF2-A1"
        self.status = "DOCKED"
        self.fuel = 400
        self.cap = 400
        self.mode = "CRUISE"
        self.cargo_units = 0
        self.cargo_cap = 40
        self.inv = {}
        self.nav_log = []          # (target, cargo_units_at_departure)
        self.extracts = 0
        self.sells = 0
        self.sold = threading.Event()

    def ship(self, sym):
        return {
            "symbol": sym,
            "nav": {"status": self.status, "waypointSymbol": self.wp,
                    "systemSymbol": "X1-AF2", "route": {}},
            "fuel": {"current": self.fuel, "capacity": self.cap},
            "cargo": {"units": self.cargo_units, "capacity": self.cargo_cap,
                      "inventory": [{"symbol": k, "units": v} for k, v in self.inv.items()]},
            "mounts": [{"symbol": "MOUNT_MINING_LASER_I"}],
        }

    def orbit(self, sym):
        self.status = "IN_ORBIT"; return {}

    def dock(self, sym):
        self.status = "DOCKED"; return {}

    def set_flight_mode(self, sym, mode):
        self.mode = mode; return {}

    def cooldown(self, sym):
        return {}

    def refuel(self, sym):
        self.fuel = self.cap; return {"transaction": {}}

    def navigate(self, sym, wp):
        dist = math.dist(COORDS[self.wp], COORDS[wp])
        cost = int(dist)
        if self.mode == "CRUISE" and cost > self.fuel:
            raise ApiError(4203, "insufficient fuel for CRUISE")
        self.nav_log.append((wp, self.cargo_units))
        self.fuel = max(0, self.fuel - (cost if self.mode == "CRUISE" else 1))
        self.wp = wp
        self.status = "IN_ORBIT"
        return {"nav": {"status": self.status, "waypointSymbol": wp}}

    def extract(self, sym, survey=None):
        self.extracts += 1
        add = min(10, self.cargo_cap - self.cargo_units)
        self.cargo_units += add
        self.inv["IRON_ORE"] = self.inv.get("IRON_ORE", 0) + add
        return {"extraction": {"yield": {"units": add, "symbol": "IRON_ORE"}},
                "cooldown": {"totalSeconds": 0}}

    def sell(self, sym, good, units):
        self.sells += 1
        have = self.inv.get(good, 0)
        n = min(units, have)
        self.inv[good] = have - n
        if self.inv[good] <= 0:
            self.inv.pop(good, None)
        self.cargo_units -= n
        self.sold.set()
        return {"transaction": {"units": n, "pricePerUnit": 50, "totalPrice": 50 * n,
                                "symbol": good}, "agent": {"credits": 1000}}

    def market(self, system, wp):
        return {"symbol": wp, "tradeGoods": [
            {"symbol": "IRON_ORE", "type": "EXCHANGE", "purchasePrice": 40,
             "sellPrice": 50, "tradeVolume": 50}]}

    def waypoint(self, system, wp):
        trait = "MARKETPLACE" if wp == "X1-AF2-A1" else "MINERAL_DEPOSITS"
        return {"symbol": wp, "traits": [{"symbol": trait}]}

    def waypoints(self, system, filters=None):
        allwp = [
            {"symbol": "X1-AF2-A1", "type": "PLANET", "x": 10, "y": 5,
             "traits": [{"symbol": "MARKETPLACE"}]},
            {"symbol": "X1-AF2-B8", "type": "ASTEROID_FIELD", "x": 353, "y": 5,
             "traits": [{"symbol": "MINERAL_DEPOSITS"}]},
        ]
        if filters and filters.get("traits"):
            return [w for w in allwp
                    if any(t["symbol"] == filters["traits"] for t in w["traits"])]
        return allwp


def test_miner_mines_and_sells_instead_of_livelocking():
    c = MineFake()
    bot = MinerBot(c, "ESOF-1", world=None, on_log=lambda m: None)
    t = threading.Thread(target=bot.run, daemon=True)
    t.start()
    completed = c.sold.wait(timeout=10)
    bot.stop()
    t.join(timeout=5)

    assert completed, "miner never completed a mine→sell cycle (livelocked)"
    assert c.extracts > 0, "miner never extracted"
    assert c.sells > 0, "miner never sold"
    # the old bug: divert back to HQ while still holding cargo space. Every trip
    # to HQ must be a full hold going to sell, never an empty-handed refuel run.
    diverts = [u for (wp, u) in c.nav_log if wp == "X1-AF2-A1" and u < c.cargo_cap]
    assert diverts == [], f"miner diverted to HQ with cargo space: {c.nav_log}"


class GeoFake:
    """Only implements what _next_rock needs: a waypoint list with coordinates."""
    WPS = [
        {"symbol": "X1-AF2-O", "type": "PLANET", "x": 0, "y": 0, "traits": []},
        {"symbol": "X1-AF2-FAR", "type": "ASTEROID_FIELD", "x": 100, "y": 0,
         "traits": [{"symbol": "MINERAL_DEPOSITS"}]},
        {"symbol": "X1-AF2-NEAR", "type": "ASTEROID_FIELD", "x": 5, "y": 5,
         "traits": [{"symbol": "MINERAL_DEPOSITS"}]},
        {"symbol": "X1-AF2-MID", "type": "ASTEROID_FIELD", "x": 40, "y": 40,
         "traits": [{"symbol": "MINERAL_DEPOSITS"}]},
    ]

    def waypoints(self, system, filters=None):
        if filters and filters.get("traits"):
            return [w for w in self.WPS
                    if any(t["symbol"] == filters["traits"] for t in w["traits"])]
        return self.WPS


def test_next_rock_picks_nearest():
    bot = MinerBot(GeoFake(), "S", world=None, on_log=lambda m: None)
    rock = bot._next_rock("X1-AF2", set(), here="X1-AF2-O")
    assert rock["symbol"] == "X1-AF2-NEAR"
    # avoiding the nearest falls through to the next-nearest
    rock2 = bot._next_rock("X1-AF2", {"X1-AF2-NEAR"}, here="X1-AF2-O")
    assert rock2["symbol"] == "X1-AF2-MID"


class TransitFake:
    """navigate raises an in-transit [4214] once; the ship is already arriving."""
    def __init__(self):
        self.raised = False

    def ship(self, sym):
        return {"nav": {"status": "IN_ORBIT", "waypointSymbol": "X1-AF2-B8",
                        "systemSymbol": "X1-AF2", "route": {}}}

    def navigate(self, sym, wp):
        self.raised = True
        raise ApiError(4214, "Ship is currently in-transit from X1-AF2-A1 to X1-AF2-B8")


def test_goto_survives_in_transit_race():
    bot = MinerBot(TransitFake(), "S", world=None, on_log=lambda m: None)
    s = {"nav": {"status": "IN_ORBIT", "waypointSymbol": "X1-AF2-A1"}}
    out = bot._goto(s, "X1-AF2-B8")  # must not raise despite the [4214]
    assert bot.c.raised and out["nav"]["waypointSymbol"] == "X1-AF2-B8"
