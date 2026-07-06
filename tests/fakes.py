"""In-memory SpaceTraders stand-in for exercising bots without the live API."""

from __future__ import annotations

from api import ApiError


class FakeClient:
    """Simulates one system with instant travel and static-ish markets."""

    def __init__(self, waypoints: list[dict], markets: dict[str, dict], ships: dict[str, dict],
                 agent: dict | None = None, contracts: list[dict] | None = None):
        self._waypoints = waypoints
        self._markets = markets
        self._ships = ships
        self._agent = agent or {"symbol": "TEST", "credits": 100_000}
        self._contracts = contracts or []
        self.log: list[tuple] = []

    # -- helpers ---------------------------------------------------------
    def _wp(self, symbol: str) -> dict:
        for w in self._waypoints:
            if w["symbol"] == symbol:
                return w
        raise ApiError(404, f"waypoint {symbol} not found")

    def _market_goods(self, wp: str) -> dict[str, dict]:
        return {g["symbol"]: g for g in self._markets.get(wp, {}).get("tradeGoods", [])}

    # -- API surface used by bots ------------------------------------------
    def my_agent(self):
        return dict(self._agent)

    def ship(self, symbol):
        return self._ships[symbol]

    def cooldown(self, symbol):
        return {}

    def orbit(self, symbol):
        self._ships[symbol]["nav"]["status"] = "IN_ORBIT"
        return {"nav": self._ships[symbol]["nav"]}

    def dock(self, symbol):
        self._ships[symbol]["nav"]["status"] = "DOCKED"
        return {"nav": self._ships[symbol]["nav"]}

    def navigate(self, symbol, waypoint):
        ship = self._ships[symbol]
        dest = self._wp(waypoint)  # validates
        ship["nav"]["waypointSymbol"] = dest["symbol"]
        ship["nav"]["status"] = "IN_ORBIT"  # instant arrival
        self.log.append(("navigate", symbol, waypoint))
        return {"nav": ship["nav"], "fuel": ship["fuel"]}

    def set_flight_mode(self, symbol, mode):
        self._ships[symbol]["nav"]["flightMode"] = mode
        return self._ships[symbol]["nav"]

    def refuel(self, symbol):
        f = self._ships[symbol]["fuel"]
        f["current"] = f["capacity"]
        self.log.append(("refuel", symbol))
        return {"fuel": f, "transaction": {"units": 0, "totalPrice": 0}}

    def waypoints(self, system, filters=None):
        wps = self._waypoints
        if filters and "traits" in filters:
            trait = filters["traits"]
            wps = [w for w in wps if any(t["symbol"] == trait for t in w.get("traits", []))]
        return list(wps)

    def waypoint(self, system, waypoint):
        return self._wp(waypoint)

    def market(self, system, waypoint):
        if waypoint not in self._markets:
            raise ApiError(404, "no market")
        return self._markets[waypoint]

    def purchase(self, symbol, trade, units):
        ship = self._ships[symbol]
        goods = self._market_goods(ship["nav"]["waypointSymbol"])
        if trade not in goods:
            raise ApiError(4601, f"{trade} not sold here")
        price = goods[trade]["purchasePrice"]
        total = price * units
        if total > self._agent["credits"]:
            raise ApiError(4600, "insufficient credits")
        self._agent["credits"] -= total
        cargo = ship["cargo"]
        inv = cargo.setdefault("inventory", [])
        for item in inv:
            if item["symbol"] == trade:
                item["units"] += units
                break
        else:
            inv.append({"symbol": trade, "units": units})
        cargo["units"] = sum(i["units"] for i in inv)
        self.log.append(("purchase", symbol, trade, units, price))
        return {
            "transaction": {"units": units, "pricePerUnit": price, "totalPrice": total},
            "cargo": cargo,
            "agent": dict(self._agent),
        }

    def sell(self, symbol, trade, units):
        ship = self._ships[symbol]
        goods = self._market_goods(ship["nav"]["waypointSymbol"])
        if trade not in goods:
            raise ApiError(4602, f"{trade} not bought here")
        price = goods[trade]["sellPrice"]
        cargo = ship["cargo"]
        inv = cargo.get("inventory", [])
        for item in inv:
            if item["symbol"] == trade:
                if item["units"] < units:
                    raise ApiError(4603, "not enough cargo")
                item["units"] -= units
                break
        else:
            raise ApiError(4603, f"no {trade} in cargo")
        cargo["inventory"] = [i for i in inv if i["units"] > 0]
        cargo["units"] = sum(i["units"] for i in cargo["inventory"])
        self._agent["credits"] += price * units
        self.log.append(("sell", symbol, trade, units, price))
        return {
            "transaction": {"units": units, "pricePerUnit": price, "totalPrice": price * units},
            "cargo": cargo,
            "agent": dict(self._agent),
        }

    # -- contracts ---------------------------------------------------------
    def contracts(self):
        return [dict(c) for c in self._contracts]

    def contract(self, cid):
        for c in self._contracts:
            if c["id"] == cid:
                return dict(c)
        raise ApiError(404, "no contract")

    def accept_contract(self, cid):
        c = self.contract(cid)
        for real in self._contracts:
            if real["id"] == cid:
                real["accepted"] = True
        self.log.append(("accept", cid))
        return {"contract": self.contract(cid)}

    def deliver_contract(self, cid, ship_symbol, trade, units):
        ship = self._ships[ship_symbol]
        cargo = ship["cargo"]
        inv = cargo.get("inventory", [])
        item = next((i for i in inv if i["symbol"] == trade), None)
        if item is None or item["units"] < units:
            raise ApiError(4509, "not enough cargo to deliver")
        item["units"] -= units
        cargo["inventory"] = [i for i in inv if i["units"] > 0]
        cargo["units"] = sum(i["units"] for i in cargo["inventory"])
        for real in self._contracts:
            if real["id"] == cid:
                for d in real["terms"]["deliver"]:
                    if d["tradeSymbol"] == trade:
                        d["unitsFulfilled"] += units
        self.log.append(("deliver", cid, ship_symbol, trade, units))
        return {"contract": self.contract(cid), "cargo": cargo}

    def fulfill_contract(self, cid):
        for real in self._contracts:
            if real["id"] == cid:
                terms = real["terms"]["deliver"]
                if not all(d["unitsFulfilled"] >= d["unitsRequired"] for d in terms):
                    raise ApiError(4510, "terms not met")
                real["fulfilled"] = True
        self.log.append(("fulfill", cid))
        return {"contract": self.contract(cid), "agent": dict(self._agent)}

    def survey(self, symbol):
        return []

    def extract(self, symbol, survey=None):
        raise ApiError(4227, "no extractor mount in fake")


def make_world():
    """Two-market system with an obvious IRON arbitrage (buy 40 @A, sell 90 @B)."""
    waypoints = [
        {"symbol": "S1-AA-A1", "type": "PLANET", "x": 0, "y": 0,
         "traits": [{"symbol": "MARKETPLACE", "name": "m"}]},
        {"symbol": "S1-AA-B2", "type": "MOON", "x": 10, "y": 0,
         "traits": [{"symbol": "MARKETPLACE", "name": "m"}]},
        {"symbol": "S1-AA-C3", "type": "ASTEROID", "x": 5, "y": 5,
         "traits": [{"symbol": "MINERAL_DEPOSITS", "name": "rocks"}]},
    ]
    markets = {
        "S1-AA-A1": {"symbol": "S1-AA-A1", "tradeGoods": [
            {"symbol": "IRON", "type": "EXPORT", "purchasePrice": 40, "sellPrice": 35, "tradeVolume": 10},
            {"symbol": "FUEL", "type": "EXCHANGE", "purchasePrice": 70, "sellPrice": 60, "tradeVolume": 100},
        ]},
        "S1-AA-B2": {"symbol": "S1-AA-B2", "tradeGoods": [
            {"symbol": "IRON", "type": "IMPORT", "purchasePrice": 100, "sellPrice": 90, "tradeVolume": 10},
            {"symbol": "FUEL", "type": "EXCHANGE", "purchasePrice": 70, "sellPrice": 60, "tradeVolume": 100},
        ]},
    }
    ships = {
        "TEST-1": {
            "symbol": "TEST-1",
            "registration": {"role": "HAULER"},
            "nav": {"status": "DOCKED", "systemSymbol": "S1-AA",
                    "waypointSymbol": "S1-AA-A1", "flightMode": "CRUISE", "route": {}},
            "fuel": {"current": 100, "capacity": 100},
            "cargo": {"units": 0, "capacity": 20, "inventory": []},
        }
    }
    return waypoints, markets, ships
