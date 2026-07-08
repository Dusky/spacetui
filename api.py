from __future__ import annotations

import time
from typing import Any, Iterator

import requests

import config
from ratelimit import LIMITER


class ApiError(Exception):
    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data or {}


class Client:
    """Thin wrapper around the SpaceTraders v2 API."""

    def __init__(self, token: str | None = None, base_url: str | None = None):
        self.token = token or config.AGENT_TOKEN
        self.base_url = (base_url or config.BASE_URL).rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    # -- low level ---------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        retry_on_rate_limit: bool = True,
    ) -> dict:
        url = f"{self.base_url}{path}"
        for attempt in range(3 if retry_on_rate_limit else 1):
            LIMITER.acquire()
            resp = self.session.request(method, url, params=params, json=json, timeout=30)
            if resp.status_code == 429 and retry_on_rate_limit:
                retry = float(resp.headers.get("Retry-After", "1") or 1)
                time.sleep(retry)
                continue
            break

        body: Any
        try:
            body = resp.json()
        except ValueError:
            body = {}

        if resp.status_code >= 400:
            err = body.get("error", {}) if isinstance(body, dict) else {}
            raise ApiError(
                err.get("code", resp.status_code),
                err.get("message", resp.reason),
                err.get("data"),
            )
        return body

    def get(self, path: str, **kw) -> dict:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> dict:
        return self.request("POST", path, **kw)

    def paginate(
        self, path: str, *, limit: int = 20, params: dict | None = None
    ) -> Iterator[dict]:
        page = 1
        params = dict(params or {})
        while True:
            params["limit"] = limit
            params["page"] = page
            body = self.get(path, params=params)
            data = body.get("data", []) or []
            for item in data:
                yield item
            meta = body.get("meta", {})
            total = meta.get("total", 0)
            if page * limit >= total:
                return
            page += 1

    # -- agent -------------------------------------------------------------
    def my_agent(self) -> dict:
        return self.get("/my/agent")["data"]

    # -- contracts ---------------------------------------------------------
    def contracts(self) -> list[dict]:
        return list(self.paginate("/my/contracts"))

    def contract(self, contract_id: str) -> dict:
        return self.get(f"/my/contracts/{contract_id}")["data"]

    def accept_contract(self, contract_id: str) -> dict:
        return self.post(f"/my/contracts/{contract_id}/accept", json={})["data"]

    def negotiate_contract(self, ship_symbol: str) -> dict:
        return self.post(f"/my/ships/{ship_symbol}/negotiate/contract", json={})["data"]

    def deliver_contract(
        self, contract_id: str, ship_symbol: str, trade_symbol: str, units: int
    ) -> dict:
        return self.post(
            f"/my/contracts/{contract_id}/deliver",
            json={
                "shipSymbol": ship_symbol,
                "tradeSymbol": trade_symbol,
                "units": int(units),
            },
        )["data"]

    def fulfill_contract(self, contract_id: str) -> dict:
        return self.post(f"/my/contracts/{contract_id}/fulfill", json={})["data"]

    # -- ships -------------------------------------------------------------
    def ships(self) -> list[dict]:
        return list(self.paginate("/my/ships"))

    def purchase_ship(self, ship_type: str, waypoint: str) -> dict:
        return self.post(
            "/my/ships",
            json={"shipType": ship_type, "waypointSymbol": waypoint},
        )["data"]

    def ship(self, symbol: str) -> dict:
        return self.get(f"/my/ships/{symbol}")["data"]

    def cooldown(self, symbol: str) -> dict:
        return self.get(f"/my/ships/{symbol}/cooldown").get("data", {}) or {}

    def orbit(self, symbol: str) -> dict:
        return self.post(f"/my/ships/{symbol}/orbit", json={})["data"]

    def dock(self, symbol: str) -> dict:
        return self.post(f"/my/ships/{symbol}/dock", json={})["data"]

    def refuel(self, symbol: str) -> dict:
        return self.post(f"/my/ships/{symbol}/refuel", json={})["data"]

    def navigate(self, symbol: str, waypoint: str) -> dict:
        return self.post(f"/my/ships/{symbol}/navigate", json={"waypointSymbol": waypoint})[
            "data"
        ]

    def set_flight_mode(self, symbol: str, mode: str) -> dict:
        return self.patch(f"/my/ships/{symbol}/nav", json={"flightMode": mode})["data"]

    def patch(self, path: str, **kw) -> dict:
        return self.request("PATCH", path, **kw)

    def extract(self, symbol: str, survey: dict | None = None) -> dict:
        payload: dict = {}
        if survey:
            payload["survey"] = survey
        return self.post(f"/my/ships/{symbol}/extract", json=payload)["data"]

    def survey(self, symbol: str) -> list[dict]:
        return self.post(f"/my/ships/{symbol}/survey", json={}).get("data", {}).get(
            "surveys", []
        ) or []

    def warp(self, symbol: str, waypoint: str) -> dict:
        return self.post(f"/my/ships/{symbol}/warp", json={"waypointSymbol": waypoint})[
            "data"
        ]

    def jump(self, symbol: str, waypoint: str) -> dict:
        # jump targets a connected jump-gate *waypoint* (in another system)
        return self.post(f"/my/ships/{symbol}/jump", json={"waypointSymbol": waypoint})["data"]

    def sell(self, symbol: str, trade_symbol: str, units: int) -> dict:
        return self.post(
            f"/my/ships/{symbol}/sell",
            json={"symbol": trade_symbol, "units": int(units)},
        )["data"]

    def purchase(self, symbol: str, trade_symbol: str, units: int) -> dict:
        return self.post(
            f"/my/ships/{symbol}/purchase",
            json={"symbol": trade_symbol, "units": int(units)},
        )["data"]

    def jettison(self, symbol: str, trade_symbol: str, units: int) -> dict:
        return self.post(
            f"/my/ships/{symbol}/jettison",
            json={"symbol": trade_symbol, "units": int(units)},
        )["data"]

    def cargo(self, symbol: str) -> dict:
        return self.get(f"/my/ships/{symbol}/cargo")["data"]

    def transfer_cargo(
        self, symbol: str, trade_symbol: str, units: int, dest_ship: str
    ) -> dict:
        return self.post(
            f"/my/ships/{symbol}/transfer",
            json={"tradeSymbol": trade_symbol, "units": int(units), "shipSymbol": dest_ship},
        )["data"]

    def siphon(self, symbol: str) -> dict:
        return self.post(f"/my/ships/{symbol}/siphon", json={})["data"]

    def refine(self, symbol: str, produce: str) -> dict:
        return self.post(f"/my/ships/{symbol}/refine", json={"produce": produce})["data"]

    def chart(self, symbol: str) -> dict:
        return self.post(f"/my/ships/{symbol}/chart", json={})["data"]

    def scan_waypoints(self, symbol: str) -> dict:
        return self.post(f"/my/ships/{symbol}/scan/waypoints", json={})["data"]

    def scan_systems(self, symbol: str) -> dict:
        return self.post(f"/my/ships/{symbol}/scan/systems", json={})["data"]

    def scan_ships(self, symbol: str) -> dict:
        return self.post(f"/my/ships/{symbol}/scan/ships", json={})["data"]

    # -- refit / maintenance ----------------------------------------------
    def ship_mounts(self, symbol: str) -> list[dict]:
        return self.get(f"/my/ships/{symbol}/mounts")["data"]

    def install_mount(self, symbol: str, mount_symbol: str) -> dict:
        return self.post(
            f"/my/ships/{symbol}/mounts/install", json={"symbol": mount_symbol}
        )["data"]

    def remove_mount(self, symbol: str, mount_symbol: str) -> dict:
        return self.post(
            f"/my/ships/{symbol}/mounts/remove", json={"symbol": mount_symbol}
        )["data"]

    def repair_cost(self, symbol: str) -> dict:
        return self.get(f"/my/ships/{symbol}/repair")["data"]

    def repair_ship(self, symbol: str) -> dict:
        return self.post(f"/my/ships/{symbol}/repair", json={})["data"]

    def scrap_value(self, symbol: str) -> dict:
        return self.get(f"/my/ships/{symbol}/scrap")["data"]

    def scrap_ship(self, symbol: str) -> dict:
        return self.post(f"/my/ships/{symbol}/scrap", json={})["data"]

    # -- world -------------------------------------------------------------
    def systems(self) -> list[dict]:
        return list(self.paginate("/systems"))

    def system(self, symbol: str) -> dict:
        return self.get(f"/systems/{symbol}")["data"]

    def waypoints(self, system: str, filters: dict | None = None) -> list[dict]:
        return list(self.paginate(f"/systems/{system}/waypoints", params=filters))

    def waypoint(self, system: str, waypoint: str) -> dict:
        return self.get(f"/systems/{system}/waypoints/{waypoint}")["data"]

    def market(self, system: str, waypoint: str) -> dict:
        return self.get(f"/systems/{system}/waypoints/{waypoint}/market")["data"]

    def shipyard(self, system: str, waypoint: str) -> dict:
        return self.get(f"/systems/{system}/waypoints/{waypoint}/shipyard")["data"]

    def jump_gate(self, system: str, waypoint: str) -> dict:
        return self.get(f"/systems/{system}/waypoints/{waypoint}/jump-gate")["data"]

    # -- account / registration -------------------------------------------
    @classmethod
    def register(
        cls, symbol: str, faction: str, account_token: str | None = None
    ) -> dict:
        token = account_token or config.ACCOUNT_TOKEN
        if not token:
            raise SystemExit("No ST_ACCOUNT_TOKEN in .env; cannot register.")
        LIMITER.acquire()
        resp = requests.post(
            f"{config.BASE_URL}/register",
            json={"symbol": symbol, "faction": faction},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        body = resp.json()
        if resp.status_code >= 400:
            err = body.get("error", {})
            raise ApiError(err.get("code", resp.status_code), err.get("message", resp.reason))
        return body["data"]
