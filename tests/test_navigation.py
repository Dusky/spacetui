import unittest

from navigation import WaypointCache, distance, fuel_cost, system_of, travel_time
from tests.fakes import FakeClient, make_world


class TestMath(unittest.TestCase):
    def test_system_of(self):
        self.assertEqual(system_of("X1-N85-A1"), "X1-N85")
        self.assertEqual(system_of("X1-ABC12-BB7D"), "X1-ABC12")
        self.assertEqual(system_of("X1-N85"), "X1-N85")

    def test_distance(self):
        a = {"x": 0, "y": 0}
        b = {"x": 3, "y": 4}
        self.assertEqual(distance(a, b), 5.0)

    def test_fuel_cost(self):
        self.assertEqual(fuel_cost(10, "CRUISE"), 10)
        self.assertEqual(fuel_cost(10, "BURN"), 20)
        self.assertEqual(fuel_cost(1000, "DRIFT"), 1)
        self.assertEqual(fuel_cost(0.2, "CRUISE"), 1)  # never free

    def test_travel_time(self):
        # dist 100, speed 30, cruise: round(100*25/30 + 15) = 98
        self.assertEqual(travel_time(100, 30, "CRUISE"), 98)
        self.assertGreater(travel_time(100, 30, "DRIFT"), travel_time(100, 30, "CRUISE"))


class TestWaypointCache(unittest.TestCase):
    def setUp(self):
        wps, markets, ships = make_world()
        self.client = FakeClient(wps, markets, ships)
        self.cache = WaypointCache(self.client)

    def test_caches_after_first_call(self):
        self.cache.waypoints("S1-AA")
        self.client._waypoints = []  # mutate source; cache should still serve
        self.assertEqual(len(self.cache.waypoints("S1-AA")), 3)

    def test_traits_and_nearest(self):
        markets = self.cache.markets("S1-AA")
        self.assertEqual({w["symbol"] for w in markets}, {"S1-AA-A1", "S1-AA-B2"})
        rocks = self.cache.asteroids("S1-AA")
        self.assertEqual(rocks[0]["symbol"], "S1-AA-C3")
        near = self.cache.nearest("S1-AA", "S1-AA-A1", markets)
        self.assertEqual(near["symbol"], "S1-AA-A1")


if __name__ == "__main__":
    unittest.main()
