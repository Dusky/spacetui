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

    def waypoints(self, system, filters=None):
        self.calls.append(("waypoints", system))
        allwp = [
            {"symbol": "X1-AF2-A1", "type": "PLANET", "x": 10, "y": 5,
             "traits": [{"symbol": "MARKETPLACE"}]},
            {"symbol": "X1-AF2-B2", "type": "ASTEROID_FIELD", "x": -8, "y": 12,
             "traits": [{"symbol": "MINERAL_DEPOSITS"}]},
            {"symbol": "X1-AF2-YARD", "type": "ORBITAL_STATION", "x": 0, "y": 0,
             "traits": [{"symbol": "SHIPYARD"}]},
        ]
        if filters and filters.get("traits"):
            want = filters["traits"]
            return [w for w in allwp
                    if any(t["symbol"] == want for t in w["traits"])]
        if filters and filters.get("type"):
            return [w for w in allwp if w["type"] == filters["type"]]
        return allwp

    def shipyard(self, system, wp):
        return {"symbol": wp, "ships": [
            {"type": "SHIP_MINING_DRONE", "purchasePrice": 60000},
            {"type": "SHIP_LIGHT_HAULER", "purchasePrice": 180000}]}


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


def test_stream_pubsub(app):
    hub = app.hub
    q = hub.subscribe()
    hub.log("hello world")
    item = q.get(timeout=1)
    assert item["event"] == "log" and item["data"]["msg"] == "hello world"
    hub.refresh()  # publishes a state snapshot
    seen = []
    while True:
        try:
            seen.append(q.get(timeout=0.3))
        except Exception:
            break
    assert any(e["event"] == "state" for e in seen)
    hub.unsubscribe(q)
    assert q not in hub._subs


def test_control_actions_push_state(app):
    hub = app.hub
    q = hub.subscribe()
    app.test_client().post("/api/bot", json={"ship": "MDOE-1", "kind": "trade"})
    events = []
    while True:
        try:
            events.append(q.get(timeout=0.3))
        except Exception:
            break
    assert any(e["event"] == "state" for e in events)  # UI updates without waiting for a poll
    hub.stop_bot("MDOE-1")
    hub.unsubscribe(q)


class SetupFactory:
    """A client_factory whose instances validate a known-good token."""
    AGENT = {"symbol": "NOVA", "headquarters": "X1-ZZ-A1", "credits": 175000, "shipCount": 2}

    def __init__(self, token=None):
        self.token = token

    def my_agent(self):
        from api import ApiError
        if self.token == "good-token":
            return dict(self.AGENT)
        raise ApiError(401, "invalid token")

    def contracts(self):
        return []

    def ships(self):
        return []


def test_setup_flow(monkeypatch, tmp_path):
    import config
    import onboarding
    monkeypatch.setattr(config, "AGENT_TOKEN", "")            # start unconfigured
    monkeypatch.setattr(onboarding, "ENV_PATH", tmp_path / ".env")

    app = create_app(client=None, start_poller=False, client_factory=SetupFactory)
    c = app.test_client()
    assert c.get("/api/state").get_json() == {"configured": False}

    # a bad token is rejected, still unconfigured
    bad = c.post("/api/setup", json={"mode": "token", "token": "nope"})
    assert bad.status_code == 400
    assert c.get("/api/state").get_json()["configured"] is False

    # a good token configures the hub and writes .env
    ok = c.post("/api/setup", json={"mode": "token", "token": "good-token"})
    assert ok.get_json()["ok"] is True
    assert c.get("/api/state").get_json()["configured"] is True
    assert "ST_AGENT_TOKEN=good-token" in (tmp_path / ".env").read_text()


def test_state_has_configured_flag(app):
    assert app.test_client().get("/api/state").get_json()["configured"] is True


def test_system_map_endpoint_and_cache(app):
    c = app.test_client()
    r = c.get("/api/system/X1-AF2").get_json()
    assert r["system"] == "X1-AF2"
    syms = {w["symbol"] for w in r["waypoints"]}
    assert syms == {"X1-AF2-A1", "X1-AF2-B2", "X1-AF2-YARD"}
    a1 = next(w for w in r["waypoints"] if w["symbol"] == "X1-AF2-A1")
    assert a1["x"] == 10 and "MARKETPLACE" in a1["traits"]
    n = sum(1 for x in app.hub.c.calls if x[0] == "waypoints")
    c.get("/api/system/X1-AF2")  # second call is cached
    assert sum(1 for x in app.hub.c.calls if x[0] == "waypoints") == n


def test_shiptypes_endpoint(app):
    r = app.test_client().get("/api/shiptypes").get_json()
    types = {x["type"] for x in r}
    assert {"SHIP_MINING_DRONE", "SHIP_LIGHT_HAULER"} <= types
    drone = next(x for x in r if x["type"] == "SHIP_MINING_DRONE")
    assert drone["price"] == 60000


def test_price_series_endpoint(app):
    conn = store.connect()
    for buy, sell in [(100, 90), (110, 100)]:
        store.record_market({"symbol": "X1-AF2-A1", "tradeGoods": [
            {"symbol": "GOLD", "type": "EXCHANGE", "purchasePrice": buy,
             "sellPrice": sell, "tradeVolume": 10}]}, conn=conn)
    r = app.test_client().get("/api/price/GOLD").get_json()
    assert [x["sell_price"] for x in r] == [90, 100]


def test_stats_credits_have_timestamps(app):
    store.record_credits(123456, 1)
    credits = app.test_client().get("/api/stats").get_json()["credits"]
    assert credits and "t" in credits[0] and "v" in credits[0]


def test_metrics_endpoint(app):
    conn = store.connect()
    store.record_trade("buy", "IRON", 40, 100, 4000, ship="MDOE-1", conn=conn)
    store.record_trade("sell", "IRON", 40, 240, 9600, ship="MDOE-1", conn=conn)
    store.record_ship_assignment("MDOE-1", "trader", conn=conn)
    m = app.test_client().get("/api/metrics").get_json()
    assert "credits_per_hour" in m and "utilization" in m and "api" in m
    roi = {r["ship"]: r for r in m["roi"]}
    assert roi["MDOE-1"]["net"] == 5600
    assert roi["MDOE-1"]["role"] == "trader"


def test_alerts_endpoint_returns_list(app):
    a = app.test_client().get("/api/alerts").get_json()
    assert isinstance(a, list)


def test_orchestrator_start_with_goal(app):
    c = app.test_client()
    c.post("/api/orchestrator", json={
        "action": "start", "goal": "construct", "construct_waypoint": "X1-AF2-GATE"})
    orch = app.hub.orchestrator
    assert orch.goal == "construct"
    assert orch.construct_waypoint == "X1-AF2-GATE"
    cfg = app.hub.snapshot()["orchestrator"]["config"]
    assert cfg["goal"] == "construct"
    c.post("/api/orchestrator", json={"action": "stop"})
    orch._sup.join(timeout=2)


def test_alert_event_reaches_subscriber(app):
    hub = app.hub
    hub._ships = [{"symbol": "DEAD-1", "fuel": {"current": 0, "capacity": 400},
                   "nav": {"status": "DOCKED", "waypointSymbol": "X1-AF2-A1"}}]
    hub._last_alerts = set()
    q = hub.subscribe()
    hub._push_alerts()
    seen = []
    while True:
        try:
            seen.append(q.get(timeout=0.3))
        except Exception:
            break
    hub.unsubscribe(q)
    assert any(e["event"] == "alert" and "stranded" in e["data"]["msg"] for e in seen)


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
