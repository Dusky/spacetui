import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts import (
    ContractManager,
    contract_reward,
    estimate_contract_cost,
    is_winnable,
    pick_contract_action,
)

NOW = dt.datetime(2026, 7, 8, tzinfo=dt.timezone.utc)


def contract(cid, accepted=False, fulfilled=False, deadline=None):
    return {"id": cid, "accepted": accepted, "fulfilled": fulfilled,
            "deadlineToAccept": deadline,
            "terms": {"deliver": [{"tradeSymbol": "IRON_ORE", "unitsRequired": 100,
                                   "unitsFulfilled": 0, "destinationSymbol": "X1-A-B"}]}}


def test_work_accepted_contract():
    assert pick_contract_action([contract("c1", accepted=True)], now=NOW) == ("work", "c1")


def test_accept_pending_contract():
    assert pick_contract_action([contract("c2")], now=NOW) == ("accept", "c2")


def test_skip_expired_pending():
    expired = contract("c3", deadline="2026-07-01T00:00:00Z")
    assert pick_contract_action([expired], now=NOW) == ("negotiate", None)


def test_negotiate_when_none():
    assert pick_contract_action([], now=NOW) == ("negotiate", None)
    assert pick_contract_action([contract("done", fulfilled=True)], now=NOW) == ("negotiate", None)


def paying_contract(cid, on_accept, on_fulfil, need=100, deadline=None):
    c = contract(cid, deadline=deadline)
    c["terms"]["payment"] = {"onAccepted": on_accept, "onFulfilled": on_fulfil}
    c["terms"]["deliver"][0]["unitsRequired"] = need
    return c


def test_contract_reward_and_cost_math():
    c = paying_contract("p1", 10_000, 90_000, need=100)
    assert contract_reward(c) == 100_000
    # 100 units @ 200c each = 20_000 estimated buy cost
    assert estimate_contract_cost(c, lambda g: 200) == 20_000
    # unknown price -> optimistic zero
    assert estimate_contract_cost(c, lambda g: None) == 0


def test_winnable_declines_unprofitable_contract():
    c = paying_contract("p2", 0, 5_000, need=100)     # pays 5k
    # materials would cost 100 * 200 = 20k -> a loss, so decline
    assert is_winnable(c, now=NOW, price_of=lambda g: 200) is False
    # with cheap materials (50c -> 5k cost) it just breaks even and is winnable
    assert is_winnable(c, now=NOW, price_of=lambda g: 50) is True


def test_pick_skips_unwinnable_and_negotiates():
    bad = paying_contract("bad", 0, 5_000, need=100)
    action, cid = pick_contract_action([bad], now=NOW, price_of=lambda g: 200)
    assert action == "negotiate"


class FakeClient:
    """Starts with nothing, negotiates one contract, then holds it."""
    def __init__(self):
        self.state = []
        self.calls = []

    def contracts(self):
        return list(self.state)

    def negotiate_contract(self, ship):
        self.calls.append(("negotiate", ship))
        self.state = [contract("new-1")]
        return {"contract": self.state[0]}

    def accept_contract(self, cid):
        self.calls.append(("accept", cid))
        for c in self.state:
            if c["id"] == cid:
                c["accepted"] = True
        return {}


def test_manager_negotiates_then_accepts():
    c = FakeClient()
    mgr = ContractManager(c, "SHIP-1")
    # one iteration of the loop body (no threads): negotiate + accept
    action, cid = pick_contract_action(c.contracts())
    assert action == "negotiate"
    data = c.negotiate_contract("SHIP-1")
    new = data["contract"]["id"]
    c.accept_contract(new)
    mgr.active_contract_id = new
    assert ("negotiate", "SHIP-1") in c.calls
    assert ("accept", "new-1") in c.calls
    # now the same contract is accepted -> "work"
    assert pick_contract_action(c.contracts())[0] == "work"
    assert mgr.active_contract_id == "new-1"

class DeadTokenClient:
    """Every call fails with the server-reset invalid-token error."""

    def contracts(self):
        from api import ApiError
        raise ApiError(4113, "Failed to parse token. Token reset_date does not "
                             "match the server. ... re-register your agent. "
                             "Expected: 2026-07-12, Actual: 2026-07-05")


def test_manager_stops_instead_of_retrying_a_dead_token():
    import threading

    c = DeadTokenClient()
    logs = []
    mgr = ContractManager(c, "SHIP-1", tick=0, on_log=logs.append)
    t = threading.Thread(target=mgr.run, daemon=True)
    t.start()
    t.join(timeout=5)

    assert not t.is_alive(), "contract manager kept looping on a dead token"
    assert mgr.cancelled
    assert any("FATAL" in m and "re-register" in m for m in logs)
    # exactly one failure logged -- not a retry-forever spam
    assert sum("contract step failed" in m for m in logs) == 0
