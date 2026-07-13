"""Autonomous contract handling.

``pick_contract_action`` is a pure decision function (unit-tested);
``ContractManager`` drives it against the live API to keep procurement contracts
flowing — accept a pending contract, or negotiate a fresh one when none is
active — while the miners (which adopt the active contract) do the fulfilling.
"""

from __future__ import annotations

import datetime as dt
import threading
import time

from api import ApiError, Client, is_invalid_token_error


def _not_expired(c: dict, now: dt.datetime) -> bool:
    dl = c.get("deadlineToAccept")
    if not dl:
        return True
    try:
        return dt.datetime.fromisoformat(dl.replace("Z", "+00:00")) > now
    except ValueError:
        return True


def contract_reward(contract: dict) -> int:
    """Total credits a contract pays (on accept + on fulfil)."""
    pay = (contract.get("terms", {}) or {}).get("payment", {}) or {}
    return (pay.get("onAccepted", 0) or 0) + (pay.get("onFulfilled", 0) or 0)


def estimate_contract_cost(contract: dict, price_of=None) -> int:
    """Estimated credits to *buy* every undelivered unit, using
    ``price_of(good) -> int | None``. Unknown prices count as 0 (optimistic)."""
    total = 0
    for d in (contract.get("terms", {}) or {}).get("deliver", []) or []:
        need = (d.get("unitsRequired", 0) or 0) - (d.get("unitsFulfilled", 0) or 0)
        if need <= 0:
            continue
        p = price_of(d.get("tradeSymbol")) if price_of else None
        if p:
            total += need * p
    return total


def is_winnable(contract: dict, now: dt.datetime | None = None, *,
                price_of=None, min_margin: int = 0) -> bool:
    """A contract worth accepting: not expired, and its reward clears the
    estimated material cost by at least ``min_margin``."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not _not_expired(contract, now):
        return False
    return (contract_reward(contract) - estimate_contract_cost(contract, price_of)) >= min_margin


def pick_contract_action(contracts: list[dict], now: dt.datetime | None = None,
                         *, price_of=None, min_margin: int = 0):
    """Decide the next step from the current contract list.

    Returns one of:
      ``("work", id)``      an accepted, unfulfilled contract is in progress
      ``("accept", id)``    a pending, winnable contract worth taking
      ``("negotiate", None)`` nothing worth working — negotiate a new one

    A pending contract is skipped (declined) when it is expired or its reward
    doesn't clear the estimated material cost by ``min_margin`` — so a bad
    procurement offer is passed over instead of locking up the fleet.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    accepted = [c for c in contracts if c.get("accepted") and not c.get("fulfilled")]
    if accepted:
        return ("work", accepted[0]["id"])
    pending = [c for c in contracts
               if not c.get("accepted") and not c.get("fulfilled")
               and is_winnable(c, now, price_of=price_of, min_margin=min_margin)]
    if pending:
        return ("accept", pending[0]["id"])
    return ("negotiate", None)


def _good(contract: dict) -> str:
    deliver = (contract.get("terms", {}) or {}).get("deliver", [])
    return deliver[0]["tradeSymbol"] if deliver else "?"


class ContractManager:
    """Keeps a procurement contract active. Cancellable; runs in a thread."""

    def __init__(self, client: Client, ship: str, *, tick: int = 30,
                 min_margin: int = 0, price_of=None, on_log=None, on_status=None):
        self.c = client
        self.ship = ship  # a ship able to negotiate (docked at a faction waypoint)
        self.tick = tick
        self.min_margin = min_margin
        # price_of(good) -> unit price used to decline unwinnable contracts;
        # defaults to the cheapest buy price seen in the store
        self.price_of = price_of or self._store_price
        self.on_log = on_log or (lambda m: None)
        self.on_status = on_status or (lambda **k: None)
        self._cancel = threading.Event()
        self.active_contract_id: str | None = None

    @staticmethod
    def _store_price(good: str):
        import store
        prices = [o["purchase_price"] for o in store.latest_prices()
                  if o.get("symbol") == good and o.get("purchase_price")]
        return min(prices) if prices else None

    def stop(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _sleep(self, secs: int) -> None:
        end = time.time() + secs
        while time.time() < end and not self._cancel.is_set():
            time.sleep(min(0.5, max(0.0, end - time.time())))

    def run(self) -> None:
        self.on_log("contract manager engaged")
        while not self._cancel.is_set():
            try:
                action, cid = pick_contract_action(
                    self.c.contracts(), price_of=self.price_of, min_margin=self.min_margin)
                if action == "work":
                    self.active_contract_id = cid
                elif action == "accept":
                    self.c.accept_contract(cid)
                    self.active_contract_id = cid
                    self.on_log(f"accepted contract {cid[:8]}")
                else:  # negotiate a new one
                    data = self.c.negotiate_contract(self.ship)
                    c = data.get("contract", data)
                    cid = c.get("id")
                    if cid:
                        self.c.accept_contract(cid)
                        self.active_contract_id = cid
                        self.on_log(f"negotiated + accepted contract for {_good(c)}")
            except ApiError as e:
                if is_invalid_token_error(e):
                    # unrecoverable -- retrying every tick forever just
                    # hammers the API with a doomed request. Stop clean.
                    self.on_log(
                        f"FATAL: agent token is invalid ({e.message}). "
                        "Re-register the agent, update the token, and "
                        "restart. Contract manager stopping."
                    )
                    self.stop()
                    break
                self.on_log(f"contract step failed: {e.message}")
            self.on_status(running=not self.cancelled, last=self.active_contract_id or "idle")
            self._sleep(self.tick)
        self.on_log("contract manager disengaged")
