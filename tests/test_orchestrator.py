import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from orchestrator import Orchestrator, classify_ship


def ship(symbol, mounts=(), cargo_cap=0, waypoint=None):
    s = {
        "symbol": symbol,
        "mounts": [{"symbol": m} for m in mounts],
        "cargo": {"capacity": cargo_cap, "units": 0, "inventory": []},
    }
    if waypoint:
        s["nav"] = {"waypointSymbol": waypoint}
    return s


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


def test_reinvest_buys_when_ship_present_at_yard(monkeypatch):
    monkeypatch.setattr(config, "HQ", "X1-HQ-A1")
    fleet = [ship("A", mounts=["MOUNT_MINING_LASER_I"], cargo_cap=30, waypoint="X1-HQ-YARD")]
    client = FakeClient(fleet, credits=250_000, price=60_000)
    orch, _ = make_orch(client, expand_ship_type="SHIP_MINING_DRONE", credit_buffer=50_000)
    orch._maybe_expand(fleet)
    assert client.purchases == [("SHIP_MINING_DRONE", "X1-HQ-YARD")]
    assert orch._buy_errand is None


def test_reinvest_respects_buffer(monkeypatch):
    monkeypatch.setattr(config, "HQ", "X1-HQ-A1")
    fleet = [ship("A", cargo_cap=30, waypoint="X1-HQ-YARD")]
    client = FakeClient(fleet, credits=80_000, price=60_000)  # only 30k over buffer
    orch, _ = make_orch(client, expand_ship_type="SHIP_MINING_DRONE", credit_buffer=50_000)
    orch._maybe_expand(fleet)
    assert client.purchases == []  # can't afford without dipping below reserve


def test_no_expand_without_ship_type():
    fleet = [ship("A", cargo_cap=30, waypoint="X1-HQ-YARD")]
    client = FakeClient(fleet, credits=10_000_000)
    orch, _ = make_orch(client)  # expand_ship_type=None
    orch._maybe_expand(fleet)
    assert client.purchases == []


def test_reinvest_dispatches_scout_instead_of_spamming_a_doomed_purchase(monkeypatch):
    # no ship is at the yard -> SpaceTraders would reject the purchase every
    # tick ("must have at least one ship available at the purchase location").
    # We must never call purchase_ship here; dispatch the scout instead.
    monkeypatch.setattr(config, "HQ", "X1-HQ-A1")
    fleet = [ship("A", mounts=["MOUNT_MINING_LASER_I"], cargo_cap=30, waypoint="X1-HQ-B9")]
    client = FakeClient(fleet, credits=250_000, price=60_000)
    orch, _ = make_orch(client, expand_ship_type="SHIP_MINING_DRONE", credit_buffer=50_000)
    orch._maybe_expand(fleet)
    assert client.purchases == []
    assert orch._buy_errand == "X1-HQ-YARD"
    # calling again (next tick) still must not purchase or re-log the dispatch
    orch._maybe_expand(fleet)
    assert client.purchases == []
    # once a ship arrives at the yard, the pending errand is fulfilled
    fleet.append(ship("B", cargo_cap=30, waypoint="X1-HQ-YARD"))
    orch._maybe_expand(fleet)
    assert client.purchases == [("SHIP_MINING_DRONE", "X1-HQ-YARD")]
    assert orch._buy_errand is None


def test_miner_adopts_active_contract():
    fleet = [ship("M", mounts=["MOUNT_MINING_LASER_I"], cargo_cap=30)]
    orch, _ = make_orch(FakeClient(fleet), auto_contracts=True)

    class MgrStub:
        active_contract_id = "ctr-9"

    orch._contract_mgr = MgrStub()
    bot = orch._make_bot("M", "miner")
    assert bot.get_contract() == "ctr-9"


def test_miner_no_contract_without_manager():
    fleet = [ship("M", mounts=["MOUNT_MINING_LASER_I"], cargo_cap=30)]
    orch, _ = make_orch(FakeClient(fleet))
    bot = orch._make_bot("M", "miner")
    assert bot.get_contract() is None


def test_default_goal_is_grow():
    orch, _ = make_orch(FakeClient([]))
    assert orch.goal == "grow"


def test_invalid_goal_falls_back_to_grow():
    orch, _ = make_orch(FakeClient([]), goal="bogus")
    assert orch.goal == "grow"


def test_construct_goal_assigns_one_supplier():
    fleet = [
        ship("HAUL1", cargo_cap=60),   # trader-capable
        ship("HAUL2", cargo_cap=60),   # trader-capable
        ship("M", mounts=["MOUNT_MINING_LASER_I"], cargo_cap=30),  # miner
    ]
    orch, deployed = make_orch(
        FakeClient(fleet), goal="construct", construct_waypoint="X1-HQ-GATE")
    for s in fleet:
        orch._deploy(s)
    roles = dict(deployed)
    assert list(roles.values()).count("construct") == 1  # exactly one supplier
    assert roles["M"] == "miner"                          # miner still funds the build
    assert getattr(orch.bots["M"], "_role") == "miner"


def test_explore_goal_enables_scout_charting():
    orch, _ = make_orch(FakeClient([]), goal="explore")
    bot = orch._make_bot("PROBE", "scout")
    assert bot.explore is True


class _DeadThread:
    def is_alive(self):
        return False


def test_reap_dead_redeploys_halted_bot():
    fleet = [ship("A", cargo_cap=40)]
    orch, _ = make_orch(FakeClient(fleet))
    orch._deploy(fleet[0])
    assert "A" in orch.bots
    # simulate the bot's thread having died (halted on some error)
    orch._threads["A"] = _DeadThread()
    orch._reap_dead()
    assert "A" not in orch.bots  # forgotten, so the next tick redeploys it

