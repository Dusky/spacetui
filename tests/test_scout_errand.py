import os
import sys

os.environ.setdefault("ST_HQ", "X1-AF2-A1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tui.bots import ScoutBot


class ErrandFake:
    """A scout sitting at a market waypoint, away from the shipyard errand."""

    def __init__(self):
        self.wp = "X1-AF2-B6"
        self.status = "IN_ORBIT"
        self.nav_log = []

    def ship(self, sym):
        return {"nav": {"status": self.status, "waypointSymbol": self.wp,
                        "systemSymbol": "X1-AF2", "route": {}},
                "fuel": {"current": 400, "capacity": 400}}

    def orbit(self, sym):
        self.status = "IN_ORBIT"; return {}

    def dock(self, sym):
        self.status = "DOCKED"; return {}

    def set_flight_mode(self, sym, mode):
        return {}

    def navigate(self, sym, wp):
        self.nav_log.append(wp)
        self.wp = wp
        self.status = "IN_ORBIT"
        return {"nav": {"status": self.status, "waypointSymbol": wp}}

    def market(self, system, wp):
        return {"symbol": wp, "tradeGoods": [{"symbol": "FUEL", "type": "EXCHANGE",
                                              "purchasePrice": 10, "sellPrice": 5,
                                              "tradeVolume": 100}]}

    def waypoints(self, system, filters=None):
        # an unscanned market that would normally lure the scout away —
        # the errand must take priority over touring it
        return [{"symbol": "X1-AF2-C9", "type": "PLANET", "x": 1, "y": 1,
                 "traits": [{"symbol": "MARKETPLACE"}]}]


def test_scout_runs_the_errand_instead_of_touring_markets():
    c = ErrandFake()
    bot = ScoutBot(c, "ESOF-2", world=None, get_errand=lambda: "X1-AF2-A2",
                   on_log=lambda m: None)
    bot._await_arrival = lambda: c.ship("ESOF-2")  # skip the real transit wait
    orig_sleep = bot._sleep
    calls = {"n": 0}

    def sleep_once(secs):
        calls["n"] += 1
        if calls["n"] >= 2:
            bot.stop()
        orig_sleep(0)  # don't actually block the test

    bot._sleep = sleep_once
    bot.run()

    assert c.nav_log == ["X1-AF2-A2"]  # went straight to the yard, no market tour
    assert c.wp == "X1-AF2-A2"


def test_scout_dwells_once_at_the_errand_waypoint():
    c = ErrandFake()
    c.wp = "X1-AF2-A2"  # already there
    bot = ScoutBot(c, "ESOF-2", world=None, get_errand=lambda: "X1-AF2-A2",
                   on_log=lambda m: None)
    bot._await_arrival = lambda: c.ship("ESOF-2")
    bot._sleep = lambda secs: bot.stop()  # stop after the first dwell tick
    bot.run()
    assert c.nav_log == []  # already at the errand waypoint; no navigation
