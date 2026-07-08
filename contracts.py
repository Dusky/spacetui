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

from api import ApiError, Client


def _not_expired(c: dict, now: dt.datetime) -> bool:
    dl = c.get("deadlineToAccept")
    if not dl:
        return True
    try:
        return dt.datetime.fromisoformat(dl.replace("Z", "+00:00")) > now
    except ValueError:
        return True


def pick_contract_action(contracts: list[dict], now: dt.datetime | None = None):
    """Decide the next step from the current contract list.

    Returns one of:
      ``("work", id)``      an accepted, unfulfilled contract is in progress
      ``("accept", id)``    a pending (unaccepted, un-expired) contract to take
      ``("negotiate", None)`` nothing to work — negotiate a new one
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    accepted = [c for c in contracts if c.get("accepted") and not c.get("fulfilled")]
    if accepted:
        return ("work", accepted[0]["id"])
    pending = [c for c in contracts
               if not c.get("accepted") and not c.get("fulfilled") and _not_expired(c, now)]
    if pending:
        return ("accept", pending[0]["id"])
    return ("negotiate", None)


def _good(contract: dict) -> str:
    deliver = (contract.get("terms", {}) or {}).get("deliver", [])
    return deliver[0]["tradeSymbol"] if deliver else "?"


class ContractManager:
    """Keeps a procurement contract active. Cancellable; runs in a thread."""

    def __init__(self, client: Client, ship: str, *, tick: int = 30,
                 on_log=None, on_status=None):
        self.c = client
        self.ship = ship  # a ship able to negotiate (docked at a faction waypoint)
        self.tick = tick
        self.on_log = on_log or (lambda m: None)
        self.on_status = on_status or (lambda **k: None)
        self._cancel = threading.Event()
        self.active_contract_id: str | None = None

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
                action, cid = pick_contract_action(self.c.contracts())
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
                self.on_log(f"contract step failed: {e.message}")
            self.on_status(running=not self.cancelled, last=self.active_contract_id or "idle")
            self._sleep(self.tick)
        self.on_log("contract manager disengaged")
