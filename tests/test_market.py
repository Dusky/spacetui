import tempfile
import unittest
from pathlib import Path

from market import MarketDB, plan_routes
from tests.fakes import make_world


class TestMarketDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = MarketDB(Path(self.tmp.name) / "m.db")
        _, self.markets, _ = make_world()

    def tearDown(self):
        self.tmp.cleanup()

    def _record_all(self):
        for m in self.markets.values():
            self.db.record_market(m)

    def test_record_and_read(self):
        n = self.db.record_market(self.markets["S1-AA-A1"])
        self.assertEqual(n, 2)
        rows = self.db.prices("S1-AA")
        self.assertEqual(len(rows), 2)

    def test_best_buy_sell(self):
        self._record_all()
        buy = self.db.best_buy("S1-AA", "IRON")
        sell = self.db.best_sell("S1-AA", "IRON")
        self.assertEqual(buy.waypoint, "S1-AA-A1")
        self.assertEqual(buy.buy, 40)
        self.assertEqual(sell.waypoint, "S1-AA-B2")
        self.assertEqual(sell.sell, 90)

    def test_coverage(self):
        self.db.record_market(self.markets["S1-AA-A1"])
        self.assertEqual(self.db.coverage("S1-AA"), {"S1-AA-A1"})

    def test_plan_routes_finds_arbitrage(self):
        self._record_all()
        wps, _, _ = make_world()
        routes = plan_routes(self.db.prices("S1-AA"), {w["symbol"]: w for w in wps})
        self.assertTrue(routes)
        best = routes[0]
        self.assertEqual(best.good, "IRON")
        self.assertEqual(best.buy_waypoint, "S1-AA-A1")
        self.assertEqual(best.sell_waypoint, "S1-AA-B2")
        self.assertEqual(best.margin, 50)

    def test_plan_routes_respects_min_margin(self):
        self._record_all()
        routes = plan_routes(self.db.prices("S1-AA"), min_margin=100)
        self.assertEqual(routes, [])

    def test_no_same_waypoint_route(self):
        self._record_all()
        for r in plan_routes(self.db.prices("S1-AA")):
            self.assertNotEqual(r.buy_waypoint, r.sell_waypoint)


if __name__ == "__main__":
    unittest.main()
