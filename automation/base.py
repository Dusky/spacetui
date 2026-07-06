from __future__ import annotations

import datetime as dt
import threading
import time

from api import ApiError, Client
from market import MarketDB
from navigation import Navigator, WaypointCache


class BotCancelled(Exception):
    pass


def _wait_seconds(ts: str | None) -> int:
    if not ts:
        return 0
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return max(0, int((t - dt.datetime.now(dt.timezone.utc)).total_seconds()))
    except ValueError:
        return 0


class BaseBot:
    """Cancellable worker-thread bot with shared ship plumbing.

    Subclasses implement `loop()`, which is called repeatedly until the bot is
    stopped, errors out, or (when max_cycles is set) the cycle budget runs out.
    """

    name = "bot"

    def __init__(
        self,
        client: Client,
        ship: str,
        *,
        cache: WaypointCache | None = None,
        db: MarketDB | None = None,
        on_log=None,
        on_status=None,
        max_cycles: int | None = None,
    ):
        self.c = client
        self.ship = ship
        self.cache = cache or WaypointCache(client)
        self.nav = Navigator(client, self.cache)
        self.db = db or MarketDB()
        self.on_log = on_log or (lambda m: None)
        self.on_status = on_status or (lambda **k: None)
        self.max_cycles = max_cycles
        self._cancel = threading.Event()

    # -- control -------------------------------------------------------------
    def stop(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _log(self, msg: str) -> None:
        self.on_log(f"{self.ship}  {msg}")

    def _status(self, mode: str = "", last: str = "") -> None:
        self.on_status(running=not self.cancelled, mode=mode, last=last)

    def _sleep(self, secs: float) -> None:
        end = time.time() + secs
        while time.time() < end:
            if self._cancel.is_set():
                raise BotCancelled()
            time.sleep(min(0.4, max(0.0, end - time.time())))

    # -- ship plumbing ---------------------------------------------------------
    def ship_state(self) -> dict:
        return self.c.ship(self.ship)

    def await_arrival(self) -> dict:
        s = self.ship_state()
        nav = s.get("nav", {})
        while nav.get("status") == "IN_TRANSIT":
            secs = _wait_seconds(nav.get("route", {}).get("arrival"))
            self._status(mode="transit", last=f"in transit {secs}s")
            self._sleep(secs + 1)
            s = self.ship_state()
            nav = s.get("nav", {})
        return s

    def await_cooldown(self) -> None:
        cd = self.c.cooldown(self.ship)
        while cd.get("remainingSeconds"):
            secs = cd["remainingSeconds"]
            self._status(mode="cooldown", last=f"cooldown {secs}s")
            self._sleep(secs + 1)
            cd = self.c.cooldown(self.ship)

    def dock(self, s: dict) -> dict:
        if s.get("nav", {}).get("status") != "DOCKED":
            s["nav"] = self.c.dock(self.ship)["nav"]
        return s

    def orbit(self, s: dict) -> dict:
        if s.get("nav", {}).get("status") != "IN_ORBIT":
            s["nav"] = self.c.orbit(self.ship)["nav"]
        return s

    def goto(self, s: dict, waypoint: str) -> dict:
        """Navigate (fuel-aware) and block until arrival."""
        if s.get("nav", {}).get("waypointSymbol") == waypoint and s["nav"].get(
            "status"
        ) != "IN_TRANSIT":
            return s
        self._status(mode="transit", last=f"→ {waypoint}")
        self.nav.goto(s, waypoint)
        s = self.await_arrival()
        self.nav.restore_cruise(s)
        return s

    def refuel_here(self, s: dict, *, threshold: float = 0.6) -> None:
        """Top up when below threshold and the waypoint sells fuel."""
        fuel = s.get("fuel", {})
        cap = fuel.get("capacity", 0)
        if not cap or fuel.get("current", 0) / cap >= threshold:
            return
        if self.nav.refuel_if_possible(s):
            f = s.get("fuel", {})
            self._log(f"refueled to {f.get('current')}/{f.get('capacity')}")

    def record_market_here(self, s: dict) -> None:
        """Snapshot prices at the current waypoint into the shared ledger."""
        nav = s.get("nav", {})
        wp = self.cache.waypoint(nav.get("systemSymbol", ""), nav.get("waypointSymbol", ""))
        if not wp or not any(t["symbol"] == "MARKETPLACE" for t in wp.get("traits", [])):
            return
        try:
            m = self.c.market(nav["systemSymbol"], nav["waypointSymbol"])
            if self.db.record_market(m):
                self._log(f"recorded market @ {nav['waypointSymbol']}")
        except ApiError:
            pass

    def sell_inventory(self, s: dict, *, keep: set[str] | None = None) -> int:
        """Sell everything in cargo at the current (docked) waypoint.
        Returns total credits received."""
        keep = keep or set()
        total = 0
        for item in list(s.get("cargo", {}).get("inventory", [])):
            if item["symbol"] in keep or item["units"] <= 0:
                continue
            try:
                data = self.c.sell(self.ship, item["symbol"], item["units"])
                t = data.get("transaction", {})
                total += t.get("totalPrice", 0)
                self._log(
                    f"sold {t.get('units', item['units'])} {item['symbol']} "
                    f"@ {t.get('pricePerUnit')} = {t.get('totalPrice')}c"
                )
                if "cargo" in data:
                    s["cargo"] = data["cargo"]
            except ApiError as e:
                self._log(f"can't sell {item['symbol']}: {e.message}")
        return total

    # -- lifecycle -------------------------------------------------------------
    def loop(self) -> None:
        raise NotImplementedError

    def run(self) -> None:
        self._log(f"{self.name} engaged")
        cycles = 0
        try:
            while not self._cancel.is_set():
                if self.max_cycles is not None and cycles >= self.max_cycles:
                    break
                cycles += 1
                self.loop()
        except BotCancelled:
            pass
        except ApiError as e:
            self._log(f"halted on API error {e.code}: {e.message}")
        except Exception as e:  # noqa: BLE001 - bots must never kill the app
            self._log(f"halted: {e!r}")
        finally:
            self._cancel.set()
            self._status(mode="stopped", last="idle")
            self._log(f"{self.name} disengaged")
