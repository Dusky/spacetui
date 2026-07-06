from __future__ import annotations

from api import ApiError

from .base import BaseBot


class ContractBot(BaseBot):
    """Full contract lifecycle: negotiate → accept → procure → deliver → fulfill.

    Procurement strategy: buy the goods at the cheapest known market when the
    ledger knows one; otherwise fall back to mining them (procurement contracts
    in the starter system are usually ores).
    """

    name = "contractor"

    def __init__(self, client, ship, *, reserve_credits: int = 3_000, **kw):
        super().__init__(client, ship, **kw)
        self.reserve_credits = reserve_credits

    # -- contract selection ------------------------------------------------------
    def _active_contract(self) -> dict | None:
        contracts = self.c.contracts()
        for ct in contracts:
            if ct.get("accepted") and not ct.get("fulfilled"):
                return ct
        # accept a pending one if present
        for ct in contracts:
            if not ct.get("accepted") and not ct.get("fulfilled"):
                try:
                    data = self.c.accept_contract(ct["id"])
                    ct = data.get("contract", ct)
                    pay = ct.get("terms", {}).get("payment", {})
                    self._log(
                        f"accepted contract {ct['id'][:8]} "
                        f"(+{pay.get('onAccepted', 0):,}c now, "
                        f"{pay.get('onFulfilled', 0):,}c on fulfill)"
                    )
                    return ct
                except ApiError as e:
                    self._log(f"accept failed: {e.message}")
        return None

    def _negotiate(self, s: dict) -> None:
        """Negotiate a new contract; requires being docked at a faction waypoint
        (the ship's current location usually works in the starter system)."""
        s = self.dock(s)
        try:
            data = self.c.negotiate_contract(self.ship)
            ct = data.get("contract", data)
            self._log(f"negotiated new contract {ct.get('id', '?')[:8]}")
        except ApiError as e:
            self._log(f"negotiate failed: {e.message}; retrying in 60s")
            self._sleep(60)

    # -- procurement ---------------------------------------------------------
    def _next_term(self, contract: dict) -> dict | None:
        for d in contract.get("terms", {}).get("deliver", []):
            if d["unitsFulfilled"] < d["unitsRequired"]:
                return d
        return None

    def _buy_goods(self, s: dict, good: str, units: int, source_wp: str) -> int:
        s = self.goto(s, source_wp)
        s = self.dock(s)
        self.record_market_here(s)
        self.refuel_here(s)
        credits = self.c.my_agent().get("credits", 0)
        live = self.db.best_buy(s["nav"]["systemSymbol"], good)
        price = live.buy if live else 0
        if price:
            affordable = max(0, (credits - self.reserve_credits) // price)
            units = min(units, affordable)
        if units <= 0:
            return 0
        volume = live.volume if live and live.volume else units
        bought = 0
        while bought < units and not self.cancelled:
            chunk = min(units - bought, max(volume, 1))
            try:
                data = self.c.purchase(self.ship, good, chunk)
            except ApiError as e:
                self._log(f"buy stopped: {e.message}")
                break
            t = data.get("transaction", {})
            bought += t.get("units", chunk)
            if "cargo" in data:
                s["cargo"] = data["cargo"]
            self._log(f"bought {t.get('units', chunk)} {good} = {t.get('totalPrice')}c")
        return bought

    def _mine_goods(self, s: dict, good: str) -> None:
        """One extract pass toward the desired good (survey-guided)."""
        system = s["nav"]["systemSymbol"]
        here = s["nav"]["waypointSymbol"]
        rocks = self.cache.asteroids(system)
        if not any(w["symbol"] == here for w in rocks):
            rock = self.cache.nearest(system, here, rocks)
            if not rock:
                self._log("nothing to mine in system; idling 60s")
                self._sleep(60)
                return
            self.goto(s, rock["symbol"])
            return
        s = self.orbit(s)
        self.await_cooldown()
        survey = None
        try:
            surveys = self.c.survey(self.ship)
            self.await_cooldown()
            survey = next(
                (
                    sv
                    for sv in surveys
                    if any(d.get("symbol") == good for d in sv.get("deposits", []))
                ),
                None,
            )
        except ApiError:
            pass
        try:
            data = self.c.extract(self.ship, survey=survey)
            y = data.get("extraction", {}).get("yield", {})
            self._log(f"mined +{y.get('units', 0)} {y.get('symbol', '')}")
        except ApiError as e:
            if e.code == 4228 or "cargo" in e.message.lower():
                return  # full — the main loop will deliver/sell
            self._log(f"extract error: {e.message}")
            self._sleep(5)

    # -- delivery ---------------------------------------------------------------
    def _deliver(self, s: dict, contract: dict, term: dict) -> None:
        inv = s.get("cargo", {}).get("inventory", [])
        have = next((i["units"] for i in inv if i["symbol"] == term["tradeSymbol"]), 0)
        if have <= 0:
            return
        need = term["unitsRequired"] - term["unitsFulfilled"]
        take = min(need, have)
        dest = term["destinationSymbol"]
        self._status(mode="deliver", last=f"→ {dest}")
        s = self.goto(s, dest)
        s = self.dock(s)
        try:
            data = self.c.deliver_contract(contract["id"], self.ship, term["tradeSymbol"], take)
            contract = data.get("contract", contract)
            self._log(f"delivered {take} {term['tradeSymbol']} ✓")
        except ApiError as e:
            self._log(f"deliver failed: {e.message}")
            self._sleep(10)
            return
        self.refuel_here(s)
        terms = contract.get("terms", {}).get("deliver", [])
        if terms and all(d["unitsFulfilled"] >= d["unitsRequired"] for d in terms):
            try:
                data = self.c.fulfill_contract(contract["id"])
                pay = contract.get("terms", {}).get("payment", {})
                self._log(f"contract FULFILLED +{pay.get('onFulfilled', 0):,}c ✓")
            except ApiError as e:
                self._log(f"fulfill failed: {e.message}")

    # -- main loop ---------------------------------------------------------------
    def loop(self) -> None:
        s = self.await_arrival()
        contract = self._active_contract()
        if contract is None:
            self._log("no open contract; negotiating")
            self._negotiate(s)
            return
        term = self._next_term(contract)
        if term is None:
            # everything delivered; fulfill and loop
            try:
                self.c.fulfill_contract(contract["id"])
                self._log("contract FULFILLED ✓")
            except ApiError as e:
                self._log(f"fulfill failed: {e.message}")
                self._sleep(30)
            return

        good = term["tradeSymbol"]
        need = term["unitsRequired"] - term["unitsFulfilled"]
        inv = s.get("cargo", {}).get("inventory", [])
        have = next((i["units"] for i in inv if i["symbol"] == good), 0)
        cargo = s.get("cargo", {})
        space = cargo.get("capacity", 0) - cargo.get("units", 0)

        # deliver when we have a full load or everything that's still needed
        if have >= need or (have > 0 and space <= 0):
            self._deliver(s, contract, term)
            return

        source = self.db.best_buy(s["nav"]["systemSymbol"], good)
        if source is not None and space > 0:
            self._status(mode="buy", last=f"{good} @ {source.waypoint}")
            got = self._buy_goods(s, good, min(need - have, space), source.waypoint)
            if got > 0:
                return
            self._log(f"couldn't buy {good}; mining instead")
        self._status(mode="mine", last=good)
        self._mine_goods(s, good)
