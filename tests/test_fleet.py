import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet import (
    FleetManager,
    find_offer,
    pick_expansion_type,
    plan_expansion,
    ship_type_role,
)


def test_plan_expansion_respects_buffer():
    # 200k credits, keep 50k reserve, ships cost 60k -> can buy 2 (120k of 150k)
    assert plan_expansion(200_000, 1, unit_price=60_000, credit_buffer=50_000) == 2


def test_plan_expansion_zero_when_below_buffer():
    assert plan_expansion(80_000, 1, unit_price=60_000, credit_buffer=50_000) == 0


def test_plan_expansion_respects_max_ships():
    # affordable count is high, but cap leaves room for only 1 more
    assert plan_expansion(1_000_000, 9, unit_price=10_000, max_ships=10) == 1
    assert plan_expansion(1_000_000, 10, unit_price=10_000, max_ships=10) == 0


def test_plan_expansion_guards_bad_price():
    assert plan_expansion(1_000_000, 0, unit_price=0) == 0
    assert plan_expansion(1_000_000, 0, unit_price=-5) == 0


def test_ship_type_role_classification():
    assert ship_type_role("SHIP_MINING_DRONE") == "miner"
    assert ship_type_role("SHIP_SIPHON_DRONE") == "miner"
    assert ship_type_role("SHIP_PROBE") == "scout"
    assert ship_type_role("SHIP_LIGHT_HAULER") == "trader"
    assert ship_type_role("SHIP_COMMAND_FRIGATE") == "trader"


TYPES = [
    {"type": "SHIP_MINING_DRONE", "price": 60_000},
    {"type": "SHIP_LIGHT_HAULER", "price": 180_000},
    {"type": "SHIP_PROBE", "price": 25_000},
]


def test_expansion_buys_scout_first_when_missing():
    roster = {"A": "miner", "B": "miner"}
    assert pick_expansion_type(roster, TYPES) == "SHIP_PROBE"


def test_expansion_buys_hauler_when_miners_outnumber_traders():
    # a scout already exists; 4 miners but 1 trader -> need more hauling
    roster = {"A": "miner", "B": "miner", "C": "miner", "D": "miner",
              "E": "trader", "S": "scout"}
    assert pick_expansion_type(roster, TYPES) == "SHIP_LIGHT_HAULER"


def test_expansion_defaults_to_miner_when_balanced():
    roster = {"A": "miner", "T": "trader", "S": "scout"}
    assert pick_expansion_type(roster, TYPES) == "SHIP_MINING_DRONE"


def test_expansion_none_when_nothing_for_sale():
    assert pick_expansion_type({"A": "miner"}, []) is None


class FakeClient:
    def __init__(self, credits, price=60_000, ships=1):
        self._credits = credits
        self._ships = ships
        self.price = price
        self.purchases = []

    def my_agent(self):
        return {"credits": self._credits, "shipCount": self._ships}

    def waypoints(self, system, filters=None):
        return [{"symbol": "X1-SIM-YARD"}]

    def shipyard(self, system, wp):
        return {"symbol": wp, "ships": [{"type": "SHIP_MINING_DRONE", "purchasePrice": self.price}]}

    def purchase_ship(self, ship_type, wp):
        self.purchases.append((ship_type, wp))
        self._credits -= self.price
        self._ships += 1
        return {"ship": {"symbol": f"SIM-{self._ships}"}, "transaction": {"price": self.price}}


def test_find_offer_locates_shipyard():
    wp, price = find_offer(FakeClient(500_000), "X1-SIM", "SHIP_MINING_DRONE")
    assert wp == "X1-SIM-YARD"
    assert price == 60_000


def test_fleet_manager_buys_until_buffer_hit():
    # 250k, keep 50k reserve, 60k each -> 200k/60k = 3 ships, then stop
    c = FakeClient(250_000, price=60_000, ships=0)
    logs, bought_syms = [], []
    fm = FleetManager(
        c, "SHIP_MINING_DRONE", system="X1-SIM", credit_buffer=50_000,
        on_log=logs.append, on_buy=bought_syms.append,
    )
    n = fm.run()
    assert n == 3
    assert len(c.purchases) == 3
    assert len(bought_syms) == 3
    assert c._credits >= 50_000  # never dipped below the reserve


def test_fleet_manager_stops_at_max_ships():
    c = FakeClient(10_000_000, price=1_000, ships=8)
    fm = FleetManager(c, "SHIP_MINING_DRONE", system="X1-SIM", credit_buffer=0, max_ships=10)
    assert fm.run() == 2
    assert c._ships == 10
