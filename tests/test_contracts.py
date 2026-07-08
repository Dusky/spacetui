import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts import ContractManager, pick_contract_action

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
