from __future__ import annotations

from api import ApiError

from .base import BaseBot

_SIZE_RANK = {"SMALL": 1, "MODERATE": 2, "LARGE": 3, "RICH": 4}


class MinerBot(BaseBot):
    """Survey-aware mining loop: mine → deliver contract goods → sell the rest.

    Contract deliveries use the real /deliver endpoint (selling at the
    destination does not count) and the contract is fulfilled automatically
    once every term is satisfied.
    """

    name = "miner"

    def __init__(self, client, ship, *, contract: str | None = None, sell: bool = True, **kw):
        super().__init__(client, ship, **kw)
        self.contract_id = contract
        self.sell = sell
        self.surveys: list[dict] = []
        self._tried_rocks: set[str] = set()

    # -- surveys ---------------------------------------------------------------
    def _pick_survey(self, desired: str | None) -> dict | None:
        if not self.surveys:
            return None
        if not desired:
            return self.surveys[0]
        best, best_score = None, -1
        for sv in self.surveys:
            for d in sv.get("deposits", []):
                if d.get("symbol") == desired:
                    sc = _SIZE_RANK.get(d.get("size", ""), 0)
                    if sc > best_score:
                        best, best_score = sv, sc
        return best or self.surveys[0]

    def _drop_survey(self, chosen: dict | None) -> None:
        if chosen:
            self.surveys = [
                sv for sv in self.surveys if sv.get("signature") != chosen.get("signature")
            ]

    # -- contract handling -------------------------------------------------------
    def _contract(self) -> dict | None:
        if not self.contract_id:
            return None
        try:
            ct = self.c.contract(self.contract_id)
        except ApiError:
            return None
        if ct.get("fulfilled"):
            self._log("contract fulfilled ✓")
            self.contract_id = None
            return None
        return ct

    def _desired_good(self, contract: dict | None) -> str | None:
        if not contract:
            return None
        for d in contract.get("terms", {}).get("deliver", []):
            if d["unitsFulfilled"] < d["unitsRequired"]:
                return d["tradeSymbol"]
        return None

    def _deliver(self, s: dict, contract: dict) -> dict:
        """Deliver whatever contract goods we hold, fulfill when complete."""
        for d in contract.get("terms", {}).get("deliver", []):
            need = d["unitsRequired"] - d["unitsFulfilled"]
            inv = s.get("cargo", {}).get("inventory", [])
            have = next((i["units"] for i in inv if i["symbol"] == d["tradeSymbol"]), 0)
            if need <= 0 or have <= 0:
                continue
            take = min(need, have)
            dest = d["destinationSymbol"]
            self._log(f"deliver {take} {d['tradeSymbol']} → {dest}")
            self._status(mode="deliver", last=f"→ {dest}")
            s = self.goto(s, dest)
            s = self.dock(s)
            try:
                data = self.c.deliver_contract(
                    contract["id"], self.ship, d["tradeSymbol"], take
                )
                contract = data.get("contract", contract)
                if "cargo" in data:
                    s["cargo"] = data["cargo"]
                self._log(f"delivered {take} {d['tradeSymbol']} ✓")
            except ApiError as e:
                self._log(f"deliver failed: {e.message}")
                return s
            self.refuel_here(s)
            self.record_market_here(s)

        terms = contract.get("terms", {}).get("deliver", [])
        if terms and all(d["unitsFulfilled"] >= d["unitsRequired"] for d in terms):
            try:
                self.c.fulfill_contract(contract["id"])
                self._log("contract FULFILLED — payout banked ✓")
                self.contract_id = None
            except ApiError as e:
                self._log(f"fulfill failed: {e.message}")
        return s

    # -- selling ---------------------------------------------------------------
    def _best_market_for_cargo(self, s: dict) -> str | None:
        """Prefer the waypoint our price ledger says pays best for the bulk of
        cargo; fall back to the nearest marketplace."""
        nav = s.get("nav", {})
        system = nav.get("systemSymbol", "")
        inv = s.get("cargo", {}).get("inventory", [])
        if not inv:
            return None
        bulk = max(inv, key=lambda i: i["units"])
        best = self.db.best_sell(system, bulk["symbol"])
        if best:
            return best.waypoint
        near = self.cache.nearest_market(system, nav.get("waypointSymbol", ""))
        return near["symbol"] if near else None

    def _sell_off(self, s: dict, contract: dict | None) -> dict:
        if contract:
            s = self._deliver(s, contract)
        if not self.sell:
            return s
        s = self.ship_state()
        inv = s.get("cargo", {}).get("inventory", [])
        keep = set()
        if contract:
            keep = {
                d["tradeSymbol"]
                for d in contract.get("terms", {}).get("deliver", [])
                if d["unitsFulfilled"] < d["unitsRequired"]
            }
        sellable = [i for i in inv if i["symbol"] not in keep]
        if not sellable:
            return s
        market = self._best_market_for_cargo(s)
        if not market:
            self._log("no marketplace to sell at")
            return s
        self._status(mode="sell", last=f"→ {market}")
        s = self.goto(s, market)
        s = self.dock(s)
        self.record_market_here(s)
        self.sell_inventory(s, keep=keep)
        self.refuel_here(s)
        return s

    # -- main loop ---------------------------------------------------------------
    def loop(self) -> None:
        s = self.await_arrival()
        nav = s.get("nav", {})
        system = nav.get("systemSymbol", "")
        here = nav.get("waypointSymbol", "")
        self.surveys = [sv for sv in self.surveys if sv.get("symbol") == here]

        cargo = s.get("cargo", {})
        capacity = cargo.get("capacity", 0)
        units = cargo.get("units", 0)

        contract = self._contract()
        desired = self._desired_good(contract)

        if capacity and units >= capacity:
            self._log(f"cargo full {units}/{capacity} → deliver/sell")
            self._sell_off(s, contract)
            self._tried_rocks = set()
            return

        rocks = self.cache.asteroids(system)
        at_rock = any(w["symbol"] == here for w in rocks)
        if not at_rock:
            candidates = [w for w in rocks if w["symbol"] not in self._tried_rocks]
            rock = self.cache.nearest(system, here, candidates)
            if not rock:
                self._log("no asteroids to mine in system; idling 30s")
                self._sleep(30)
                return
            self._log(f"→ {rock['symbol']} to mine")
            self.goto(s, rock["symbol"])
            return

        s = self.orbit(s)
        self.await_cooldown()

        if desired and not self.surveys:
            try:
                self.surveys = self.c.survey(self.ship)
                self.await_cooldown()
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
                self._tried_rocks.add(here)
                candidates = [
                    w for w in self.cache.asteroids(system) if w["symbol"] not in self._tried_rocks
                ]
                rock = self.cache.nearest(system, here, candidates)
                if rock:
                    self._log(f"no {desired} at {here} → {rock['symbol']}")
                    self.surveys = []
                    self.goto(s, rock["symbol"])
                    return
                self._log(f"no {desired} found anywhere; mining raw")
                self.surveys = []
            else:
                self._tried_rocks.discard(here)

        chosen = self._pick_survey(desired)
        self._status(mode="mine", last=f"extract @ {here}")
        try:
            data = self.c.extract(self.ship, survey=chosen)
        except ApiError as e:
            if chosen and e.code in (4221, 4222, 4224, 4044):
                self._drop_survey(chosen)
                return
            if e.code == 4228 or "cargo" in e.message.lower():
                self._sell_off(s, contract)
                return
            self._log(f"extract error: {e.message}")
            self._sleep(5)
            return
        y = data.get("extraction", {}).get("yield", {})
        cd = data.get("cooldown", {})
        self._log(f"+{y.get('units', 0)} {y.get('symbol', '')} (cd {cd.get('totalSeconds', 0)}s)")
        if desired and chosen and y.get("symbol") != desired:
            self._drop_survey(chosen)
