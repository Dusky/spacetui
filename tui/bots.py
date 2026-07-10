from __future__ import annotations

import datetime as dt
import re
import threading
import time

import claims
import store
import world as world_mod
from api import ApiError, Client
from arbitrage import (
    demand_factor,
    plan_sales,
    price_ceiling,
    price_floor,
    sustainable_units,
)
from construction import cheapest_source, is_complete, materials_gap, next_material
from routing import build_graph, shortest_path, system_of


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


class BaseBot:
    """Shared machinery for cancellable worker-thread bots."""

    def __init__(
        self,
        client: Client,
        ship: str,
        *,
        world=None,
        on_log=None,
        on_status=None,
    ):
        self.c = client
        self.ship = ship
        # shared world model (cached discovery); fall back to the module default
        self.world = world if world is not None else world_mod.WORLD
        self.on_log = on_log or (lambda m: None)
        self.on_status = on_status or (lambda **k: None)
        self._cancel = threading.Event()
        # exploration defaults (subclasses override as needed)
        self.max_age_s = 3600.0
        self.max_hops = 2
        self.cross_system = False
        self._gated: set[str] = set()

    # -- discovery via the shared world model (falls back to direct calls) --
    def _wps(self, system: str, *, trait: str | None = None,
             type: str | None = None) -> list[dict]:
        if self.world is not None:
            return self.world.find_waypoints(system, trait=trait, type=type)
        filters: dict = {}
        if trait:
            filters["traits"] = trait
        if type:
            filters["type"] = type
        return self.c.waypoints(system, filters=filters or None)

    def _market(self, system: str, waypoint: str, fresh: bool = False) -> dict | None:
        if self.world is not None:
            return self.world.get_market(system, waypoint, max_age=0 if fresh else None)
        try:
            m = self.c.market(system, waypoint)
        except ApiError:
            return None
        store.record_market(m)
        return m

    def stop(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @staticmethod
    def _is_transient_nav_error(e: ApiError) -> bool:
        """A ship-in-transit conflict (e.g. a stale action fired mid-flight) —
        transient, so re-sync and retry rather than halting the bot."""
        return getattr(e, "code", None) == 4214 or "in-transit" in getattr(e, "message", "").lower()

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

    def _goto(self, s: dict, waypoint: str) -> dict:
        """Navigate to ``waypoint`` (DRIFT fallback if fuel-short) and wait."""
        nav = s.get("nav", {})
        if nav.get("waypointSymbol") == waypoint and nav.get("status") != "IN_TRANSIT":
            return s
        if nav.get("status") == "DOCKED":
            self.c.orbit(self.ship)
        self._status(mode="transit", last=f"→ {waypoint}")
        try:
            self.c.navigate(self.ship, waypoint)
        except ApiError as e:
            if e.code == 4203 or "fuel" in e.message.lower():
                self.c.set_flight_mode(self.ship, "DRIFT")
                self.c.navigate(self.ship, waypoint)
            elif self._is_transient_nav_error(e):
                # already in transit (e.g. a racing action) — just ride it out
                pass
            else:
                raise
        return self._await_arrival()

    def _maybe_refuel(self, threshold: float = 0.4) -> None:
        s = self.c.ship(self.ship)
        if s.get("nav", {}).get("status") != "DOCKED":
            return
        fuel = s.get("fuel", {})
        cap = fuel.get("capacity", 0)
        if cap == 0 or fuel.get("current", 0) / cap >= threshold:
            return
        try:
            self.c.refuel(self.ship)
            self.c.set_flight_mode(self.ship, "CRUISE")
            self._log("refueled")
        except ApiError as e:
            self._log(f"refuel skipped: {e.message}")

    # -- market & jump-gate exploration (shared by traders and scouts) -----
    def _record_here(self, system: str, waypoint: str, fresh: bool = False) -> dict | None:
        m = self._market(system, waypoint, fresh=fresh)
        if m is None:
            return None
        goods = m.get("tradeGoods") or []
        if goods:
            self._log(f"scanned {waypoint} ({len(goods)} goods)")
        return m

    def _next_unscanned_market(self, system: str, here: str) -> str | None:
        fresh = {
            o["waypoint"]
            for o in store.latest_prices(system=system, max_age_s=self.max_age_s)
        }
        for w in self._wps(system, trait="MARKETPLACE"):
            if w["symbol"] != here and w["symbol"] not in fresh:
                return w["symbol"]
        return None

    def _map_gate(self, system: str) -> None:
        """Record ``system``'s jump gate and its connections (once per system)."""
        if system in self._gated:
            return
        self._gated.add(system)
        try:
            gates = self._wps(system, type="JUMP_GATE")
        except ApiError:
            return
        if not gates:
            return
        try:
            jg = self.c.jump_gate(system, gates[0]["symbol"])
        except ApiError:
            return
        n = store.record_jump_gate(jg)
        if n:
            self._log(f"mapped jump gate {gates[0]['symbol']} ({n} links)")

    def _travel_to_system(self, target: str) -> bool:
        """Jump-hop from the current system to ``target``. Returns success."""
        s = self.c.ship(self.ship)
        current = s.get("nav", {}).get("systemSymbol", "")
        if current == target:
            return True
        self._map_gate(current)
        adj, gate_of = build_graph(store.jump_edges())
        path = shortest_path(adj, current, target)
        if not path or len(path) < 2:
            self._log(f"no jump route {current} → {target}")
            return False
        for nxt in path[1:]:
            cur_gate, dest_gate = gate_of.get(current), gate_of.get(nxt)
            if not cur_gate or not dest_gate:
                self._log(f"missing gate for {current}/{nxt}")
                return False
            s = self._goto(s, cur_gate)
            if s.get("nav", {}).get("status") == "DOCKED":
                self.c.orbit(self.ship)
            self._status(mode="jump", last=f"jump → {nxt}")
            try:
                self.c.jump(self.ship, dest_gate)
            except ApiError as e:
                self._log(f"jump failed: {e.message}")
                return False
            self._await_cooldown()
            s = self._await_arrival()
            current = nxt
            self._map_gate(current)
        self._log(f"arrived in {target}")
        return True

    def _next_unscanned_system(self, current: str) -> str | None:
        """A reachable neighbour system we have no market prices for yet."""
        adj, _ = build_graph(store.jump_edges())
        priced = {o["system"] for o in store.latest_prices(max_age_s=self.max_age_s)}
        frontier, seen, hops = [current], {current}, 0
        while frontier and hops < self.max_hops:
            hops += 1
            nxt_frontier = []
            for sys_sym in frontier:
                for nb in sorted(adj.get(sys_sym, ())):
                    if nb in seen:
                        continue
                    seen.add(nb)
                    if nb not in priced:
                        return nb
                    nxt_frontier.append(nb)
            frontier = nxt_frontier
        return None


class MinerBot(BaseBot):
    """Survey-aware mining bot. Runs in a worker thread, cancellable."""

    def __init__(
        self,
        client: Client,
        ship: str,
        *,
        contract: str | None = None,
        sell: bool = True,
        get_contract=None,
        world=None,
        on_log=None,
        on_status=None,
    ):
        super().__init__(client, ship, world=world, on_log=on_log, on_status=on_status)
        self.contract_id = contract
        self.sell = sell
        # optional callable returning the currently-active contract id to adopt
        self.get_contract = get_contract
        self.surveys: list[dict] = []
        # goods learned to be unsellable in this system (jettisoned on yield)
        self._unsellable: set[str] = set()

    # -- internals ---------------------------------------------------------
    def _has_trait(self, system: str, wp: str, trait: str) -> bool:
        if self.world is not None:
            for w in self.world.get_waypoints(system):
                if w["symbol"] == wp:
                    return trait in (w.get("traits") or [])
            return False
        w = self.c.waypoint(system, wp)
        return any(t["symbol"] == trait for t in w.get("traits", []))

    def _next_rock(self, system: str, avoid: set[str], here: str | None = None) -> dict | None:
        """Nearest un-avoided asteroid field to ``here`` (by x/y). Falls back to
        the first candidate when the origin's coordinates are unknown."""
        rocks = [w for w in self._wps(system, trait="MINERAL_DEPOSITS")
                 if w["symbol"] not in avoid]
        if not rocks:
            return None
        origin = None
        if here:
            for w in self._wps(system):
                if w["symbol"] == here:
                    origin = (w.get("x", 0), w.get("y", 0))
                    break
        if origin is None:
            return rocks[0]
        return min(rocks, key=lambda w: (w.get("x", 0) - origin[0]) ** 2
                   + (w.get("y", 0) - origin[1]) ** 2)

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

    def _sell_off(self, s: dict, contract: dict | None) -> bool:
        """Deliver contract goods, sell the rest where it's actually listed,
        jettison what nothing in-system buys. Returns True if the hold shrank
        (sold, delivered or jettisoned anything) so the caller can back off
        instead of spinning when no progress is possible."""
        progress = False
        nav = s.get("nav", {})
        inv = s.get("cargo", {}).get("inventory", [])
        # 1) contract delivery (via the /deliver endpoint, not a market sale)
        if contract and self.contract_id and not contract.get("fulfilled"):
            for d in contract["terms"]["deliver"]:
                need = d["unitsRequired"] - d["unitsFulfilled"]
                have = next((i["units"] for i in inv if i["symbol"] == d["tradeSymbol"]), 0)
                if need > 0 and have > 0:
                    take = min(need, have)
                    dest = d["destinationSymbol"]
                    self._log(f"deliver {take} {d['tradeSymbol']} → {dest}")
                    self._status(mode="deliver", last=f"→ {dest}")
                    if nav.get("waypointSymbol") != dest:
                        s = self._goto(s, dest)
                        nav = s["nav"]
                    if nav.get("status") != "DOCKED":
                        self.c.dock(self.ship)
                    try:
                        res = self.c.deliver_contract(
                            self.contract_id, self.ship, d["tradeSymbol"], take
                        )
                        contract = res.get("contract", contract)
                        self._log(f"delivered {take} {d['tradeSymbol']}")
                        progress = True
                    except ApiError as e:
                        self._log(f"deliver failed: {e.message}")
            # 2) fulfill once every deliverable is complete
            if contract and not contract.get("fulfilled") and all(
                dd["unitsFulfilled"] >= dd["unitsRequired"]
                for dd in contract["terms"]["deliver"]
            ):
                try:
                    fres = self.c.fulfill_contract(self.contract_id)
                    paid = fres.get("agent", {}).get("credits")
                    self._log(f"contract fulfilled ✓ (credits {paid})")
                    self.contract_id = None
                except ApiError as e:
                    self._log(f"fulfill failed: {e.message}")
        # 3) sell the rest — but only where each good is actually listed. A
        # market rejects goods outside its imports/exports/exchange catalog,
        # so plan stops from the (cached) market payloads instead of dumping
        # everything on the first marketplace.
        s = self.c.ship(self.ship)
        nav = s.get("nav", {})
        inv = s.get("cargo", {}).get("inventory", [])
        if not inv:
            return progress
        system = nav["systemSymbol"]
        markets = {w["symbol"]: self._market(system, w["symbol"])
                   for w in self._wps(system, trait="MARKETPLACE")}
        if not markets:
            self._log("no marketplace to sell at")
            return progress
        plan, unsellable = plan_sales(inv, markets)
        for wp, goods in plan:
            self._status(mode="sell", last=f"→ {wp}")
            s = self._goto(s, wp)
            self.c.dock(self.ship)
            # top up so the next mining run can CRUISE out on a full tank
            self._maybe_refuel(threshold=0.9)
            held = {i["symbol"]: i["units"]
                    for i in self.c.cargo(self.ship).get("inventory", [])}
            for good in goods:
                units = held.get(good, 0)
                if units <= 0:
                    continue
                try:
                    data = self.c.sell(self.ship, good, units)
                    t = data.get("transaction", {})
                    store.record_trade("sell", good, t.get("units", units),
                                       t.get("pricePerUnit", 0), t.get("totalPrice", 0),
                                       ship=self.ship, waypoint=wp)
                    self._log(
                        f"sold {t.get('units', units)} {good} "
                        f"@ {t.get('pricePerUnit')} = {t.get('totalPrice')}c"
                    )
                    progress = True
                except ApiError as e:
                    self._log(f"can't sell {good}: {e.message}")
            # record the fresh prices we just saw here
            self._record_here(system, wp, fresh=True)
        # jettison what no market in the system buys — mined goods are free,
        # and holding them forever is what livelocked the ship. Never drop a
        # good an unfinished contract still needs, and never drop anything on
        # the strength of zero market data (all fetches failed = we know
        # nothing, not "nothing buys it").
        if not any(m for m in markets.values()):
            self._log("no market data available; will retry")
            return progress
        keep = set()
        if contract and not contract.get("fulfilled"):
            keep = {d["tradeSymbol"] for d in contract["terms"]["deliver"]
                    if d["unitsFulfilled"] < d["unitsRequired"]}
        if unsellable:
            self._unsellable.update(unsellable)
            held = {i["symbol"]: i["units"]
                    for i in self.c.cargo(self.ship).get("inventory", [])}
            for good in unsellable:
                units = held.get(good, 0)
                if units <= 0 or good in keep:
                    continue
                try:
                    self.c.jettison(self.ship, good, units)
                    self._log(f"jettisoned {units} {good} (no market in {system} buys it)")
                    progress = True
                except ApiError as e:
                    self._log(f"jettison failed for {good}: {e.message}")
        return progress

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

                # adopt the orchestrator's active contract, if any
                if self.get_contract:
                    cid = self.get_contract()
                    if cid:
                        self.contract_id = cid

                # top up opportunistically if we happen to be docked low — but
                # never divert away from a rock to refuel: mining costs no fuel,
                # only navigation does, and the sell trip refuels at the market.
                self._maybe_refuel()

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
                    if not self._sell_off(s, contract):
                        # nothing sold/delivered/jettisoned — don't spin on a
                        # failing sale, ease off before trying again
                        self._log("sell made no progress; backing off 30s")
                        self._sleep(30)
                    tried = set()
                    continue

                if not self._has_trait(system, here, "MINERAL_DEPOSITS"):
                    rock = self._next_rock(system, tried, here)
                    if not rock:
                        self._log("no mineral deposits in system")
                        self._sleep(15)
                        continue
                    self._log(f"→ {rock['symbol']} to mine")
                    # _goto orbits-if-docked, DRIFTs if fuel-short, and awaits
                    self._goto(s, rock["symbol"])
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
                        rock = self._next_rock(system, tried, here)
                        if rock:
                            self._log(f"no {desired} at {here} → {rock['symbol']}")
                            self.surveys = []
                            self._goto(s, rock["symbol"])
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
                        if not self._sell_off(s, contract):
                            self._log("sell made no progress; backing off 30s")
                            self._sleep(30)
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
                # don't haul goods we've learned nothing in-system buys — drop
                # them at the rock so the hold fills with sellable ore instead
                if (y.get("symbol") in self._unsellable and y.get("units")
                        and y.get("symbol") != desired):
                    try:
                        self.c.jettison(self.ship, y["symbol"], y["units"])
                        self._log(f"jettisoned {y['units']} {y['symbol']} (unsellable here)")
                    except ApiError as e:
                        self._log(f"jettison failed: {e.message}")
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


class TraderBot(BaseBot):
    """Autonomous arbitrage trader.

    Records the price of every market it visits into ``store``, then buys the
    most profitable good in its current system and sells it for a gain,
    exploring unseen markets when it has no profitable route yet.
    """

    def __init__(
        self,
        client: Client,
        ship: str,
        *,
        min_profit: int = 50,
        budget: int | None = None,
        max_age_s: float = 3600.0,
        loops: int | None = None,
        cross_system: bool = False,
        max_hops: int = 2,
        hop_penalty: int = 0,
        world=None,
        on_log=None,
        on_status=None,
    ):
        super().__init__(client, ship, world=world, on_log=on_log, on_status=on_status)
        self.min_profit = min_profit
        self.budget = budget
        self.max_age_s = max_age_s
        self.loops = loops
        self.cross_system = cross_system
        self.max_hops = max_hops
        self.hop_penalty = hop_penalty

    def _buy(self, good, want, per_tx, waypoint="", ceiling=None) -> tuple[int, int]:
        """Buy up to ``want`` units in ``per_tx`` chunks, stopping if the price
        rises above ``ceiling``. Returns (units, spend)."""
        bought = spent = 0
        while bought < want and not self._cancel.is_set():
            n = min(per_tx, want - bought)
            try:
                data = self.c.purchase(self.ship, good, n)
            except ApiError as e:
                self._log(f"buy stopped: {e.message}")
                break
            t = data.get("transaction", {})
            got = t.get("units", n)
            ppu = t.get("pricePerUnit", 0)
            bought += got
            spent += t.get("totalPrice", 0)
            store.record_trade("buy", good, got, ppu, t.get("totalPrice", 0),
                               ship=self.ship, waypoint=waypoint)
            self._log(f"bought {got} {good} @ {ppu} = {t.get('totalPrice')}c")
            if got < n:
                break
            if ceiling is not None and ppu >= ceiling:
                self._log(f"buy price hit {ppu} ≥ ceiling {ceiling}; stopping to protect margin")
                break
        return bought, spent

    def _sell(self, good, per_tx, waypoint="", floor=None) -> tuple[int, int]:
        """Sell held ``good`` in ``per_tx`` chunks, stopping if the price falls
        below ``floor`` (keeps the rest for a better market). Returns (units, revenue)."""
        sold = revenue = 0
        while not self._cancel.is_set():
            held = next(
                (i["units"] for i in self.c.cargo(self.ship).get("inventory", [])
                 if i["symbol"] == good),
                0,
            )
            if held <= 0:
                break
            n = min(per_tx, held)
            try:
                data = self.c.sell(self.ship, good, n)
            except ApiError as e:
                self._log(f"sell stopped: {e.message}")
                break
            t = data.get("transaction", {})
            got = t.get("units", n)
            ppu = t.get("pricePerUnit", 0)
            sold += got
            revenue += t.get("totalPrice", 0)
            store.record_trade("sell", good, got, ppu, t.get("totalPrice", 0),
                               ship=self.ship, waypoint=waypoint)
            self._log(f"sold {got} {good} @ {ppu} = {t.get('totalPrice')}c")
            if floor is not None and ppu <= floor:
                self._log(f"sell price hit {ppu} ≤ floor {floor}; holding {held - got} for elsewhere")
                break
        return sold, revenue

    def _execute(self, route: dict, s: dict, capacity: int) -> None:
        good = route["good"]
        per_tx = max(1, route.get("volume", 1))
        margin = self.min_profit
        # size the trade so we don't spike the buy market or crash the sell one
        want = min(
            capacity,
            sustainable_units(route.get("volume"), route.get("buy_supply")),
            sustainable_units(route.get("volume"), route.get("sell_supply")),
        )
        # ease off further if our recent fills have been depressing the sell price
        sell_pts = [
            r["sell_price"]
            for r in store.price_series(good, waypoint=route["sell_wp"], limit=8)
        ]
        factor = demand_factor(sell_pts)
        if factor < 1.0 and want > 1:
            want = max(1, int(want * factor))
            self._log(f"easing {good} order to {want}u ({int(factor * 100)}%): sell price softening")
        if self.budget and route["buy"] > 0:
            want = min(want, self.budget // route["buy"])
        if want <= 0:
            self._log(f"budget too small for {good} @ {route['buy']}c")
            self._sleep(15)
            return
        buy_system = route.get("buy_system", route["system"])
        sell_system = route.get("sell_system", route["system"])
        hop_note = f"  [{route.get('hops', 0)} hop(s)]" if route.get("hops") else ""
        self._log(
            f"route {good}: buy {route['buy']} @ {route['buy_wp']} → "
            f"sell {route['sell']} @ {route['sell_wp']} (+{route['profit']}/u, "
            f"{want}u){hop_note}"
        )
        # buy leg — travel to the buy system first if it's elsewhere
        if system_of(route["buy_wp"]) != s.get("nav", {}).get("systemSymbol", ""):
            if not self._travel_to_system(buy_system):
                return
            s = self.c.ship(self.ship)
        s = self._goto(s, route["buy_wp"])
        self.c.dock(self.ship)
        self._maybe_refuel()
        self._status(mode="buy", last=f"buy {good}")
        ceiling = price_ceiling(route["sell"], margin)
        bought, spent = self._buy(good, want, per_tx, waypoint=route["buy_wp"], ceiling=ceiling)
        self._record_here(buy_system, route["buy_wp"], fresh=True)
        if bought <= 0:
            return
        # never sell below what we actually paid, plus our margin
        avg_buy = spent / bought if bought else route["buy"]
        floor = price_floor(int(avg_buy), margin)
        # sell leg — travel to the sell system if different
        if sell_system != s.get("nav", {}).get("systemSymbol", ""):
            if not self._travel_to_system(sell_system):
                self._log("stranded with cargo; will offload next cycle")
                return
            s = self.c.ship(self.ship)
        s = self._goto(s, route["sell_wp"])
        self.c.dock(self.ship)
        self._maybe_refuel()
        self._status(mode="sell", last=f"sell {good}")
        sold, revenue = self._sell(good, per_tx, waypoint=route["sell_wp"], floor=floor)
        self._record_here(sell_system, route["sell_wp"], fresh=True)
        self._log(f"trade done {good}: spent {spent}c, earned {revenue}c → net {revenue - spent}c")

    def _offload(self, s: dict, system: str) -> None:
        """Sell any leftover cargo (e.g. from an interrupted trade) — at
        markets that actually list each good; jettison what nothing buys."""
        inv = s.get("cargo", {}).get("inventory", [])
        if not inv:
            return
        markets = {w["symbol"]: self._market(system, w["symbol"])
                   for w in self._wps(system, trait="MARKETPLACE")}
        plan, unsellable = plan_sales(inv, markets)
        if not plan and not unsellable:
            self._log("leftover cargo but no market to sell at")
            return
        for wp, goods in plan:
            self._log(f"offloading leftover cargo → {wp}")
            s = self._goto(s, wp)
            self.c.dock(self.ship)
            for item in list(inv):
                if item["symbol"] in goods:
                    self._sell(item["symbol"], item.get("units", 1) or 1, waypoint=wp)
            self._record_here(system, wp)
        if not any(m for m in markets.values()):
            return  # zero market data — keep the cargo, this was bought
        held = {i["symbol"]: i["units"]
                for i in self.c.cargo(self.ship).get("inventory", [])}
        for good in unsellable:
            units = held.get(good, 0)
            if units <= 0:
                continue
            try:
                self.c.jettison(self.ship, good, units)
                self._log(f"jettisoned {units} {good} (no market in {system} buys it)")
            except ApiError as e:
                self._log(f"jettison failed for {good}: {e.message}")

    # -- main loop ---------------------------------------------------------
    def run(self) -> None:
        self._log("trader engaged")
        iteration = 0
        try:
            while not self._cancel.is_set():
                if self.loops is not None and iteration >= self.loops:
                    break
                iteration += 1
                s = self._await_arrival()
                nav = s.get("nav", {})
                system = nav.get("systemSymbol", "")
                here = nav.get("waypointSymbol", "")
                cargo = s.get("cargo", {})
                capacity = cargo.get("capacity", 0)
                units = cargo.get("units", 0)

                # record where we are, if it's a market
                self._record_here(system, here)
                if self.cross_system:
                    self._map_gate(system)

                # clear leftover cargo before starting a fresh trade
                if units > 0:
                    self._offload(s, system)
                    continue

                hops = self.max_hops if self.cross_system else 0
                routes = store.best_routes(
                    system, self.min_profit, self.max_age_s,
                    max_hops=hops, hop_penalty=self.hop_penalty,
                )
                # deconflict: take the best route no other trader has claimed
                route = claims.pick_unclaimed(routes, self.ship) if routes else None
                if route:
                    claims.claim(route, self.ship)
                    try:
                        self._execute(route, s, capacity)
                    finally:
                        claims.release(route, self.ship)
                    continue
                if routes:
                    self._log("all profitable routes claimed by other traders; exploring")

                # no known profitable route — scan an unseen market in-system
                nxt = self._next_unscanned_market(system, here)
                if nxt:
                    self._log(f"exploring markets → {nxt}")
                    self._goto(s, nxt)
                    continue

                # then, if allowed, jump to a neighbour system to gather prices
                if self.cross_system:
                    nsys = self._next_unscanned_system(system)
                    if nsys:
                        self._log(f"exploring system → {nsys}")
                        if self._travel_to_system(nsys):
                            continue

                self._log(
                    f"no route ≥ {self.min_profit}c reachable from {system}; waiting"
                )
                self._status(mode="idle", last="no route")
                self._sleep(30)
        except BotCancelled:
            pass
        except Exception as e:
            self._log(f"halted: {e!r}")
        finally:
            self._status(mode="stopped", last="idle")
            self._log("disengaged")


class ScoutBot(BaseBot):
    """A probe/satellite that tours markets recording live prices.

    Scouts carry no cargo, so instead of trading they keep the price store fresh
    for the traders and the analytics pane. ``max_age_s`` doubles as the re-scan
    interval: a market observed longer ago than that counts as unscanned again,
    so the scout keeps cycling and prices never go stale.
    """

    def __init__(
        self,
        client: Client,
        ship: str,
        *,
        cross_system: bool = False,
        max_hops: int = 1,
        max_age_s: float = 600.0,
        dwell: int = 45,
        explore: bool = False,
        world=None,
        on_log=None,
        on_status=None,
    ):
        super().__init__(client, ship, world=world, on_log=on_log, on_status=on_status)
        self.cross_system = cross_system
        self.max_hops = max_hops
        self.max_age_s = max_age_s
        self.dwell = dwell
        # explore mode: also chart UNCHARTED waypoints to widen the world model
        self.explore = explore

    def _chart_here(self, system: str, here: str) -> None:
        """Chart the current waypoint if it's still uncharted (explore goal)."""
        if not self.explore or self.world is None:
            return
        for w in self.world.get_waypoints(system):
            if w["symbol"] == here and "UNCHARTED" in (w.get("traits") or []):
                try:
                    self.c.chart(self.ship)
                    self._log(f"charted {here}")
                except ApiError as e:
                    self._log(f"chart skipped: {e.message}")
                return

    def run(self) -> None:
        self._log("scout engaged" + (" (explore)" if self.explore else ""))
        try:
            while not self._cancel.is_set():
                s = self._await_arrival()
                nav = s.get("nav", {})
                system = nav.get("systemSymbol", "")
                here = nav.get("waypointSymbol", "")

                self._chart_here(system, here)
                self._record_here(system, here)
                if self.cross_system:
                    self._map_gate(system)

                nxt = self._next_unscanned_market(system, here)
                if nxt:
                    self._status(mode="scout", last=f"→ {nxt}")
                    self._goto(s, nxt)
                    continue

                if self.cross_system:
                    nsys = self._next_unscanned_system(system)
                    if nsys and self._travel_to_system(nsys):
                        continue

                # everything fresh — idle, then loop to re-scan as prices age out
                self._status(mode="scout", last="all fresh; dwelling")
                self._sleep(self.dwell)
        except BotCancelled:
            pass
        except Exception as e:
            self._log(f"halted: {e!r}")
        finally:
            self._status(mode="stopped", last="idle")
            self._log("disengaged")


class ConstructionBot(BaseBot):
    """Supplies a construction site (the endgame jump gate).

    Buys the cheapest known source of each still-needed material and delivers it
    to the site via the ``construct`` endpoint, until the site is complete. When
    no market for a needed good is known yet, it scans in-system to find one.
    """

    def __init__(
        self,
        client: Client,
        ship: str,
        *,
        target: str,
        world=None,
        on_log=None,
        on_status=None,
    ):
        super().__init__(client, ship, world=world, on_log=on_log, on_status=on_status)
        self.target = target                      # construction-site waypoint
        self.target_system = system_of(target)

    def _supply(self, s: dict, good: str, units: int) -> dict:
        s = self._goto(s, self.target)
        if s.get("nav", {}).get("status") != "DOCKED":
            self.c.dock(self.ship)
        try:
            res = self.c.supply_construction(self.target_system, self.target, self.ship, good, units)
            self._log(f"supplied {units} {good} → {self.target}")
            return res.get("construction", {})
        except ApiError as e:
            self._log(f"supply failed: {e.message}")
            return {}

    def _acquire(self, s: dict, good: str, need: int) -> None:
        obs = store.latest_prices(max_age_s=self.max_age_s)
        wp, price = cheapest_source(good, obs)
        if not wp:
            self._log(f"no known market sells {good}; scanning")
            here = s.get("nav", {}).get("waypointSymbol", "")
            nxt = self._next_unscanned_market(self.target_system, here)
            if nxt:
                self._goto(s, nxt)
            else:
                self._sleep(30)
            return
        capacity = s.get("cargo", {}).get("capacity", 0) or need
        want = min(need, capacity)
        s = self._goto(s, wp)
        self.c.dock(self.ship)
        self._maybe_refuel()
        bought = 0
        while bought < want and not self._cancel.is_set():
            try:
                data = self.c.purchase(self.ship, good, want - bought)
            except ApiError as e:
                self._log(f"buy stopped: {e.message}")
                break
            t = data.get("transaction", {})
            got = t.get("units", 0)
            if got <= 0:
                break
            bought += got
            store.record_trade("buy", good, got, t.get("pricePerUnit", 0),
                               t.get("totalPrice", 0), ship=self.ship, waypoint=wp)
            self._log(f"bought {got} {good} @ {t.get('pricePerUnit')}c for construction")
        self._record_here(self.target_system, wp, fresh=True)

    def run(self) -> None:
        self._log(f"construction supplier engaged → {self.target}")
        try:
            while not self._cancel.is_set():
                s = self._await_arrival()
                try:
                    con = self.c.construction(self.target_system, self.target)
                except ApiError as e:
                    self._log(f"no construction site at {self.target}: {e.message}")
                    return
                if is_complete(con):
                    self._log("construction complete ✓")
                    return
                gap = materials_gap(con)
                # already carrying something the site needs? deliver it first
                inv = {i["symbol"]: i["units"]
                       for i in s.get("cargo", {}).get("inventory", [])}
                carrying = next((g for g in gap if inv.get(g)), None)
                if carrying:
                    self._status(mode="supply", last=f"→ {self.target}")
                    self._supply(s, carrying, min(gap[carrying], inv[carrying]))
                    continue
                good = next_material(con, store.latest_prices(max_age_s=self.max_age_s))
                if not good:
                    continue
                self._status(mode="buy", last=f"source {good}")
                self._acquire(s, good, gap[good])
        except BotCancelled:
            pass
        except Exception as e:
            self._log(f"halted: {e!r}")
        finally:
            self._status(mode="stopped", last="idle")
            self._log("disengaged")
