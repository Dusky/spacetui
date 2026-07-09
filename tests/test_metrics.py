import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from metrics import (
    derive_alerts,
    fleet_utilization,
    profit_per_hour,
    roi_per_ship,
    role_contribution,
)

NOW = dt.datetime(2026, 7, 9, 12, 0, tzinfo=dt.timezone.utc)


def test_profit_per_hour_linear():
    # +6000 credits over 2 hours -> 3000/hr
    base = 1_000_000.0
    pts = [
        {"observed_at": base, "credits": 100_000},
        {"observed_at": base + 3600, "credits": 103_000},
        {"observed_at": base + 7200, "credits": 106_000},
    ]
    assert profit_per_hour(pts, window_s=10_000) == 3000.0


def test_profit_per_hour_insufficient_data():
    assert profit_per_hour([]) == 0.0
    assert profit_per_hour([{"observed_at": 1.0, "credits": 5}]) == 0.0


def test_roi_per_ship_merges_role_and_sorts():
    pnl = [
        {"ship": "A", "spent": 4000, "earned": 9600},   # +5600
        {"ship": "B", "spent": 5000, "earned": 3000},   # -2000
    ]
    assigns = [{"ship": "A", "role": "trader"}, {"ship": "B", "role": "miner"}]
    rows = roi_per_ship(pnl, assigns)
    assert [r["ship"] for r in rows] == ["A", "B"]      # sorted by net desc
    assert rows[0] == {"ship": "A", "role": "trader", "spent": 4000,
                       "earned": 9600, "net": 5600}
    assert rows[1]["net"] == -2000


def test_role_contribution_sums_net():
    rows = [
        {"role": "trader", "net": 5600},
        {"role": "trader", "net": 400},
        {"role": "miner", "net": -2000},
    ]
    assert role_contribution(rows) == {"trader": 6000, "miner": -2000}


def test_fleet_utilization():
    assert fleet_utilization(4, 3) == {"total": 4, "active": 3, "idle": 1, "pct": 75.0}
    assert fleet_utilization(0, 0)["pct"] == 0.0


def ship(sym, fuel_cur, fuel_cap, status="DOCKED", wp="X1-A-1"):
    return {"symbol": sym, "fuel": {"current": fuel_cur, "capacity": fuel_cap},
            "nav": {"status": status, "waypointSymbol": wp}}


def test_alerts_flag_stranded_idle_and_falling():
    ships = [
        ship("STUCK", 0, 400),                         # out of fuel, docked -> stranded
        ship("MOVING", 0, 400, status="IN_TRANSIT"),   # 0 fuel but in transit -> fine
        ship("WORKER", 380, 400),                      # has fuel, has a bot -> fine
    ]
    alerts = derive_alerts(ships, active_symbols={"WORKER"}, pph=-1500,
                           contracts=[], orch_running=True, now=NOW)
    msgs = " | ".join(a["msg"] for a in alerts)
    assert "STUCK stranded" in msgs
    assert "STUCK idle" in msgs          # stranded ship also has no bot
    assert "MOVING idle" in msgs         # in-transit but not in the active set
    assert "WORKER" not in msgs          # working ship raises nothing
    assert "net worth falling" in msgs


def test_alerts_contract_deadline_risk():
    con = [{"id": "abcd1234", "accepted": True, "fulfilled": False,
            "terms": {"deadline": "2026-07-09T12:30:00Z"}}]  # 30m out
    alerts = derive_alerts([], active_symbols=set(), pph=100, contracts=con, now=NOW)
    assert any("due in 30m" in a["msg"] for a in alerts)


def test_alerts_quiet_when_healthy():
    ships = [ship("A", 380, 400)]
    assert derive_alerts(ships, active_symbols={"A"}, pph=5000,
                         contracts=[], orch_running=True, now=NOW) == []
