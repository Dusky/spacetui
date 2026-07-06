from __future__ import annotations

import datetime as dt
import re
import threading
import time

import config
from api import ApiError, Client


class BotCancelled(Exception):
    pass


def _system_of(waypoint: str) -> str:
    m = re.match(r"^(X1-[A-Z0-9]+)-", waypoint)
    return m.group(1) if m else waypoint


def _wait_seconds(ts: str | None) -> int:
    if not ts:
        return 0
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return max(0, int((t - dt.datetime.now(dt.timezone.utc)).total_seconds()))
    except ValueError:
        return 0


class MinerBot:
    """Survey-aware mining bot. Runs in a worker thread, cancellable."""

    def __init__(
        self,
        client: Client,
        ship: str,
        *,
        contract: str | None = None,
        sell: bool = True,
        on_log=None,
        on_status=None,
    ):
        self.c = client
        self.ship = ship
        self.contract_id = contract
        self.sell = sell
        self.on_log = on_log or (lambda m: None)
        self.on_status = on_status or (lambda **k: None)
        self._cancel = threading.Event()
        self.surveys: list[dict] = []

    def stop(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- internals ---------------------------------------------------------
    def _log(self, msg: str) -> None:
        self.on_log(f"{self.ship}  {msg}")

    def _status(self, mode: str = "", last: str = "") -> None:
        self.on_status(running=not self.cancelled, mode=mode, last=last)

    def _sleep(self, secs: int) -> None:
        if secs <= 0:
            return
        end = time.time() + secs
        while time.time() < end:
            if self._cancel.is_set():
                raise BotCancelled()
            time.sleep(min(0.4, max(0.0, end - time.time())))

    def _await_arrival(self) -> dict:
        s = self.c.ship(self.ship)
        nav = s.get("nav", {})
        while nav.get("status") == "IN_TRANSIT":
            secs = _wait_seconds(nav.get("route", {}).get("arrival"))
            self._status(mode="transit", last=f"in transit {secs}s")
            self._sleep(secs + 1)
            s = self.c.ship(self.ship)
            nav = s.get("nav", {})
        return s

    def _await_cooldown(self) -> None:
        cd = self.c.cooldown(self.ship)
        while cd.get("remainingSeconds"):
            secs = cd["remainingSeconds"]
            self._status(mode="cooldown", last=f"cooldown {secs}s")
            self._sleep(secs + 1)
            cd = self.c.cooldown(self.ship)

    def _has_trait(self, system: str, wp: str, trait: str) -> bool:
        w = self.c.waypoint(system, wp)
        return any(t["symbol"] == trait for t in w.get("traits", []))

    def _next_rock(self, system: str, avoid: set[str]) -> dict | None:
        for w in self.c.waypoints(system, filters={"traits": "MINERAL_DEPOSITS"}):
            if w["symbol"] not in avoid:
                return w
        return None

    def _pick_survey(self, desired: str | None) -> dict | None:
        if not self.surveys:
            return None
        if not desired:
            return self.surveys[0]
        rank = {"SMALL": 1, "MODERATE": 2, "LARGE": 3, "RICH": 4}
        best, best_score = None, -1
        for sv in self.surveys:
            for d in sv.get("deposits", []):
                if d.get("symbol") == desired:
                    sc = rank.get(d.get("size", ""), 0)
                    if sc > best_score:
                        best, best_score = sv, sc
        return best or self.surveys[0]

    def _ensure_fuel(self, s: dict) -> bool:
        nav = s.get("nav", {})
        fuel = s.get("fuel", {})
        cap = fuel.get("capacity", 0)
        cur = fuel.get("current", 0)
        if cap == 0 or cur / max(cap, 1) >= 0.5:
            return False
        target = config.HQ
        self._log(f"fuel low {cur}/{cap} → refuel at {target}")
        self._status(mode="refuel", last=f"→ {target}")
        if nav.get("waypointSymbol") != target:
            if nav.get("status") == "DOCKED":
                self.c.orbit(self.ship)
            try:
                self.c.navigate(self.ship, target)
            except ApiError as e:
                if e.code == 4203 or "fuel" in e.message.lower():
                    self.c.set_flight_mode(self.ship, "DRIFT")
                    self.c.navigate(self.ship, target)
                else:
                    raise
            return True
        if nav.get("status") != "DOCKED":
            self.c.dock(self.ship)
        try:
            self.c.refuel(self.ship)
            self.c.set_flight_mode(self.ship, "CRUISE")
            self._log("refueled")
        except ApiError as e:
            self._log(f"refuel failed: {e.message}")
        return True

    def _sell_off(self, s: dict, contract: dict | None) -> None:
        nav = s.get("nav", {})
        inv = s.get("cargo", {}).get("inventory", [])
        # 1) contract delivery
        if contract and not contract.get("fulfilled"):
            for d in contract["terms"]["deliver"]:
                need = d["unitsRequired"] - d["unitsFulfilled"]
                have = next((i["units"] for i in inv if i["symbol"] == d["tradeSymbol"]), 0)
                if need > 0 and have > 0:
                    take = min(need, have)
                    dest = d["destinationSymbol"]
                    self._log(f"deliver {take} {d['tradeSymbol']} → {dest}")
                    self._status(mode="deliver", last=f"→ {dest}")
                    if nav.get("waypointSymbol") != dest:
                        if nav.get("status") == "DOCKED":
                            self.c.orbit(self.ship)
                        self.c.navigate(self.ship, dest)
                        s = self._await_arrival()
                        nav = s["nav"]
                    if nav.get("status") != "DOCKED":
                        self.c.dock(self.ship)
                    self.c.sell(self.ship, d["tradeSymbol"], take)
                    self._log(f"delivered {take} {d['tradeSymbol']}")
        # 2) sell the rest at a market
        s = self.c.ship(self.ship)
        inv = s.get("cargo", {}).get("inventory", [])
        if not inv:
            return
        market = None
        for w in self.c.waypoints(nav["systemSymbol"], filters={"traits": "MARKETPLACE"}):
            market = w["symbol"]
            break
        if not market:
            self._log("no marketplace to sell at")
            return
        if s["nav"]["waypointSymbol"] != market:
            if s["nav"]["status"] == "DOCKED":
                self.c.orbit(self.ship)
            self._status(mode="sell", last=f"→ {market}")
            self.c.navigate(self.ship, market)
            s = self._await_arrival()
        self.c.dock(self.ship)
        try:
            self.c.refuel(self.ship)
            self.c.set_flight_mode(self.ship, "CRUISE")
        except ApiError:
            pass
        for item in inv:
            try:
                data = self.c.sell(self.ship, item["symbol"], item["units"])
                t = data.get("transaction", {})
                self._log(
                    f"sold {t.get('units', item['units'])} {item['symbol']} "
                    f"@ {t.get('pricePerUnit')} = {t.get('totalPrice')}c"
                )
            except ApiError as e:
                self._log(f"can't sell {item['symbol']}: {e.message}")

    # -- main loop ---------------------------------------------------------
    def run(self) -> None:
        self._log("engaged")
        tried: set[str] = set()
        try:
            while not self._cancel.is_set():
                s = self._await_arrival()
                nav = s.get("nav", {})
                system = nav.get("systemSymbol", "")
                here = nav.get("waypointSymbol", "")
                self.surveys = [sv for sv in self.surveys if sv.get("symbol") == here]

                if self._ensure_fuel(s):
                    continue

                cargo = s.get("cargo", {})
                capacity = cargo.get("capacity", 0)
                units = cargo.get("units", 0)

                contract = None
                if self.contract_id:
                    try:
                        contract = self.c.contract(self.contract_id)
                    except ApiError:
                        contract = None
                desired = None
                if contract and not contract.get("fulfilled"):
                    for d in contract["terms"]["deliver"]:
                        if d["unitsFulfilled"] < d["unitsRequired"]:
                            desired = d["tradeSymbol"]
                            break
                if contract and contract.get("fulfilled"):
                    self._log("contract fulfilled ✓")
                    self.contract_id = None

                if units >= capacity:
                    self._log(f"cargo full {units}/{capacity} → sell")
                    self._sell_off(s, contract)
                    tried = set()
                    continue

                if not self._has_trait(system, here, "MINERAL_DEPOSITS"):
                    rock = self._next_rock(system, tried)
                    if not rock:
                        self._log("no mineral deposits in system")
                        self._sleep(15)
                        continue
                    self._log(f"→ {rock['symbol']} to mine")
                    self._status(mode="transit", last=f"→ {rock['symbol']}")
                    if nav.get("status") == "DOCKED":
                        self.c.orbit(self.ship)
                    self.c.navigate(self.ship, rock["symbol"])
                    continue

                if nav.get("status") != "IN_ORBIT":
                    self.c.orbit(self.ship)
                self._await_cooldown()

                if desired and not self.surveys:
                    try:
                        self.surveys = self.c.survey(self.ship)
                        self._await_cooldown()
                        self._log(
                            "survey: "
                            + " | ".join(
                                ",".join(d["symbol"] for d in sv.get("deposits", []))
                                for sv in self.surveys
                            )
                        )
                    except ApiError as e:
                        self._log(f"survey unavailable: {e.message}")

                if desired and self.surveys:
                    has = any(
                        d.get("symbol") == desired
                        for sv in self.surveys
                        for d in sv.get("deposits", [])
                    )
                    if not has:
                        tried.add(here)
                        rock = self._next_rock(system, tried)
                        if rock:
                            self._log(f"no {desired} at {here} → {rock['symbol']}")
                            self._status(mode="transit", last=f"→ {rock['symbol']}")
                            self.c.navigate(self.ship, rock["symbol"])
                            self.surveys = []
                            continue
                        self._log(f"no {desired} found; mining raw")
                        self.surveys = []
                    else:
                        tried.discard(here)

                chosen = self._pick_survey(desired)
                try:
                    self._status(mode="mine", last=f"extract @ {here}")
                    data = self.c.extract(self.ship, survey=chosen)
                except ApiError as e:
                    if chosen and e.code in (4221, 4222, 4044):
                        self.surveys = [
                            sv for sv in self.surveys if sv.get("signature") != chosen.get("signature")
                        ]
                        continue
                    if e.code == 4228 or "cargo" in e.message.lower():
                        self._sell_off(s, contract)
                        continue
                    self._log(f"extract error: {e.message}")
                    self._sleep(5)
                    continue
                y = data.get("extraction", {}).get("yield", {})
                cd = data.get("cooldown", {})
                self._log(
                    f"+{y.get('units', 0)} {y.get('symbol', '')} "
                    f"(cd {cd.get('totalSeconds', 0)}s)"
                )
                if desired and y.get("symbol") != desired and chosen:
                    self.surveys = [
                        sv for sv in self.surveys if sv.get("signature") != chosen.get("signature")
                    ]
        except BotCancelled:
            pass
        except Exception as e:
            self._log(f"halted: {e!r}")
        finally:
            self._status(mode="stopped", last="idle")
            self._log("disengaged")
