import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("ST_HQ", "X1-AF2-A1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store

# isolate the process-global store DB (World calls store without a conn arg)
store.DB_PATH = tempfile.mktemp(suffix=".db")
store._conn = None

from world import World


class FakeClient:
    def __init__(self, delay: float = 0.0):
        self.wp_calls = 0
        self.market_calls = 0
        self.shipyard_calls = 0
        self.delay = delay

    def waypoints(self, system, filters=None):
        # the world always fetches the full, unfiltered list and filters locally
        self.wp_calls += 1
        return [
            {"symbol": f"{system}-A", "type": "PLANET", "x": 1, "y": 2,
             "traits": [{"symbol": "MARKETPLACE"}]},
            {"symbol": f"{system}-B", "type": "ASTEROID_FIELD", "x": 3, "y": 4,
             "traits": [{"symbol": "MINERAL_DEPOSITS"}]},
            {"symbol": f"{system}-Y", "type": "ORBITAL_STATION", "x": 0, "y": 0,
             "traits": [{"symbol": "SHIPYARD"}]},
        ]

    def market(self, system, waypoint):
        self.market_calls += 1
        if self.delay:
            time.sleep(self.delay)
        return {"symbol": waypoint, "tradeGoods": [
            {"symbol": "IRON", "type": "EXCHANGE", "purchasePrice": 100,
             "sellPrice": 90, "tradeVolume": 10}]}

    def shipyard(self, system, waypoint):
        self.shipyard_calls += 1
        return {"symbol": waypoint, "ships": [
            {"type": "SHIP_MINING_DRONE", "purchasePrice": 60000}]}


def test_get_waypoints_caches():
    c = FakeClient()
    w = World(c)
    a = w.get_waypoints("X9-AAA")
    w.get_waypoints("X9-AAA")  # second read is served from memory
    assert c.wp_calls == 1
    assert {x["symbol"] for x in a} == {"X9-AAA-A", "X9-AAA-B", "X9-AAA-Y"}


def test_find_waypoints_filters_from_one_fetch():
    c = FakeClient()
    w = World(c)
    yards = w.find_waypoints("X9-BBB", trait="SHIPYARD")
    fields = w.find_waypoints("X9-BBB", type="ASTEROID_FIELD")
    assert [x["symbol"] for x in yards] == ["X9-BBB-Y"]
    assert [x["symbol"] for x in fields] == ["X9-BBB-B"]
    assert c.wp_calls == 1  # both filtered views come from the single cached list


def test_get_market_cached_and_written_through():
    c = FakeClient()
    w = World(c)
    m1 = w.get_market("X9-CCC", "X9-CCC-A")
    w.get_market("X9-CCC", "X9-CCC-A")
    assert c.market_calls == 1
    assert m1["symbol"] == "X9-CCC-A"
    # write-through to the store so arbitrage can see it
    obs = {o["waypoint"] for o in store.latest_prices(system="X9-CCC")}
    assert "X9-CCC-A" in obs


def test_get_market_single_flight():
    c = FakeClient(delay=0.05)
    w = World(c)
    results: list = []

    def go():
        results.append(w.get_market("X9-DDD", "X9-DDD-A"))

    threads = [threading.Thread(target=go) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.market_calls == 1  # concurrent lookups coalesced into one API call
    assert all(r["symbol"] == "X9-DDD-A" for r in results)


def test_warm_restart_no_api_call():
    # a previous process persisted these; a fresh World must read them warm
    store.record_waypoints("X9-WARM", [
        {"symbol": "X9-WARM-A", "type": "PLANET", "x": 1, "y": 2,
         "traits": ["MARKETPLACE", "SHIPYARD"]}])
    c = FakeClient()
    w = World(c)
    wps = w.get_waypoints("X9-WARM")
    assert c.wp_calls == 0
    assert wps[0]["symbol"] == "X9-WARM-A"
    assert wps[0]["traits"] == ["MARKETPLACE", "SHIPYARD"]


def test_ship_types_from_world():
    c = FakeClient()
    w = World(c)
    types = w.ship_types("X9-EEE")
    assert types == [{"type": "SHIP_MINING_DRONE", "price": 60000}]
    assert c.shipyard_calls == 1


def test_ship_assignment_latest_wins():
    store.record_ship_assignment("ROIT-1", "miner")
    store.record_ship_assignment("ROIT-1", "trader")   # newer assignment wins
    store.record_ship_assignment("ROIT-2", "scout")
    by_ship = {r["ship"]: r["role"] for r in store.ship_assignments()}
    assert by_ship["ROIT-1"] == "trader"
    assert by_ship["ROIT-2"] == "scout"
