"""Shared, cached world model.

Every bot used to independently call ``waypoints`` / ``market`` / ``shipyard``
every loop, so a fleet of N ships cost N× the discovery traffic and hit the rate
limiter that much sooner. ``World`` is the single read-through/write-through
front for that data: it caches each system's waypoints, live markets and
shipyard listings in memory (with a TTL), coalesces concurrent lookups of the
same key into one API call (single-flight), and persists to ``store`` so a
restarted process starts *warm* instead of blind.

All real API calls still funnel through the shared ``ratelimit.LIMITER`` (inside
the ``Client``), so the win here is simply making *fewer* of them: the more bots
share one ``World``, the bigger the saving.
"""

from __future__ import annotations

import threading
import time

import store
from api import ApiError


def _norm_wp(w: dict) -> dict:
    """Reduce a raw waypoint payload to the fields the app cares about."""
    return {
        "symbol": w.get("symbol"),
        "type": w.get("type"),
        "x": w.get("x", 0),
        "y": w.get("y", 0),
        "traits": [
            t.get("symbol") if isinstance(t, dict) else t
            for t in (w.get("traits") or [])
        ],
    }


class World:
    def __init__(self, client, *, wp_ttl: float = 600.0,
                 market_ttl: float = 120.0, yard_ttl: float = 600.0):
        self.c = client
        self.wp_ttl = wp_ttl
        self.market_ttl = market_ttl
        self.yard_ttl = yard_ttl
        self._cache: dict = {}          # key -> (data, ts)
        self._locks: dict = {}          # key -> Lock (single-flight per key)
        self._master = threading.Lock()

    # -- cache plumbing ----------------------------------------------------
    def _key_lock(self, key) -> threading.Lock:
        with self._master:
            lk = self._locks.get(key)
            if lk is None:
                lk = self._locks[key] = threading.Lock()
            return lk

    def _cached(self, key, ttl: float):
        with self._master:
            hit = self._cache.get(key)
        if hit is not None and time.time() - hit[1] < ttl:
            return hit[0]
        return None

    def _put(self, key, data) -> None:
        with self._master:
            self._cache[key] = (data, time.time())

    # -- waypoints ---------------------------------------------------------
    def get_waypoints(self, system: str, max_age: float | None = None) -> list[dict]:
        """All normalized waypoints in a system (cached; warm from ``store``)."""
        ttl = self.wp_ttl if max_age is None else max_age
        key = ("wp", system)
        hit = self._cached(key, ttl)
        if hit is not None:
            return hit
        with self._key_lock(key):
            hit = self._cached(key, ttl)  # double-check after waiting on the lock
            if hit is not None:
                return hit
            warm = store.waypoints_of(system, max_age_s=ttl)
            if warm:
                self._put(key, warm)
                return warm
            wps = [_norm_wp(w) for w in self.c.waypoints(system)]
            store.record_waypoints(system, wps)
            self._put(key, wps)
            return wps

    def find_waypoints(self, system: str, *, trait: str | None = None,
                       type: str | None = None, max_age: float | None = None) -> list[dict]:
        """Waypoints in a system filtered locally from the cached full list —
        replaces per-loop filtered API calls (``filters={"traits": ...}``)."""
        out = []
        for w in self.get_waypoints(system, max_age=max_age):
            if trait is not None and trait not in (w.get("traits") or []):
                continue
            if type is not None and w.get("type") != type:
                continue
            out.append(w)
        return out

    # -- markets -----------------------------------------------------------
    def get_market(self, system: str, waypoint: str,
                   max_age: float | None = None) -> dict | None:
        """Live market payload for a waypoint (cached; write-through to ``store``).

        Pass ``max_age=0`` to force a fresh read (e.g. right after a trade that
        moved the price). Returns ``None`` if the waypoint exposes no market.
        """
        ttl = self.market_ttl if max_age is None else max_age
        key = ("mkt", waypoint)
        hit = self._cached(key, ttl)
        if hit is not None:
            return hit
        with self._key_lock(key):
            hit = self._cached(key, ttl)
            if hit is not None:
                return hit
            try:
                m = self.c.market(system, waypoint)
            except ApiError:
                return None
            store.record_market(m)
            self._put(key, m)
            return m

    # -- shipyards ---------------------------------------------------------
    def get_shipyard(self, system: str, waypoint: str,
                     max_age: float | None = None) -> dict | None:
        ttl = self.yard_ttl if max_age is None else max_age
        key = ("yard", waypoint)
        hit = self._cached(key, ttl)
        if hit is not None:
            return hit
        with self._key_lock(key):
            hit = self._cached(key, ttl)
            if hit is not None:
                return hit
            try:
                y = self.c.shipyard(system, waypoint)
            except ApiError:
                return None
            self._put(key, y)
            return y

    def ship_types(self, system: str, max_age: float | None = None) -> list[dict]:
        """Ship types for sale in a system's shipyards, ``[{type, price}]``
        (folds the reinvest-dropdown lookup the web Hub used to own)."""
        ttl = self.yard_ttl if max_age is None else max_age
        key = ("types", system)
        hit = self._cached(key, ttl)
        if hit is not None:
            return hit
        with self._key_lock(key):
            hit = self._cached(key, ttl)
            if hit is not None:
                return hit
            found: dict[str, int | None] = {}
            for wp in self.find_waypoints(system, trait="SHIPYARD"):
                yard = self.get_shipyard(system, wp["symbol"], max_age=max_age)
                if not yard:
                    continue
                for offer in yard.get("ships", []):     # live listings w/ price
                    t = offer.get("type")
                    if t:
                        found[t] = offer.get("purchasePrice")
                for st in yard.get("shipTypes", []):    # types only (no ship present)
                    t = st.get("type") if isinstance(st, dict) else st
                    if t and t not in found:
                        found[t] = None
            out = [{"type": t, "price": found[t]} for t in sorted(found)]
            self._put(key, out)
            return out


# Process-wide instance, bound to the live client by whoever owns it (the web
# Hub, the CLI). Bots default to this when no explicit ``world=`` is passed.
WORLD: World | None = None


def bind(client, **kw) -> World:
    """Create the shared ``World`` for ``client`` and install it as the module
    default. Returns it so the caller can also hold a direct reference."""
    global WORLD
    WORLD = World(client, **kw)
    return WORLD
