from __future__ import annotations

import datetime as dt
import re
import threading
import time

import claims
import config
import store
from api import ApiError, Client
from arbitrage import price_ceiling, price_floor, sustainable_units
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
        on_log=None,
        on_status=None,
    ):
        self.c = client
        self.ship = ship
        self.on_log = on_log or (lambda m: None)
        self.on_status = on_status or (lambda **k: None)
        self._cancel = threading.Event()
        # exploration defaults (subclasses override as needed)
        self.max_age_s = 3600.0
        self.max_hops = 2
        self.cross_system = False
        self._gated: set[str] = set()

    def stop(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

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
    def _record_here(self, system: str, waypoint: str) -> dict | None:
        try:
            m = self.c.market(system, waypoint)
        except ApiError:
            return None
        n = store.record_market(m)
        if n:
            self._log(f"scanned {waypoint} ({n} goods)")
        return m

    def _first_market(self, system: str) -> str | None:
        for w in self.c.waypoints(system, filters={"traits": "MARKETPLACE"}):
            return w["symbol"]
        return None

    def _next_unscanned_market(self, system: str, here: str) -> str | None:
        fresh = {
            o["waypoint"]
            for o in store.latest_prices(system=system, max_age_s=self.max_age_s)
        }
        for w in self.c.waypoints(system, filters={"traits": "MARKETPLACE"}):
            if w["symbol"] != here and w["symbol"] not in fresh:
                return w["symbol"]
        return None

    def _map_gate(self, system: str) -> None:
        """Record ``system``'s jump gate and its connections (once per system)."""
        if system in self._gated:
            return
        self._gated.add(system)
        try:
            gates = self.c.waypoints(system, filters={"type": "JUMP_GATE"})
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
        on_log=None,
        on_status=None,
    ):
        super().__init__(client, ship, on_log=on_log, on_status=on_status)
        self.contract_id = contract
        self.sell = sell
        self.surveys: list[dict] = []

    # -- internals ---------------------------------------------------------
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
        # 3) sell the rest at a market
        s = self.c.ship(self.ship)
        nav = s.get("nav", {})
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
        if nav["waypointSymbol"] != market:
            self._status(mode="sell", last=f"→ {market}")
            s = self._goto(s, market)
        self.c.dock(self.ship)
        self._maybe_refuel()
        for item in inv:
            try:
                data = self.c.sell(self.ship, item["symbol"], item["units"])
                t = data.get("transaction", {})
                store.record_trade("sell", item["symbol"], t.get("units", item["units"]),
                                   t.get("pricePerUnit", 0), t.get("totalPrice", 0),
                                   ship=self.ship, waypoint=market)
                self._log(
                    f"sold {t.get('units', item['units'])} {item['symbol']} "
                    f"@ {t.get('pricePerUnit')} = {t.get('totalPrice')}c"
                )
            except ApiError as e:
                self._log(f"can't sell {item['symbol']}: {e.message}")
        # record the fresh prices we just saw here
        try:
            store.record_market(self.c.market(nav["systemSymbol"], market))
        except ApiError:
            pass

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
        on_log=None,
        on_status=None,
    ):
        super().__init__(client, ship, on_log=on_log, on_status=on_status)
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
        self._record_here(buy_system, route["buy_wp"])
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
        self._record_here(sell_system, route["sell_wp"])
        self._log(f"trade done {good}: spent {spent}c, earned {revenue}c → net {revenue - spent}c")

    def _offload(self, s: dict, system: str) -> None:
        """Sell any leftover cargo (e.g. from an interrupted trade)."""
        inv = s.get("cargo", {}).get("inventory", [])
        if not inv:
            return
        market = self._first_market(system)
        if not market:
            self._log("leftover cargo but no market to sell at")
            return
        self._log(f"offloading leftover cargo → {market}")
        s = self._goto(s, market)
        self.c.dock(self.ship)
        for item in list(inv):
            self._sell(item["symbol"], item.get("units", 1) or 1, waypoint=market)
        self._record_here(system, market)

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
        on_log=None,
        on_status=None,
    ):
        super().__init__(client, ship, on_log=on_log, on_status=on_status)
        self.cross_system = cross_system
        self.max_hops = max_hops
        self.max_age_s = max_age_s
        self.dwell = dwell

    def run(self) -> None:
        self._log("scout engaged")
        try:
            while not self._cancel.is_set():
                s = self._await_arrival()
                nav = s.get("nav", {})
                system = nav.get("systemSymbol", "")
                here = nav.get("waypointSymbol", "")

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
