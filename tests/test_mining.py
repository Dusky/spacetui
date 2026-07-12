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

    def cargo(self, sym):
        return {"units": self.cargo_units, "capacity": self.cargo_cap,
                "inventory": [{"symbol": k, "units": v} for k, v in self.inv.items()]}

    def jettison(self, sym, good, units):
        have = self.inv.get(good, 0)
        n = min(units, have)
        self.inv[good] = have - n
        if self.inv[good] <= 0:
            self.inv.pop(good, None)
        self.cargo_units -= n
        return {"cargo": self.cargo(sym)}

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


def test_holdless_ship_disengages_instead_of_spinning():
    # a probe (cargo capacity 0) can't mine: units>=capacity (0>=0) used to read
    # as "full" and loop forever on "cargo full 0/0 -> sell -> backing off 30s"
    c = MineFake()
    c.cargo_cap = 0
    bot = MinerBot(c, "ESOF-2", world=None, on_log=lambda m: None)
    t = threading.Thread(target=bot.run, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "hold-less miner never disengaged"
    assert c.extracts == 0 and c.sells == 0


def test_empty_hold_sell_off_counts_as_progress():
    c = MineFake()  # inv is empty by default
    bot = MinerBot(c, "ESOF-1", world=None, on_log=lambda m: None)
    assert bot._sell_off(c.ship("ESOF-1"), None) is True


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


class SellFake:
    """The 8-hour livelock layout: market A1 lists only FUEL, market B4 lists
    IRON_ORE + ICE_WATER; the miner sits at A1 with a full mixed hold."""

    LISTINGS = {
        "X1-AF2-A1": ["FUEL"],
        "X1-AF2-B4": ["IRON_ORE", "ICE_WATER"],
    }

    def __init__(self, fail_markets=False):
        self.wp = "X1-AF2-A1"
        self.status = "DOCKED"
        self.inv = {"IRON_ORE": 20, "ICE_WATER": 10, "QUARTZ_SAND": 10}
        self.cargo_cap = 40
        self.fail_markets = fail_markets
        self.sell_calls = []       # (waypoint, good)
        self.jettisoned = []       # (good, units)
        self.nav_log = []
        self.done = threading.Event()   # set when the hold empties

    @property
    def cargo_units(self):
        return sum(self.inv.values())

    def ship(self, sym):
        return {"symbol": sym,
                "nav": {"status": self.status, "waypointSymbol": self.wp,
                        "systemSymbol": "X1-AF2", "route": {}},
                "fuel": {"current": 400, "capacity": 400},
                "cargo": {"units": self.cargo_units, "capacity": self.cargo_cap,
                          "inventory": [{"symbol": k, "units": v} for k, v in self.inv.items()]},
                "mounts": [{"symbol": "MOUNT_MINING_LASER_I"}]}

    def cargo(self, sym):
        return self.ship(sym)["cargo"]

    def orbit(self, sym):
        self.status = "IN_ORBIT"; return {}

    def dock(self, sym):
        self.status = "DOCKED"; return {}

    def refuel(self, sym):
        return {"transaction": {}}

    def set_flight_mode(self, sym, mode):
        return {}

    def cooldown(self, sym):
        return {}

    def navigate(self, sym, wp):
        self.nav_log.append(wp)
        self.wp = wp
        self.status = "IN_ORBIT"
        return {"nav": {"status": self.status, "waypointSymbol": wp}}

    def market(self, system, wp):
        if self.fail_markets:
            raise ApiError(503, "market temporarily unavailable")
        return {"symbol": wp,
                "imports": [{"symbol": g} for g in self.LISTINGS.get(wp, [])]}

    def sell(self, sym, good, units):
        self.sell_calls.append((self.wp, good))
        if good not in self.LISTINGS.get(self.wp, []):
            raise ApiError(4602, f"Trade good {good} is not listed at market {self.wp}")
        have = self.inv.get(good, 0)
        n = min(units, have)
        self.inv[good] = have - n
        if self.inv[good] <= 0:
            self.inv.pop(good, None)
        if not self.inv:
            self.done.set()
        return {"transaction": {"units": n, "pricePerUnit": 30, "totalPrice": 30 * n,
                                "symbol": good}}

    def jettison(self, sym, good, units):
        self.jettisoned.append((good, units))
        self.inv.pop(good, None)
        if not self.inv:
            self.done.set()
        return {"cargo": self.cargo(sym)}

    def waypoints(self, system, filters=None):
        allwp = [
            {"symbol": "X1-AF2-A1", "type": "PLANET", "x": 10, "y": 5,
             "traits": [{"symbol": "MARKETPLACE"}]},
            {"symbol": "X1-AF2-B4", "type": "MOON", "x": 60, "y": 5,
             "traits": [{"symbol": "MARKETPLACE"}]},
            {"symbol": "X1-AF2-B8", "type": "ASTEROID_FIELD", "x": 200, "y": 5,
             "traits": [{"symbol": "MINERAL_DEPOSITS"}]},
        ]
        if filters and filters.get("traits"):
            return [w for w in allwp
                    if any(t["symbol"] == filters["traits"] for t in w["traits"])]
        return allwp

    def waypoint(self, system, wp):
        for w in self.waypoints(system):
            if w["symbol"] == wp:
                return w
        return {"symbol": wp, "traits": []}


def test_sell_goes_where_goods_are_listed_and_jettisons_the_rest():
    c = SellFake()
    bot = MinerBot(c, "ESOF-1", world=None, on_log=lambda m: None)
    made_progress = bot._sell_off(c.ship("ESOF-1"), None)

    assert made_progress is True
    assert c.inv == {}                                # the hold emptied
    # sold at B4 (which lists the goods), never attempted the unlisted A1 sale
    assert ("X1-AF2-B4", "IRON_ORE") in c.sell_calls
    assert ("X1-AF2-B4", "ICE_WATER") in c.sell_calls
    assert all(wp != "X1-AF2-A1" for wp, _ in c.sell_calls)
    # the good nothing buys was jettisoned, and remembered as unsellable
    assert ("QUARTZ_SAND", 10) in c.jettisoned
    assert "QUARTZ_SAND" in bot._unsellable


def test_contract_goods_survive_the_jettison():
    c = SellFake()
    bot = MinerBot(c, "ESOF-1", world=None, on_log=lambda m: None)
    bot.contract_id = "ctr-1"
    contract = {"id": "ctr-1", "accepted": True, "fulfilled": False,
                "terms": {"deliver": [{"tradeSymbol": "QUARTZ_SAND",
                                       "unitsRequired": 100, "unitsFulfilled": 0,
                                       "destinationSymbol": "X1-AF2-A1"}]}}
    # deliver_contract isn't implemented on the fake -> delivery is skipped,
    # but the jettison pass must still protect the contract good
    c.deliver_contract = lambda cid, ship, good, units: (_ for _ in ()).throw(
        ApiError(400, "not accepting deliveries in this test"))
    bot._sell_off(c.ship("ESOF-1"), contract)
    assert all(g != "QUARTZ_SAND" for g, _ in c.jettisoned)
    assert c.inv.get("QUARTZ_SAND") == 10             # still aboard


def test_no_market_data_means_no_jettison_and_no_progress():
    c = SellFake(fail_markets=True)
    bot = MinerBot(c, "ESOF-1", world=None, on_log=lambda m: None)
    made_progress = bot._sell_off(c.ship("ESOF-1"), None)
    assert made_progress is False                     # caller backs off 30s
    assert c.jettisoned == []                         # never dump on zero data
    assert c.inv["IRON_ORE"] == 20                    # cargo intact


def test_known_junk_yield_is_jettisoned_at_the_rock():
    c = SellFake()
    c.wp, c.status = "X1-AF2-B8", "IN_ORBIT"          # sitting on the rock
    c.inv = {}
    c.extracted = False

    def extract(sym, survey=None):
        c.extracted = True
        c.inv["QUARTZ_SAND"] = c.inv.get("QUARTZ_SAND", 0) + 5
        return {"extraction": {"yield": {"units": 5, "symbol": "QUARTZ_SAND"}},
                "cooldown": {"totalSeconds": 0}}
    c.extract = extract

    bot = MinerBot(c, "ESOF-1", world=None, on_log=lambda m: None)
    bot._unsellable = {"QUARTZ_SAND"}                 # learned on a prior pass
    t = threading.Thread(target=bot.run, daemon=True)
    t.start()
    for _ in range(200):
        if c.jettisoned:
            break
        threading.Event().wait(0.05)
    bot.stop()
    t.join(timeout=5)
    assert c.extracted
    assert c.jettisoned and c.jettisoned[0][0] == "QUARTZ_SAND"


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
