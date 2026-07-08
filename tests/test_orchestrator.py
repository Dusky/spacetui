import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from orchestrator import Orchestrator, classify_ship


def ship(symbol, mounts=(), cargo_cap=0):
    return {
        "symbol": symbol,
        "mounts": [{"symbol": m} for m in mounts],
        "cargo": {"capacity": cargo_cap, "units": 0, "inventory": []},
    }


def test_classify_miner():
    assert classify_ship(ship("A", mounts=["MOUNT_MINING_LASER_II"], cargo_cap=30)) == "miner"
    assert classify_ship(ship("A", mounts=["MOUNT_SURVEYOR_I"], cargo_cap=30)) == "miner"


def test_classify_trader():
    assert classify_ship(ship("A", mounts=["MOUNT_SENSOR_ARRAY_I"], cargo_cap=40)) == "trader"


def test_classify_scout():
    assert classify_ship(ship("PROBE", mounts=["MOUNT_SENSOR_ARRAY_I"], cargo_cap=0)) == "scout"


class FakeClient:
    def __init__(self, ships, credits=500_000, price=60_000):
        self._ships = ships
        self._credits = credits
        self.price = price
        self.purchases = []

    def ships(self):
        return list(self._ships)

    def my_agent(self):
        return {"credits": self._credits, "shipCount": len(self._ships)}

    def waypoints(self, system, filters=None):
        return [{"symbol": "X1-HQ-YARD"}]

    def shipyard(self, system, wp):
        return {"symbol": wp, "ships": [{"type": "SHIP_MINING_DRONE", "purchasePrice": self.price}]}

    def purchase_ship(self, ship_type, wp):
        self.purchases.append((ship_type, wp))
        sym = f"NEW-{len(self.purchases)}"
        self._ships.append(ship(sym, mounts=["MOUNT_MINING_LASER_I"], cargo_cap=30))
        self._credits -= self.price
        return {"ship": {"symbol": sym}, "transaction": {"price": self.price}}


def make_orch(client, **kw):
    deployed = []
    orch = Orchestrator(
        client,
        spawn=lambda bot: None,  # don't run real bot threads
        on_deploy=lambda sym, role: deployed.append((sym, role)),
        **kw,
    )
    return orch, deployed


def test_deploys_one_bot_per_ship_by_role():
    fleet = [
        ship("CMD", mounts=["MOUNT_MINING_LASER_I"], cargo_cap=40),   # miner
        ship("HAUL", mounts=[], cargo_cap=60),                        # trader
        ship("PROBE", mounts=[], cargo_cap=0),                        # scout
    ]
    orch, deployed = make_orch(FakeClient(fleet))
    for s in fleet:
        orch._deploy(s)
    assert dict(deployed) == {"CMD": "miner", "HAUL": "trader", "PROBE": "scout"}
    # deploying again is idempotent (no duplicate bots)
    orch._deploy(fleet[0])
    assert len(orch.bots) == 3


def test_reap_removes_vanished_ships():
    fleet = [ship("A", cargo_cap=40), ship("B", cargo_cap=40)]
    orch, _ = make_orch(FakeClient(fleet))
    for s in fleet:
        orch._deploy(s)
    orch._reap({"A"})  # B was scrapped/sold
    assert set(orch.bots) == {"A"}


def test_reinvest_buys_when_affordable(monkeypatch):
    monkeypatch.setattr(config, "HQ", "X1-HQ-A1")
    fleet = [ship("A", mounts=["MOUNT_MINING_LASER_I"], cargo_cap=30)]
    client = FakeClient(fleet, credits=250_000, price=60_000)
    orch, _ = make_orch(client, expand_ship_type="SHIP_MINING_DRONE", credit_buffer=50_000)
    orch._maybe_expand(len(fleet))
    assert client.purchases == [("SHIP_MINING_DRONE", "X1-HQ-YARD")]


def test_reinvest_respects_buffer(monkeypatch):
    monkeypatch.setattr(config, "HQ", "X1-HQ-A1")
    fleet = [ship("A", cargo_cap=30)]
    client = FakeClient(fleet, credits=80_000, price=60_000)  # only 30k over buffer
    orch, _ = make_orch(client, expand_ship_type="SHIP_MINING_DRONE", credit_buffer=50_000)
    orch._maybe_expand(len(fleet))
    assert client.purchases == []  # can't afford without dipping below reserve


def test_no_expand_without_ship_type():
    client = FakeClient([ship("A", cargo_cap=30)], credits=10_000_000)
    orch, _ = make_orch(client)  # expand_ship_type=None
    orch._maybe_expand(1)
    assert client.purchases == []
