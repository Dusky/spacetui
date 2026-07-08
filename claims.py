"""Route claims — keep multiple traders from piling onto the same deal.

When the orchestrator runs several traders, they all see the same "best route"
and would stampede it, crashing the price. A trader claims the route it's
working; others skip claimed routes and take the next best. Claims carry a TTL so
a stalled or crashed trader never locks a route forever.

In-process and thread-safe. Cross-process traders (separate ``st.py trade``
runs) aren't deconflicted — the orchestrator is the multi-trader path.
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_claims: dict[str, tuple[str, float]] = {}  # route_key -> (ship, expires_at)

DEFAULT_TTL = 300.0


def route_key(route: dict) -> str:
    return f"{route.get('good')}|{route.get('buy_wp')}|{route.get('sell_wp')}"


def _prune(now: float) -> None:
    for k, (_ship, exp) in list(_claims.items()):
        if exp <= now:
            del _claims[k]


def claimed_by_other(route: dict, ship: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with _lock:
        _prune(now)
        holder = _claims.get(route_key(route))
        return bool(holder and holder[0] != ship)


def claim(route: dict, ship: str, ttl: float = DEFAULT_TTL, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _lock:
        _claims[route_key(route)] = (ship, now + ttl)


def release(route: dict, ship: str) -> None:
    with _lock:
        k = route_key(route)
        if k in _claims and _claims[k][0] == ship:
            del _claims[k]


def pick_unclaimed(routes: list[dict], ship: str, now: float | None = None) -> dict | None:
    """First route in ``routes`` not currently claimed by another ship."""
    now = time.time() if now is None else now
    for r in routes:
        if not claimed_by_other(r, ship, now=now):
            return r
    return None


def clear() -> None:
    with _lock:
        _claims.clear()
