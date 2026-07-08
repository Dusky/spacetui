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
