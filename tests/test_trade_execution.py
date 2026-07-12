import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tui.bots as bots
from tui.bots import TraderBot


class SellClient:
    def __init__(self, prices, held=40):
        self.prices = prices
        self.i = 0
        self.held = held
        self.sold = []

    def cargo(self, ship):
        return {"inventory": [{"symbol": "IRON", "units": self.held}]}

    def sell(self, ship, good, n):
        ppu = self.prices[min(self.i, len(self.prices) - 1)]
        self.i += 1
        self.held -= n
        self.sold.append((n, ppu))
        return {"transaction": {"units": n, "pricePerUnit": ppu, "totalPrice": ppu * n}}


class BuyClient:
    def __init__(self, prices):
        self.prices = prices
        self.i = 0
        self.bought = []

    def purchase(self, ship, good, n):
        ppu = self.prices[min(self.i, len(self.prices) - 1)]
        self.i += 1
        self.bought.append((n, ppu))
        return {"transaction": {"units": n, "pricePerUnit": ppu, "totalPrice": ppu * n}}


def test_sell_stops_at_floor(monkeypatch):
    monkeypatch.setattr(bots.store, "record_trade", lambda *a, **k: None)
    c = SellClient(prices=[300, 300, 120], held=40)   # third chunk crashes below floor
    bot = TraderBot(c, "S")
    sold, revenue = bot._sell("IRON", per_tx=10, floor=200)
    assert sold == 30                 # sold 10@300, 10@300, 10@120 then stopped
    assert c.held == 10               # kept the rest for a better market


def test_buy_stops_at_ceiling(monkeypatch):
    monkeypatch.setattr(bots.store, "record_trade", lambda *a, **k: None)
    c = BuyClient(prices=[100, 100, 260])   # third chunk too expensive
    bot = TraderBot(c, "S")
    bought, spent = bot._buy("IRON", want=40, per_tx=10, ceiling=250)
    assert bought == 30               # bought 3 chunks, stopped when price hit ceiling
    assert len(c.bought) == 3


class AffordFake:
    """A ship docked at a single waypoint; buy price is fixed at 3714c/unit
    (matches the live MEDICINE route that triggered this bug)."""

    BUY_PPU = 3714
    SELL_PPU = 4367

    def __init__(self, credits):
        self.credits = credits
        self.status = "DOCKED"
        self.wp = "X1-A-1"
        self.held = 0
        self.purchases = []
        self.sold = []

    def ship(self, sym):
        return {"nav": {"status": self.status, "waypointSymbol": self.wp,
                        "systemSymbol": "X1-A", "route": {}},
                "cargo": {"units": self.held, "capacity": 40, "inventory": []},
                "fuel": {"current": 400, "capacity": 400}}

    def my_agent(self):
        return {"credits": self.credits}

    def orbit(self, sym):
        self.status = "IN_ORBIT"; return {}

    def dock(self, sym):
        self.status = "DOCKED"; return {}

    def refuel(self, sym):
        return {}

    def set_flight_mode(self, sym, mode):
        return {}

    def purchase(self, sym, good, n):
        self.purchases.append((good, n))
        self.credits -= n * self.BUY_PPU
        self.held += n
        return {"transaction": {"units": n, "pricePerUnit": self.BUY_PPU,
                                "totalPrice": n * self.BUY_PPU}}

    def cargo(self, sym):
        inv = [{"symbol": "MEDICINE", "units": self.held}] if self.held else []
        return {"inventory": inv}

    def sell(self, sym, good, n):
        self.sold.append((good, n))
        self.held -= n
        return {"transaction": {"units": n, "pricePerUnit": self.SELL_PPU,
                                "totalPrice": n * self.SELL_PPU}}

    def market(self, system, wp):
        return {"symbol": wp, "tradeGoods": []}


MEDICINE_ROUTE = {
    "good": "MEDICINE", "buy": 3714, "sell": 4367,
    "buy_wp": "X1-A-1", "sell_wp": "X1-A-1", "system": "X1-A",
    "volume": 20, "buy_supply": "ABUNDANT", "sell_supply": "ABUNDANT",
    "profit": 653,
}


def _quiet_store(monkeypatch):
    monkeypatch.setattr(bots.store, "record_trade", lambda *a, **k: None)
    monkeypatch.setattr(bots.store, "record_market", lambda *a, **k: 0)
    monkeypatch.setattr(bots.store, "price_series", lambda *a, **k: [])


def test_execute_never_attempts_an_unaffordable_purchase(monkeypatch):
    # 1000c can't buy even one unit at 3714c -- the bug bought nothing, failed,
    # and the caller retried the identical purchase every cycle forever
    _quiet_store(monkeypatch)
    c = AffordFake(credits=1000)
    bot = TraderBot(c, "S", world=None, on_log=lambda m: None)
    progress = bot._execute(dict(MEDICINE_ROUTE), c.ship("S"), capacity=40)
    assert progress is False
    assert c.purchases == []  # purchase() was never called with money we don't have


def test_execute_sizes_the_buy_to_affordable_credits(monkeypatch):
    # 50,000c affords 13 units at 3714c even though supply/cargo sizing wants more
    _quiet_store(monkeypatch)
    c = AffordFake(credits=50_000)
    bot = TraderBot(c, "S", world=None, on_log=lambda m: None)
    progress = bot._execute(dict(MEDICINE_ROUTE), c.ship("S"), capacity=40)
    assert progress is True
    assert c.purchases == [("MEDICINE", 13)]  # capped by credits, not the 40-unit hold
    assert c.sold == [("MEDICINE", 13)]


def test_run_backs_off_when_execute_makes_no_progress(monkeypatch):
    _quiet_store(monkeypatch)
    monkeypatch.setattr(bots.store, "best_routes",
                        lambda *a, **k: [dict(MEDICINE_ROUTE)])
    bots.claims.clear()
    c = AffordFake(credits=1000)  # can never afford this route
    bot = TraderBot(c, "S", loops=3, world=None, on_log=lambda m: None)
    sleeps = []
    bot._sleep = lambda secs: sleeps.append(secs)
    bot.run()
    # one 30s backoff per no-progress cycle -- not a tight retry loop
    assert sleeps == [30, 30, 30]
