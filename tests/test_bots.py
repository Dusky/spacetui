import tempfile
import unittest
from pathlib import Path

from automation import ContractBot, TraderBot
from market import MarketDB
from navigation import WaypointCache
from tests.fakes import FakeClient, make_world


def _db(tmp):
    return MarketDB(Path(tmp.name) / "m.db")


class TestTraderBot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        wps, markets, ships = make_world()
        self.client = FakeClient(wps, markets, ships)
        self.db = _db(self.tmp)
        for m in markets.values():
            self.db.record_market(m)

    def tearDown(self):
        self.tmp.cleanup()

    def test_executes_arbitrage_round_trip(self):
        start_credits = self.client._agent["credits"]
        bot = TraderBot(
            self.client,
            "TEST-1",
            cache=WaypointCache(self.client),
            db=self.db,
            max_cycles=1,
            reserve_credits=0,
        )
        bot.run()
        buys = [e for e in self.client.log if e[0] == "purchase"]
        sells = [e for e in self.client.log if e[0] == "sell"]
        self.assertTrue(buys, "trader never bought")
        self.assertTrue(sells, "trader never sold")
        self.assertEqual(buys[0][2], "IRON")
        self.assertEqual(sells[0][2], "IRON")
        # 20 cargo * 50 margin = +1000 credits
        self.assertEqual(self.client._agent["credits"], start_credits + 1000)
        # sold at the high-price market
        sell_wp = self.client._ships["TEST-1"]["nav"]["waypointSymbol"]
        self.assertEqual(sell_wp, "S1-AA-B2")

    def test_scans_when_no_data(self):
        empty_db = MarketDB(Path(self.tmp.name) / "empty.db")
        bot = TraderBot(
            self.client,
            "TEST-1",
            cache=WaypointCache(self.client),
            db=empty_db,
            max_cycles=1,
        )
        bot.run()
        # with no price data the bot should have recorded at least one market
        self.assertTrue(empty_db.coverage("S1-AA"))


class TestContractBot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        wps, markets, ships = make_world()
        self.contract = {
            "id": "c1",
            "type": "PROCUREMENT",
            "accepted": False,
            "fulfilled": False,
            "terms": {
                "payment": {"onAccepted": 1000, "onFulfilled": 5000},
                "deliver": [
                    {"tradeSymbol": "IRON", "destinationSymbol": "S1-AA-B2",
                     "unitsRequired": 15, "unitsFulfilled": 0}
                ],
            },
        }
        self.client = FakeClient(wps, markets, ships, contracts=[self.contract])
        self.db = _db(self.tmp)
        for m in markets.values():
            self.db.record_market(m)

    def tearDown(self):
        self.tmp.cleanup()

    def test_buys_delivers_and_fulfills(self):
        bot = ContractBot(
            self.client,
            "TEST-1",
            cache=WaypointCache(self.client),
            db=self.db,
            max_cycles=4,
            reserve_credits=0,
        )
        bot.run()
        ops = [e[0] for e in self.client.log]
        self.assertIn("accept", ops)
        self.assertIn("purchase", ops)
        self.assertIn("deliver", ops)
        self.assertIn("fulfill", ops)
        self.assertTrue(self.client._contracts[0]["fulfilled"])
        term = self.client._contracts[0]["terms"]["deliver"][0]
        self.assertEqual(term["unitsFulfilled"], 15)


if __name__ == "__main__":
    unittest.main()
