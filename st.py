from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import time
from typing import Any

import config
import store
from api import ApiError, Client


# -- formatting helpers -----------------------------------------------------
def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts


def _credits(n: int | float) -> str:
    return f"{int(n):,}c"


def _system_of(waypoint: str) -> str:
    m = re.match(r"^(X1-[A-Z0-9]+)-", waypoint)
    return m.group(1) if m else waypoint


def _wait_seconds(target: str | None, now_field: str | None = None) -> int:
    if not target:
        return 0
    try:
        t = dt.datetime.fromisoformat(target.replace("Z", "+00:00"))
        delta = (t - dt.datetime.now(dt.timezone.utc)).total_seconds()
        return max(0, int(delta))
    except ValueError:
        return 0


def hr(label: str = "") -> None:
    if label:
        print(f"\n== {label} ==")
    else:
        print("-" * 60)


def show_agent(a: dict) -> None:
    print(f"Agent  {a['symbol']}  [{a.get('startingFaction', '?')}]")
    print(f"HQ     {a.get('headquarters')}")
    print(f"Credits {_credits(a.get('credits', 0))}   Ships {a.get('shipCount')}")


def show_ship(s: dict, verbose: bool = False) -> None:
    nav = s.get("nav", {})
    cargo = s.get("cargo", {})
    fuel = s.get("fuel", {})
    frame = s.get("frame", {})
    reg = s.get("registration", {})
    print(f"{s['symbol']}  {frame.get('name', '?')}  [{reg.get('role', '?')}]")
    print(
        f"  nav: {nav.get('status')} @ {nav.get('waypointSymbol')} "
        f"(mode {nav.get('flightMode')})"
    )
    route = nav.get("route", {})
    arr = route.get("arrival") if route else None
    if nav.get("status") == "IN_TRANSIT" and arr:
        print(f"  arrives in {_wait_seconds(arr)}s  -> {route['destination']['symbol']}")
    print(
        f"  fuel {fuel.get('current')}/{fuel.get('capacity')}   "
        f"cargo {cargo.get('units')}/{cargo.get('capacity')}"
    )
    inv = cargo.get("inventory", [])
    if inv:
        print("  cargo: " + ", ".join(f"{i['units']} {i['symbol']}" for i in inv))
    cd = s.get("cooldown")
    if cd and cd.get("remainingSeconds"):
        print(f"  cooldown {cd['remainingSeconds']}s")
    if verbose:
        mounts = [m["name"] for m in s.get("mounts", [])]
        modules = [m["name"] for m in s.get("modules", [])]
        if mounts:
            print("  mounts: " + ", ".join(mounts))
        if modules:
            print("  modules: " + ", ".join(modules))


def show_contract(c: dict) -> None:
    status = "ACCEPTED" if c.get("accepted") else "PENDING"
    if c.get("fulfilled"):
        status = "DONE"
    terms = c.get("terms", {})
    pay = terms.get("payment", {})
    print(f"Contract {c['id']}  [{c['type']}] {status}")
    print(f"  faction {c.get('factionSymbol')}  deadline {_fmt_ts(c.get('deadline'))}")
    if not c.get("accepted") and c.get("deadlineToAccept"):
        print(f"  must accept by {_fmt_ts(c.get('deadlineToAccept'))}")
    elif c.get("expiration"):
        print(f"  expires {_fmt_ts(c.get('expiration'))}")
    print(f"  pay  accept {_credits(pay.get('onAccepted', 0))}  fulfill {_credits(pay.get('onFulfilled', 0))}")
    for d in terms.get("deliver", []):
        done = "OK" if d["unitsFulfilled"] >= d["unitsRequired"] else "..."
        print(
            f"  deliver {d['unitsFulfilled']}/{d['unitsRequired']} {d['tradeSymbol']} "
            f"-> {d['destinationSymbol']} {done}"
        )


def show_market(m: dict) -> None:
    print(f"Market @ {m.get('symbol')}")
    imports = m.get("imports", [])
    exports = m.get("exports", [])
    tx = m.get("transactions", [])
    goods = {g["symbol"]: g for g in m.get("tradeGoods", [])}
    if goods:
        print("  trade goods (live):")
        rows = sorted(
            goods.values(),
            key=lambda g: (g.get("type", ""), -g.get("purchasePrice", 0)),
        )
        for g in rows:
            print(
                f"    {g['symbol']:<22} {g.get('type',''):<8} "
                f"buy {g.get('purchasePrice','-'):>5}  sell {g.get('sellPrice','-'):>5}"
            )
    elif imports or exports:
        sym = {g["symbol"] for g in imports} | {g["symbol"] for g in exports}
        print("  deals (catalog only, no live prices): " + ", ".join(sorted(sym)))


def show_waypoint(w: dict) -> None:
    traits = ", ".join(t["name"] for t in w.get("traits", []))
    print(
        f"{w['symbol']}  {w.get('type','?')}  @({w.get('x')},{w.get('y')})  "
        f"isCharted={w.get('chart',{}).get('isCharted','?')}"
    )
    if traits:
        print(f"  traits: {traits}")


# -- commands ---------------------------------------------------------------
def cmd_register(args) -> None:
    data = Client.register(args.symbol.upper(), args.faction.upper())
    token = data["token"]
    print(f"Registered {data['agent']['symbol']} ({data['agent']['startingFaction']}).")
    print("Save this token to .env as ST_AGENT_TOKEN:\n")
    print(token)
    print("\nAgent:", data["agent"])


def cmd_agent(args, c: Client) -> None:
    show_agent(c.my_agent())


def cmd_ships(args, c: Client) -> None:
    for s in c.ships():
        show_ship(s, verbose=args.v)
        print()


def cmd_ship(args, c: Client) -> None:
    show_ship(c.ship(args.symbol), verbose=True)


def cmd_contracts(args, c: Client) -> None:
    for ct in c.contracts():
        show_contract(ct)
        print()


def cmd_accept(args, c: Client) -> None:
    data = c.accept_contract(args.contract_id)
    print("Accepted contract.")
    show_contract(data.get("contract", data))


def cmd_negotiate(args, c: Client) -> None:
    data = c.negotiate_contract(args.ship)
    show_contract(data.get("contract", data))


def cmd_fulfill(args, c: Client) -> None:
    data = c.fulfill_contract(args.contract_id)
    print("Fulfilled contract.")
    show_agent(data.get("agent", {}))


def cmd_waypoints(args, c: Client) -> None:
    system = args.system or _system_of(config.HQ) or "X1-N85"
    filters: dict[str, Any] = {}
    if args.type:
        filters["waypointType"] = args.type
    if args.traits:
        filters["traits"] = ",".join(args.traits)
    wps = c.waypoints(system, filters=filters or None)
    for w in wps:
        show_waypoint(w)
    print(f"\n{len(wps)} waypoints in {system}")


def cmd_waypoint(args, c: Client) -> None:
    system = args.system or _system_of(args.symbol)
    show_waypoint(c.waypoint(system, args.symbol))


def cmd_market(args, c: Client) -> None:
    system = args.system or _system_of(args.waypoint)
    m = c.market(system, args.waypoint)
    n = store.record_market(m)
    show_market(m)
    if n:
        print(f"  (recorded {n} live prices)")


def cmd_shipyard(args, c: Client) -> None:
    system = args.system or _system_of(args.waypoint)
    data = c.shipyard(system, args.waypoint)
    print(f"Shipyard @ {data.get('symbol')}")
    for t in data.get("ships", []):
        st = t.get("purchasePrice", "?")
        print(f"  {t.get('type','?'):<28} {st}")


def cmd_buyship(args, c: Client) -> None:
    from fleet import find_offer

    ship_type = args.ship_type.upper()
    system = args.system or _system_of(config.HQ) or _system_of(args.waypoint or "")
    chosen_wp, price = find_offer(c, system, ship_type, args.waypoint)
    if not chosen_wp:
        print(f"No shipyard in {system} sells {ship_type}.")
        return
    credits = c.my_agent().get("credits", 0)
    if price is not None:
        if args.max_price and price > args.max_price:
            print(f"{ship_type} costs {price}c at {chosen_wp}, above --max-price {args.max_price}c.")
            return
        if price > credits:
            print(f"{ship_type} costs {price}c but you only have {_credits(credits)}.")
            return
    data = c.purchase_ship(ship_type, chosen_wp)
    new_ship = data.get("ship", {})
    tx = data.get("transaction", {})
    ag = data.get("agent", {})
    print(
        f"Purchased {new_ship.get('symbol', '?')} ({ship_type}) at {chosen_wp} "
        f"for {_credits(tx.get('price', price or 0))}.  "
        f"Credits now {_credits(ag.get('credits', credits))}."
    )


def cmd_orbit(args, c: Client) -> None:
    s = c.orbit(args.ship)["nav"]
    print(f"{args.ship} now {s['status']} @ {s['waypointSymbol']}")


def cmd_dock(args, c: Client) -> None:
    s = c.dock(args.ship)["nav"]
    print(f"{args.ship} now {s['status']} @ {s['waypointSymbol']}")


def cmd_refuel(args, c: Client) -> None:
    data = c.refuel(args.ship)
    a = data.get("agent", {})
    f = data.get("fuel", {})
    print(
        f"{args.ship} refueled to {f.get('current')}/{f.get('capacity')} "
        f"for {_credits(a.get('transaction', {}).get('totalPrice', data.get('transaction', {}).get('totalPrice', 0)))}"
    )


def cmd_navigate(args, c: Client) -> None:
    s = c.ship(args.ship)
    nav = s.get("nav", {})
    if nav.get("flightMode") != "DRIFT" and nav.get("status") == "IN_TRANSIT":
        print(f"{args.ship} already in transit.")
        show_ship(s)
        return
    data = c.navigate(args.ship, args.waypoint)
    s2 = data.get("ship", data)
    nav2 = s2.get("nav", {})
    fuel = s2.get("fuel", {})
    route = nav2.get("route", {})
    print(
        f"{args.ship} -> {args.waypoint}  status {nav2.get('status')}  "
        f"fuel {fuel.get('current')}/{fuel.get('capacity')}  "
        f"arrives in {_wait_seconds(route.get('arrival'))}s"
    )


def cmd_extract(args, c: Client) -> None:
    data = c.extract(args.ship)
    cd = data.get("cooldown", {})
    inv = data.get("cargo", data.get("extraction", {}))
    extracted = data.get("extraction", {}).get("yield")
    if extracted:
        print(f"Extracted {extracted.get('units')} {extracted.get('symbol')}")
    else:
        print("Extracted (no yield info).")
    if cd:
        print(f"  cooldown {cd.get('totalSeconds')}s")
    cargo = data.get("cargo")
    if cargo:
        print(f"  cargo {cargo.get('units')}/{cargo.get('capacity')}")


def cmd_survey(args, c: Client) -> None:
    surveys = c.survey(args.ship)
    if not surveys:
        print(f"{args.ship} produced no surveys (on cooldown or unsupported).")
        return
    for sv in surveys:
        deps = ", ".join(f"{d['symbol']}({d.get('size','')})" for d in sv.get("deposits", []))
        print(f"{sv['symbol']}  {deps}  expires {_fmt_ts(sv.get('expiration'))}")


def cmd_sell(args, c: Client) -> None:
    s = c.ship(args.ship)
    inv = {i["symbol"]: i["units"] for i in s.get("cargo", {}).get("inventory", [])}
    units = args.units
    if units is None:
        if args.trade not in inv:
            raise ApiError(0, f"{args.trade} not in cargo of {args.ship}")
        units = inv[args.trade]
    data = c.sell(args.ship, args.trade, units)
    t = data.get("transaction", {})
    ag = data.get("agent", {})
    print(
        f"Sold {t.get('units', units)} {t.get('symbol', args.trade)} "
        f"@ {t.get('pricePerUnit','?')} = {_credits(t.get('totalPrice', 0))}  "
        f"credits now {_credits(ag.get('credits', 0))}"
    )


def cmd_purchase(args, c: Client) -> None:
    data = c.purchase(args.ship, args.trade, args.units)
    t = data.get("transaction", {})
    ag = data.get("agent", {})
    print(
        f"Bought {t.get('units', args.units)} {t.get('symbol', args.trade)} "
        f"@ {t.get('pricePerUnit','?')} = {_credits(t.get('totalPrice', 0))}  "
        f"credits now {_credits(ag.get('credits', 0))}"
    )


def cmd_jettison(args, c: Client) -> None:
    data = c.jettison(args.ship, args.trade, args.units)
    cargo = data.get("cargo", {})
    print(f"Jettisoned. cargo {cargo.get('units')}/{cargo.get('capacity')}")


def cmd_cooldown(args, c: Client) -> None:
    cd = c.cooldown(args.ship)
    if not cd or cd.get("remainingSeconds") in (None, 0):
        print(f"{args.ship} ready.")
    else:
        print(f"{args.ship} cooldown {cd.get('remainingSeconds')}s")


# -- autopilot --------------------------------------------------------------
def _await_arrival(c: Client, ship: str) -> dict:
    s = c.ship(ship)
    nav = s.get("nav", {})
    while nav.get("status") == "IN_TRANSIT":
        secs = _wait_seconds(nav.get("route", {}).get("arrival"))
        if secs:
            print(f"  ...{ship} in transit, waiting {secs}s")
            time.sleep(secs + 1)
        s = c.ship(ship)
        nav = s.get("nav", {})
    return s


def _await_cooldown(c: Client, ship: str) -> None:
    cd = c.cooldown(ship)
    while cd.get("remainingSeconds"):
        secs = cd["remainingSeconds"]
        print(f"  ...{ship} cooldown {secs}s")
        time.sleep(secs + 1)
        cd = c.cooldown(ship)


def _navigate_smart(c: Client, ship: str, waypoint: str, *, prefer_mode: str = "CRUISE") -> dict:
    """Navigate in CRUISE. Caller must ensure fuel via _ensure_fuel first."""
    return c.navigate(ship, waypoint)


def _ensure_fuel(c: Client, ship: str, system: str, threshold: float = 0.5) -> bool:
    """Divert to a fuel market and refuel if fuel ratio < threshold.

    Returns True if a refuel trip was started/done (caller should re-loop).
    """
    s = c.ship(ship)
    nav = s.get("nav", {})
    if nav.get("status") == "IN_TRANSIT":
        return False
    fuel = s.get("fuel", {})
    cap = fuel.get("capacity", 0)
    cur = fuel.get("current", 0)
    if cap == 0 or cur / cap >= threshold:
        return False
    target = config.HQ
    print(f"  fuel low ({cur}/{cap}); diverting to {target} to refuel")
    if nav.get("waypointSymbol") != target:
        if nav.get("status") == "DOCKED":
            c.orbit(ship)
        try:
            c.navigate(ship, target)
        except ApiError as e:
            if e.code == 4203 or "fuel" in e.message.lower():
                print("  CRUISE fuel short; using DRIFT to reach fuel station")
                c.set_flight_mode(ship, "DRIFT")
                c.navigate(ship, target)
            else:
                raise
        return True
    if nav.get("status") != "DOCKED":
        c.dock(ship)
    _maybe_refuel(c, ship, threshold=0.0)
    try:
        c.set_flight_mode(ship, "CRUISE")
    except ApiError:
        pass
    return True


def _find_waypoint(c: Client, system: str, trait: str) -> dict | None:
    for w in c.waypoints(system, filters={"traits": trait}):
        return w
    return None


def _next_rock(c: Client, system: str, avoid: set[str]) -> dict | None:
    for w in c.waypoints(system, filters={"traits": "MINERAL_DEPOSITS"}):
        if w["symbol"] not in avoid:
            return w
    return None


def cmd_autopilot(args, c: Client) -> None:
    """Mine ore with SHIP and optionally sell / fulfill a procurement contract."""
    ship = args.ship
    print(f"Autopilot engaged on {ship}. Ctrl+C to stop.")
    loops = args.loops
    iteration = 0
    surveys: list[dict] = []
    tried_rocks: set[str] = set()
    try:
        while loops is None or iteration < loops:
            iteration += 1
            s = _await_arrival(c, ship)
            nav = s["nav"]
            system = nav["systemSymbol"]
            here = nav["waypointSymbol"]
            cargo = s.get("cargo", {})
            capacity = cargo.get("capacity", 0)
            units = cargo.get("units", 0)

            # surveys are per-waypoint; drop stale ones after a move
            surveys = [s for s in surveys if s.get("symbol") == here]

            # keep the tank topped up before doing anything else
            if _ensure_fuel(c, ship, system):
                continue

            # decide what to mine for
            contract = None
            if args.contract:
                contract = c.contract(args.contract)
            desired = None
            if contract and not contract.get("fulfilled"):
                for d in contract["terms"]["deliver"]:
                    if d["unitsFulfilled"] < d["unitsRequired"]:
                        desired = (d["tradeSymbol"], d["destinationSymbol"])
                        break

            # if full, go sell / deliver
            if units >= capacity or (capacity == 0):
                print(f"[{iteration}] cargo full ({units}/{capacity}); heading to market.")
                _sell_off(c, ship, system, contract=contract, sell_all=args.sell)
                continue

            # move to an asteroid field if not at one
            if not _has_trait(c, system, here, "MINERAL_DEPOSITS"):
                wp = _find_waypoint(c, system, "MINERAL_DEPOSITS")
                if not wp:
                    raise ApiError(0, f"No mineral deposits found in {system}")
                print(f"[{iteration}] navigating to {wp['symbol']} to mine.")
                c.dock(ship) if nav["status"] == "DOCKED" else None
                c.orbit(ship)
                _navigate_smart(c, ship, wp["symbol"])
                continue

            # orbit + extract
            if nav["status"] != "IN_ORBIT":
                c.orbit(ship)
            _await_cooldown(c, ship)

            desired_good = desired[0] if desired else None
            if desired_good and not surveys:
                try:
                    surveys = c.survey(ship)
                    _await_cooldown(c, ship)
                    hits = [
                        f"{s['symbol']}:{','.join(d['symbol'] for d in s.get('deposits',[]))}"
                        for s in surveys
                    ]
                    print(f"[{iteration}] surveyed {here}: {hits}")
                except ApiError as e:
                    print(f"[{iteration}] survey unavailable: {e.message}")

            # relocate if desired good isn't present at this rock
            if desired_good and surveys:
                has_good = any(
                    d.get("symbol") == desired_good
                    for sv in surveys
                    for d in sv.get("deposits", [])
                )
                if not has_good:
                    tried_rocks.add(here)
                    nxt = _next_rock(c, system, avoid=tried_rocks)
                    if nxt:
                        print(
                            f"[{iteration}] no {desired_good} at {here}; "
                            f"relocating to {nxt['symbol']}"
                        )
                        if nav["status"] != "IN_ORBIT":
                            c.orbit(ship)
                        _navigate_smart(c, ship, nxt["symbol"])
                        surveys = []
                        continue
                    print(f"[{iteration}] no {desired_good} in surveyed rocks; mining raw.")
                    surveys = []
                else:
                    tried_rocks.discard(here)

            chosen = _pick_survey(surveys, desired_good)
            label = f" (survey {chosen['symbol']})" if chosen else ""
            print(f"[{iteration}] extracting at {here}{label} ({capacity-units} free)")
            try:
                data = c.extract(ship, survey=chosen)
            except ApiError as e:
                if chosen and e.code in (4221, 4222, 4044):
                    surveys = [s for s in surveys if s.get("signature") != chosen.get("signature")]
                    print(f"[{iteration}] survey stale/exhausted, retrying.")
                    continue
                if e.code == 4228 or "cargo" in e.message.lower():
                    print(f"[{iteration}] cargo full, going to sell.")
                    _sell_off(c, ship, system, contract=contract, sell_all=args.sell)
                    continue
                raise
            y = data.get("extraction", {}).get("yield", {})
            cd = data.get("cooldown", {})
            print(
                f"[{iteration}] +{y.get('units',0)} {y.get('symbol','')} "
                f"(cooldown {cd.get('totalSeconds',0)}s)"
            )
            if desired_good and y.get("symbol") != desired_good and chosen:
                # survey yielded a different deposit than hoped; drop it to try a better one
                surveys = [s for s in surveys if s.get("signature") != chosen.get("signature")]
    except KeyboardInterrupt:
        print("\nAutopilot disengaged.")


def _has_trait(c: Client, system: str, waypoint: str, trait: str) -> bool:
    w = c.waypoint(system, waypoint)
    return any(t["symbol"] == trait for t in w.get("traits", []))


def _pick_survey(surveys: list[dict], desired_good: str | None) -> dict | None:
    """Choose the survey most likely to yield desired_good (largest matching deposit)."""
    if not surveys:
        return None
    if not desired_good:
        return surveys[0]
    best: dict | None = None
    best_size = -1
    for sv in surveys:
        for d in sv.get("deposits", []):
            if d.get("symbol") == desired_good and d.get("size") is not None:
                size_rank = {"SMALL": 1, "MODERATE": 2, "LARGE": 3, "RICH": 4}.get(
                    d.get("size", ""), 0
                )
                if size_rank > best_size:
                    best, best_size = sv, size_rank
    return best or surveys[0]


def _sell_off(c: Client, ship: str, system: str, *, contract: dict | None, sell_all: bool):
    s = _await_arrival(c, ship)
    nav = s["nav"]
    cargo = s.get("cargo", {})
    inv = cargo.get("inventory", [])

    # 1) deliver contract goods first (via the /deliver endpoint)
    if contract and not contract.get("fulfilled"):
        cid = contract["id"]
        deliver_goods = {}
        for d in contract["terms"]["deliver"]:
            need = d["unitsRequired"] - d["unitsFulfilled"]
            if need > 0 and d["tradeSymbol"] in {i["symbol"] for i in inv}:
                deliver_goods[d["tradeSymbol"]] = (min(need, _inv_units(inv, d["tradeSymbol"])), d["destinationSymbol"])
        for good, (units, destination) in deliver_goods.items():
            print(f"  delivering {units} {good} -> {destination}")
            if nav["waypointSymbol"] != destination:
                c.dock(ship) if nav["status"] == "DOCKED" else None
                if nav["status"] != "IN_ORBIT":
                    c.orbit(ship)
                _navigate_smart(c, ship, destination)
                s = _await_arrival(c, ship)
                nav = s["nav"]
            c.dock(ship)
            contract = c.deliver_contract(cid, ship, good, units).get("contract", contract)
            nav = {"waypointSymbol": destination, "status": "DOCKED"}
            print(f"  delivered {units} {good} to {destination}")
        # fulfill once every deliverable is complete
        if contract and not contract.get("fulfilled") and all(
            dd["unitsFulfilled"] >= dd["unitsRequired"] for dd in contract["terms"]["deliver"]
        ):
            data = c.fulfill_contract(cid)
            print(f"  contract fulfilled; credits now {_credits(data.get('agent', {}).get('credits', 0))}")

    # 2) sell remaining cargo at best marketplace
    s = _await_arrival(c, ship)
    s = c.ship(ship)
    inv = s.get("cargo", {}).get("inventory", [])
    if not inv:
        print("  cargo empty.")
        return
    market_wp = _find_market(c, system)
    if not market_wp:
        print(f"  no marketplace in {system}; can't sell.")
        return
    if s["nav"]["waypointSymbol"] != market_wp["symbol"]:
        if s["nav"]["status"] != "IN_ORBIT":
            c.orbit(ship)
        print(f"  navigating to market {market_wp['symbol']}")
        _navigate_smart(c, ship, market_wp["symbol"])
        s = _await_arrival(c, ship)
    c.dock(ship)
    _maybe_refuel(c, ship)
    for item in inv:
        units = item["units"]
        if units <= 0:
            continue
        try:
            data = c.sell(ship, item["symbol"], units)
            t = data.get("transaction", {})
            print(f"  sold {units} {item['symbol']} @ {t.get('pricePerUnit')} = {t.get('totalPrice')}c")
        except ApiError as e:
            print(f"  ! couldn't sell {item['symbol']}: {e.message}")


def _inv_units(inv: list[dict], symbol: str) -> int:
    for i in inv:
        if i["symbol"] == symbol:
            return i["units"]
    return 0


def _find_market(c: Client, system: str) -> dict | None:
    for w in c.waypoints(system, filters={"traits": "MARKETPLACE"}):
        return w
    return None


def _maybe_refuel(c: Client, ship: str, threshold: float = 0.4) -> None:
    s = c.ship(ship)
    nav = s.get("nav", {})
    fuel = s.get("fuel", {})
    if nav.get("status") != "DOCKED":
        return
    cap = fuel.get("capacity", 0)
    if cap == 0:
        return
    if fuel.get("current", 0) / cap >= threshold:
        return
    try:
        data = c.refuel(ship)
        t = data.get("transaction", {})
        print(
            f"  refueled {ship} +{t.get('units',0)} fuel for "
            f"{t.get('totalPrice',0)}c (credits {data.get('agent', {}).get('credits')})"
        )
        try:
            c.set_flight_mode(ship, "CRUISE")
        except ApiError:
            pass
    except ApiError as e:
        print(f"  refuel skipped: {e.message}")


# -- trading ----------------------------------------------------------------
def cmd_deals(args, c: Client) -> None:
    routes = store.best_routes(
        system=args.system, min_profit=args.min_profit, max_age_s=args.max_age
    )
    if not routes:
        print(
            "No profitable routes recorded yet.\n"
            "Visit markets to gather live prices first, e.g. `st.py market <WAYPOINT>`."
        )
        return
    print(
        f"{'GOOD':<20} {'BUY @':<13} {'SELL @':<13} "
        f"{'buy':>6} {'sell':>6} {'profit':>7} {'vol':>4}"
    )
    for r in routes[: args.limit]:
        print(
            f"{r['good']:<20} {r['buy_wp']:<13} {r['sell_wp']:<13} "
            f"{r['buy']:>6} {r['sell']:>6} {r['profit']:>7} {r['volume']:>4}"
        )


def cmd_trade(args, c: Client) -> None:
    from tui.bots import TraderBot

    bot = TraderBot(
        c,
        args.ship,
        min_profit=args.min_profit,
        budget=args.budget,
        loops=args.loops,
        on_log=lambda m: print(m),
    )
    print(f"Trader engaged on {args.ship}. Ctrl+C to stop.")
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()
        print("\nTrader disengaged.")


# -- fleet growth & refit ---------------------------------------------------
def cmd_expand(args, c: Client) -> None:
    from fleet import FleetManager

    fm = FleetManager(
        c,
        args.ship_type,
        system=args.system,
        waypoint=args.waypoint,
        credit_buffer=args.credit_buffer,
        max_ships=args.max_ships,
        max_price=args.max_price,
        loops=args.loops,
        on_log=lambda m: print(m),
    )
    try:
        fm.run()
    except KeyboardInterrupt:
        fm.stop()
        print("\nFleet manager stopped.")


def cmd_mounts(args, c: Client) -> None:
    for m in c.ship_mounts(args.ship):
        print(f"  {m.get('symbol','?'):<28} {m.get('name','')}")


def cmd_install_mount(args, c: Client) -> None:
    c.install_mount(args.ship, args.mount)
    print(f"Installed {args.mount} on {args.ship}.")


def cmd_remove_mount(args, c: Client) -> None:
    c.remove_mount(args.ship, args.mount)
    print(f"Removed {args.mount} from {args.ship}.")


def cmd_transfer(args, c: Client) -> None:
    c.transfer_cargo(args.ship, args.trade, args.units, args.dest)
    print(f"Transferred {args.units} {args.trade} from {args.ship} → {args.dest}.")


def cmd_refine(args, c: Client) -> None:
    data = c.refine(args.ship, args.produce.upper())
    produced = data.get("produced", [])
    consumed = data.get("consumed", [])
    print(
        f"Refined on {args.ship}: "
        + ", ".join(f"+{p['units']} {p['tradeSymbol']}" for p in produced)
        + (" (used " + ", ".join(f"{x['units']} {x['tradeSymbol']}" for x in consumed) + ")" if consumed else "")
    )


def cmd_siphon(args, c: Client) -> None:
    c.orbit(args.ship)
    data = c.siphon(args.ship)
    y = data.get("siphon", {}).get("yield", {})
    cd = data.get("cooldown", {})
    print(f"Siphoned +{y.get('units',0)} {y.get('symbol','')} (cooldown {cd.get('totalSeconds',0)}s)")


def cmd_repair(args, c: Client) -> None:
    if args.quote:
        tx = c.repair_cost(args.ship).get("transaction", {})
        print(f"{args.ship} repair would cost {_credits(tx.get('totalPrice', 0))}.")
        return
    data = c.repair_ship(args.ship)
    tx = data.get("transaction", {})
    print(f"Repaired {args.ship} for {_credits(tx.get('totalPrice', 0))}.")


def cmd_scrap(args, c: Client) -> None:
    if args.quote:
        tx = c.scrap_value(args.ship).get("transaction", {})
        print(f"{args.ship} scrap would yield {_credits(tx.get('totalPrice', 0))}.")
        return
    data = c.scrap_ship(args.ship)
    tx = data.get("transaction", {})
    print(f"Scrapped {args.ship} for {_credits(tx.get('totalPrice', 0))}.")


# -- entry ------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="st", description="SpaceTraders helper CLI")
    p.add_argument("--token", help="override ST_AGENT_TOKEN")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("register", help="register a new agent (uses account token)")
    sp.add_argument("symbol")
    sp.add_argument("faction")
    sp.set_defaults(func=cmd_register, raw=True)

    sp = sub.add_parser("agent", help="show your agent")
    sp.set_defaults(func=cmd_agent)

    sp = sub.add_parser("ships", help="list your ships")
    sp.add_argument("-v", action="store_true")
    sp.set_defaults(func=cmd_ships)

    sp = sub.add_parser("ship", help="show one ship")
    sp.add_argument("symbol")
    sp.set_defaults(func=cmd_ship)

    sp = sub.add_parser("contracts", help="list contracts")
    sp.set_defaults(func=cmd_contracts)

    sp = sub.add_parser("accept", help="accept a contract")
    sp.add_argument("contract_id")
    sp.set_defaults(func=cmd_accept)

    sp = sub.add_parser("negotiate", help="negotiate a new contract from a ship's location")
    sp.add_argument("ship")
    sp.set_defaults(func=cmd_negotiate)

    sp = sub.add_parser("fulfill", help="fulfill a contract")
    sp.add_argument("contract_id")
    sp.set_defaults(func=cmd_fulfill)

    sp = sub.add_parser("waypoints", help="list waypoints in a system")
    sp.add_argument("system", nargs="?", help="default: your HQ system")
    sp.add_argument("--type")
    sp.add_argument("--traits", nargs="+")
    sp.add_argument("--system", dest="system_flag")
    sp.set_defaults(func=cmd_waypoints)

    sp = sub.add_parser("waypoint", help="show one waypoint")
    sp.add_argument("symbol")
    sp.add_argument("--system")
    sp.set_defaults(func=cmd_waypoint)

    sp = sub.add_parser("market", help="show market at a waypoint")
    sp.add_argument("waypoint")
    sp.add_argument("--system")
    sp.set_defaults(func=cmd_market)

    sp = sub.add_parser("shipyard", help="show shipyard at a waypoint")
    sp.add_argument("waypoint")
    sp.add_argument("--system")
    sp.set_defaults(func=cmd_shipyard)

    sp = sub.add_parser("buyship", help="buy a ship (auto-locates a shipyard selling the type)")
    sp.add_argument("ship_type", help="e.g. SHIP_MINING_DRONE, SHIP_LIGHT_HAULER")
    sp.add_argument("--waypoint", help="specific shipyard waypoint (else scan the system)")
    sp.add_argument("--system", help="system to search (default: your HQ system)")
    sp.add_argument("--max-price", type=int, dest="max_price", help="skip if price exceeds this")
    sp.set_defaults(func=cmd_buyship)

    sp = sub.add_parser("orbit", help="orbit a ship")
    sp.add_argument("ship")
    sp.set_defaults(func=cmd_orbit)

    sp = sub.add_parser("dock", help="dock a ship")
    sp.add_argument("ship")
    sp.set_defaults(func=cmd_dock)

    sp = sub.add_parser("refuel", help="refuel a ship")
    sp.add_argument("ship")
    sp.set_defaults(func=cmd_refuel)

    sp = sub.add_parser("navigate", help="navigate a ship to a waypoint")
    sp.add_argument("ship")
    sp.add_argument("waypoint")
    sp.set_defaults(func=cmd_navigate)

    sp = sub.add_parser("extract", help="extract (mine) with a ship")
    sp.add_argument("ship")
    sp.set_defaults(func=cmd_extract)

    sp = sub.add_parser("survey", help="survey deposits at the ship's waypoint")
    sp.add_argument("ship")
    sp.set_defaults(func=cmd_survey)

    sp = sub.add_parser("sell", help="sell cargo; units defaults to all")
    sp.add_argument("ship")
    sp.add_argument("trade")
    sp.add_argument("units", type=int, nargs="?")
    sp.set_defaults(func=cmd_sell)

    sp = sub.add_parser("buy", help="buy cargo into a ship")
    sp.add_argument("ship")
    sp.add_argument("trade")
    sp.add_argument("units", type=int)
    sp.set_defaults(func=cmd_purchase)

    sp = sub.add_parser("jettison", help="jettison cargo")
    sp.add_argument("ship")
    sp.add_argument("trade")
    sp.add_argument("units", type=int)
    sp.set_defaults(func=cmd_jettison)

    sp = sub.add_parser("cooldown", help="show a ship's cooldown")
    sp.add_argument("ship")
    sp.set_defaults(func=cmd_cooldown)

    sp = sub.add_parser("autopilot", help="auto-mine (and optionally sell/contract)")
    sp.add_argument("ship")
    sp.add_argument("--contract", help="contract id to fulfill")
    sp.add_argument("--sell", action="store_true", help="sell non-contract cargo at market")
    sp.add_argument("--loops", type=int, help="stop after N extract cycles")
    sp.set_defaults(func=cmd_autopilot)

    sp = sub.add_parser("deals", help="show best known arbitrage routes from stored prices")
    sp.add_argument("--system", help="limit to one system (default: all)")
    sp.add_argument("--min-profit", type=int, default=1, dest="min_profit")
    sp.add_argument("--max-age", type=float, default=3600.0, dest="max_age",
                    help="ignore prices older than N seconds (default 3600)")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_deals)

    sp = sub.add_parser("trade", help="autonomous arbitrage trader for one ship")
    sp.add_argument("ship")
    sp.add_argument("--min-profit", type=int, default=50, dest="min_profit",
                    help="minimum profit per unit to take a route (default 50)")
    sp.add_argument("--budget", type=int, help="max credits to spend per buy leg")
    sp.add_argument("--loops", type=int, help="stop after N trade cycles")
    sp.set_defaults(func=cmd_trade)

    sp = sub.add_parser("expand", help="autonomously buy ships while credits allow")
    sp.add_argument("ship_type", help="e.g. SHIP_MINING_DRONE, SHIP_LIGHT_HAULER")
    sp.add_argument("--credit-buffer", type=int, default=50000, dest="credit_buffer",
                    help="keep at least this many credits in reserve (default 50000)")
    sp.add_argument("--max-ships", type=int, dest="max_ships", help="stop at this fleet size")
    sp.add_argument("--max-price", type=int, dest="max_price", help="skip if unit price exceeds this")
    sp.add_argument("--waypoint", help="specific shipyard waypoint (else scan the system)")
    sp.add_argument("--system", help="system to search (default: your HQ system)")
    sp.add_argument("--loops", type=int, help="cap the number of purchase attempts")
    sp.set_defaults(func=cmd_expand)

    sp = sub.add_parser("mounts", help="list a ship's installed mounts")
    sp.add_argument("ship")
    sp.set_defaults(func=cmd_mounts)

    sp = sub.add_parser("install-mount", help="install a mount (ship must be docked at a shipyard)")
    sp.add_argument("ship")
    sp.add_argument("mount", help="e.g. MOUNT_MINING_LASER_II, MOUNT_SURVEYOR_I")
    sp.set_defaults(func=cmd_install_mount)

    sp = sub.add_parser("remove-mount", help="remove a mount")
    sp.add_argument("ship")
    sp.add_argument("mount")
    sp.set_defaults(func=cmd_remove_mount)

    sp = sub.add_parser("transfer", help="transfer cargo to another ship at the same waypoint")
    sp.add_argument("ship")
    sp.add_argument("trade")
    sp.add_argument("units", type=int)
    sp.add_argument("dest", help="destination ship symbol")
    sp.set_defaults(func=cmd_transfer)

    sp = sub.add_parser("refine", help="refine raw materials in a ship's refinery")
    sp.add_argument("ship")
    sp.add_argument("produce", help="output good, e.g. IRON, COPPER, FUEL")
    sp.set_defaults(func=cmd_refine)

    sp = sub.add_parser("siphon", help="siphon gas at a gas giant")
    sp.add_argument("ship")
    sp.set_defaults(func=cmd_siphon)

    sp = sub.add_parser("repair", help="repair a ship (docked at a shipyard)")
    sp.add_argument("ship")
    sp.add_argument("--quote", action="store_true", help="show price without repairing")
    sp.set_defaults(func=cmd_repair)

    sp = sub.add_parser("scrap", help="scrap a ship for credits (docked at a shipyard)")
    sp.add_argument("ship")
    sp.add_argument("--quote", action="store_true", help="show value without scrapping")
    sp.set_defaults(func=cmd_scrap)

    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "raw", False):
        args.func(args)
        return 0

    token = args.token or config.require_agent_token()
    c = Client(token=token)
    try:
        args.func(args, c)
    except ApiError as e:
        print(f"API ERROR {e.code}: {e.message}", file=sys.stderr)
        if e.data:
            print(f"  {e.data}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
