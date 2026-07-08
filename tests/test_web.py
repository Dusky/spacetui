import os
import sys
import tempfile

os.environ.setdefault("ST_HQ", "X1-AF2-A1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import store

# isolate the process-global store DB (the server uses store without a conn arg)
store.DB_PATH = tempfile.mktemp(suffix=".db")
store._conn = None

from web.server import create_app

AGENT = {"symbol": "MDOE", "credits": 175000, "headquarters": "X1-AF2-A1",
         "startingFaction": "COSMIC", "shipCount": 2}


def ship(sym, cap, mounts=()):
    return {"symbol": sym, "frame": {"name": "Frigate"}, "registration": {"role": "COMMAND"},
            "mounts": [{"symbol": m} for m in mounts],
            "nav": {"status": "DOCKED", "waypointSymbol": "X1-AF2-A1", "systemSymbol": "X1-AF2"},
            "fuel": {"current": 380, "capacity": 400},
            "cargo": {"units": 0, "capacity": 40, "inventory": []}}


class FakeClient:
    def __init__(self):
        self.ships_list = [ship("MDOE-1", 40, ["MOUNT_MINING_LASER_I"]), ship("MDOE-2", 60)]
        self.calls = []

    def my_agent(self):
        return AGENT

    def ships(self):
        return self.ships_list

    def contracts(self):
        return []

    def orbit(self, s):
        self.calls.append(("orbit", s)); return {"nav": {}}


@pytest.fixture
def app():
    a = create_app(FakeClient(), start_poller=False)
    a.hub.refresh()  # one poll cycle to fill the cache
    return a


def test_state_served_from_cache(app):
    r = app.test_client().get("/api/state").get_json()
    assert r["agent"]["symbol"] == "MDOE"
    assert len(r["ships"]) == 2
    assert r["poll_ok"] is True
    assert r["orchestrator"]["running"] is False


def test_index_served(app):
    r = app.test_client().get("/")
    assert r.status_code == 200
    assert b"SPACETRADERS" in r.data


def test_stats_and_deals(app):
    conn = store.connect()  # default temp db from ST_DB_PATH
    store.record_market({"symbol": "X1-AF2-A1", "tradeGoods": [
        {"symbol": "IRON", "type": "EXCHANGE", "purchasePrice": 100, "sellPrice": 90, "tradeVolume": 50}]}, conn=conn)
    store.record_market({"symbol": "X1-AF2-B2", "tradeGoods": [
        {"symbol": "IRON", "type": "EXCHANGE", "purchasePrice": 250, "sellPrice": 240, "tradeVolume": 50}]}, conn=conn)
    store.record_trade("buy", "IRON", 40, 100, 4000, conn=conn)
    store.record_trade("sell", "IRON", 40, 240, 9600, conn=conn)
    c = app.test_client()
    deals = c.get("/api/deals?min_profit=1").get_json()
    assert any(d["good"] == "IRON" for d in deals)
    stats = c.get("/api/stats").get_json()
    assert stats["pnl"]["net"] == 5600


def test_orchestrator_toggle(app):
    c = app.test_client()
    started = c.post("/api/orchestrator", json={"action": "start"}).get_json()
    assert started["running"] is True
    assert app.hub.snapshot()["orchestrator"]["running"] is True
    c.post("/api/orchestrator", json={"action": "stop"})
    app.hub.orchestrator._sup.join(timeout=2)
    assert app.hub.snapshot()["orchestrator"]["running"] is False


def test_orchestrator_start_with_config(app):
    c = app.test_client()
    c.post("/api/orchestrator", json={
        "action": "start", "expand": "SHIP_MINING_DRONE",
        "credit_buffer": "250000", "max_ships": "8", "cross_system": True,
    })
    orch = app.hub.orchestrator
    assert orch.expand_ship_type == "SHIP_MINING_DRONE"
    assert orch.credit_buffer == 250000
    assert orch.max_ships == 8
    assert orch.cross_system is True
    cfg = app.hub.snapshot()["orchestrator"]["config"]
    assert cfg["expand"] == "SHIP_MINING_DRONE" and cfg["max_ships"] == 8
    c.post("/api/orchestrator", json={"action": "stop"})
    orch._sup.join(timeout=2)


def test_fleet_action_calls_client(app):
    r = app.test_client().post("/api/fleet", json={"ship": "MDOE-1", "action": "orbit"}).get_json()
    assert r["ok"] is True
    assert ("orbit", "MDOE-1") in app.hub.c.calls


def test_bot_start_and_stop(app):
    c = app.test_client()
    c.post("/api/bot", json={"ship": "MDOE-1", "kind": "trade"})
    assert "MDOE-1" in app.hub.bots
    c.post("/api/bot", json={"ship": "MDOE-1", "kind": "stop"})
    assert "MDOE-1" not in app.hub.bots


def test_token_auth_blocks_and_allows():
    app = create_app(FakeClient(), start_poller=False, token="sekret")
    app.hub.refresh()
    c = app.test_client()
    # no token -> API blocked, page shows the hint
    assert c.get("/api/state").status_code == 401
    assert c.get("/").status_code == 401
    # header token -> allowed
    assert c.get("/api/state", headers={"X-Auth-Token": "sekret"}).status_code == 200
    # a valid ?token on the page sets a cookie and redirects
    r = c.get("/?token=sekret")
    assert r.status_code == 302
    assert any(ck.startswith("st_token=sekret") for ck in r.headers.getlist("Set-Cookie"))
    # cookie now carries auth for subsequent API calls
    assert c.get("/api/state").status_code == 200
