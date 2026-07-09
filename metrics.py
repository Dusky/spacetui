"""Pure performance aggregation for the mission-control view.

Every function takes plain rows (the kind ``store`` returns) and computes a KPI
or a derived alert, with no I/O — so they unit-test cleanly and the web Hub can
assemble them into ``/api/metrics`` and ``/api/alerts`` payloads.
"""

from __future__ import annotations

import datetime as dt


def profit_per_hour(credit_points, *, window_s: float = 3600.0) -> float:
    """Net-worth change per hour over the last ``window_s`` of the credit series.

    ``credit_points`` is ``[{observed_at, credits}]`` (any order). Returns 0 when
    there isn't enough history or time hasn't advanced.
    """
    pts = sorted(
        (p["observed_at"], p["credits"])
        for p in credit_points if p.get("observed_at") is not None
    )
    if len(pts) < 2:
        return 0.0
    now = pts[-1][0]
    windowed = [p for p in pts if p[0] >= now - window_s]
    if len(windowed) < 2:
        windowed = pts[-2:]
    (t0, c0), (t1, c1) = windowed[0], windowed[-1]
    span = t1 - t0
    if span <= 0:
        return 0.0
    return (c1 - c0) / span * 3600.0


def roi_per_ship(ship_pnl, assignments) -> list[dict]:
    """Merge per-ship spent/earned with each ship's current role.

    ``ship_pnl`` is ``[{ship, spent, earned}]``; ``assignments`` is
    ``[{ship, role}]``. Returns ``[{ship, role, spent, earned, net}]`` sorted by
    net profit, most profitable first.
    """
    roles = {a["ship"]: a.get("role") for a in assignments}
    out = []
    for r in ship_pnl:
        spent = r.get("spent", 0) or 0
        earned = r.get("earned", 0) or 0
        out.append({
            "ship": r.get("ship"),
            "role": roles.get(r.get("ship")),
            "spent": spent,
            "earned": earned,
            "net": earned - spent,
        })
    out.sort(key=lambda d: d["net"], reverse=True)
    return out


def role_contribution(roi_rows) -> dict[str, int]:
    """Total net profit grouped by role."""
    out: dict[str, int] = {}
    for r in roi_rows:
        role = r.get("role") or "?"
        out[role] = out.get(role, 0) + (r.get("net", 0) or 0)
    return out


def fleet_utilization(total_ships: int, active_ships: int) -> dict:
    """Share of the fleet that currently has a bot on it."""
    active = max(0, min(active_ships, total_ships))
    idle = max(0, total_ships - active)
    pct = (active / total_ships * 100.0) if total_ships else 0.0
    return {"total": total_ships, "active": active, "idle": idle, "pct": round(pct, 1)}


def _seconds_until(ts: str | None, now: dt.datetime | None = None):
    if not ts:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t - now).total_seconds()


def derive_alerts(ships, active_symbols, pph, contracts, *,
                  orch_running: bool = False, api_blocked_for: float = 0.0,
                  now: dt.datetime | None = None) -> list[dict]:
    """Turn live state into a ranked list of ``{level, msg}`` conditions worth
    the operator's attention (fuel-stranded ships, idle hulls, a falling net
    worth, contract deadlines, and API-limit pressure)."""
    active = set(active_symbols or ())
    alerts: list[dict] = []

    for s in ships:
        fuel = s.get("fuel", {}) or {}
        nav = s.get("nav", {}) or {}
        if (fuel.get("capacity", 0) and fuel.get("current", 0) == 0
                and nav.get("status") != "IN_TRANSIT"):
            alerts.append({"level": "warn",
                           "msg": f"{s.get('symbol')} stranded (no fuel) at {nav.get('waypointSymbol', '?')}"})

    if orch_running:
        for s in ships:
            if s.get("symbol") not in active:
                alerts.append({"level": "info", "msg": f"{s.get('symbol')} idle (no bot assigned)"})

    if pph < 0:
        alerts.append({"level": "warn", "msg": f"net worth falling ({int(pph):,}/hr)"})

    for c in contracts:
        if c.get("accepted") and not c.get("fulfilled"):
            secs = _seconds_until((c.get("terms", {}) or {}).get("deadline"), now)
            if secs is not None and secs < 3600:
                mins = max(0, int(secs // 60))
                alerts.append({"level": "warn",
                               "msg": f"contract {str(c.get('id', ''))[:8]} due in {mins}m"})

    if api_blocked_for > 0.5:
        alerts.append({"level": "info",
                       "msg": f"API rate-limited, easing off ({api_blocked_for:.0f}s)"})

    order = {"warn": 0, "info": 1}
    alerts.sort(key=lambda a: order.get(a["level"], 2))
    return alerts
