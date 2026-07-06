from __future__ import annotations

import argparse
import datetime as dt
import sys
from typing import Any

import config
from api import ApiError, Client
from navigation import system_of as _system_of


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
    show_market(c.market(system, args.waypoint))


def cmd_shipyard(args, c: Client) -> None:
    system = args.system or _system_of(args.waypoint)
    data = c.shipyard(system, args.waypoint)
    print(f"Shipyard @ {data.get('symbol')}")
    for t in data.get("ships", []):
        st = t.get("purchasePrice", "?")
        print(f"  {t.get('type','?'):<28} {st}")


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


# -- fleet & world commands ---------------------------------------------------
def cmd_deliver(args, c: Client) -> None:
    data = c.deliver_contract(args.contract_id, args.ship, args.trade, args.units)
    print(f"Delivered {args.units} {args.trade}.")
    show_contract(data.get("contract", data))


def cmd_buy_ship(args, c: Client) -> None:
    data = c.purchase_ship(args.type.upper(), args.waypoint)
    ship = data.get("ship", {})
    t = data.get("transaction", {})
    print(f"Purchased {ship.get('symbol', '?')} for {_credits(t.get('price', 0))}")


def cmd_transfer(args, c: Client) -> None:
    data = c.transfer(args.from_ship, args.to_ship, args.trade, args.units)
    cargo = data.get("cargo", {})
    print(f"Transferred. {args.from_ship} cargo {cargo.get('units')}/{cargo.get('capacity')}")


def cmd_chart(args, c: Client) -> None:
    data = c.chart(args.ship)
    wp = data.get("waypoint", {})
    print(f"Charted {wp.get('symbol', '?')} ({wp.get('type', '?')})")


def cmd_siphon(args, c: Client) -> None:
    data = c.siphon(args.ship)
    y = data.get("siphon", {}).get("yield", {})
    print(f"Siphoned +{y.get('units', 0)} {y.get('symbol', '')}")


def cmd_refine(args, c: Client) -> None:
    data = c.refine(args.ship, args.produce.upper())
    for p in data.get("produced", []):
        print(f"Refined +{p.get('units')} {p.get('tradeSymbol')}")


def cmd_repair(args, c: Client) -> None:
    if args.quote:
        t = c.repair_quote(args.ship).get("transaction", {})
        print(f"Repair would cost {_credits(t.get('totalPrice', 0))}")
        return
    data = c.repair(args.ship)
    t = data.get("transaction", {})
    print(f"Repaired {args.ship} for {_credits(t.get('totalPrice', 0))}")


def cmd_prices(args, c: Client) -> None:
    from market import MarketDB

    system = args.system or _system_of(config.HQ)
    rows = MarketDB().prices(system)
    if not rows:
        print(f"No recorded prices for {system}. Run a probe/trader bot or visit markets.")
        return
    rows.sort(key=lambda r: (r.good, r.waypoint))
    print(f"{'GOOD':<24} {'WAYPOINT':<14} {'TYPE':<8} {'BUY':>6} {'SELL':>6}  AGE")
    now = dt.datetime.now().timestamp()
    for r in rows:
        age = int((now - r.ts) / 60)
        print(
            f"{r.good:<24} {r.waypoint:<14} {r.type:<8} "
            f"{r.buy or '-':>6} {r.sell or '-':>6}  {age}m"
        )


def cmd_routes(args, c: Client) -> None:
    from market import MarketDB, best_routes
    from navigation import WaypointCache

    system = args.system or _system_of(config.HQ)
    routes = best_routes(
        MarketDB(), WaypointCache(c), system, min_margin=args.min_margin
    )
    if not routes:
        print(f"No profitable routes known in {system}. Gather prices first (probe bot).")
        return
    print(f"{'GOOD':<24} {'BUY @':<14} {'':>6} {'SELL @':<14} {'':>6} {'+/u':>5} {'dist':>6}")
    for r in routes[: args.n]:
        print(
            f"{r.good:<24} {r.buy_waypoint:<14} {r.buy_price:>6} "
            f"{r.sell_waypoint:<14} {r.sell_price:>6} {r.margin:>5} {r.dist:>6.0f}"
        )


# -- bots ---------------------------------------------------------------------
def _run_bot(bot) -> None:
    import threading

    t = threading.Thread(target=bot.run, daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.5)
    except KeyboardInterrupt:
        print("\nstopping bot…")
        bot.stop()
        t.join(30)


def cmd_autopilot(args, c: Client) -> None:
    """Kept for muscle memory: `autopilot` == `bot mine`."""
    from automation import MinerBot

    bot = MinerBot(
        c,
        args.ship,
        contract=args.contract,
        sell=args.sell,
        max_cycles=args.loops,
        on_log=print,
    )
    print(f"Autopilot (miner) engaged on {args.ship}. Ctrl+C to stop.")
    _run_bot(bot)


def cmd_bot(args, c: Client) -> None:
    from automation import BOT_TYPES

    cls = BOT_TYPES[args.kind]
    kw: dict[str, Any] = {"on_log": print, "max_cycles": args.loops}
    if args.kind == "mine":
        kw["contract"] = args.contract
    if args.kind == "trade":
        kw["min_margin"] = args.min_margin
    bot = cls(c, args.ship, **kw)
    print(f"{cls.name} bot engaged on {args.ship}. Ctrl+C to stop.")
    _run_bot(bot)


def cmd_fleet(args, c: Client) -> None:
    from automation import FleetCommander

    cmdr = FleetCommander(
        c,
        autobuy=args.autobuy.upper() if args.autobuy else None,
        autobuy_reserve=args.reserve,
        max_ships=args.max_ships,
        on_log=print,
    )
    print("Fleet commander engaged (role-based bots per ship). Ctrl+C to stop.")
    _run_bot(cmdr)


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

    sp = sub.add_parser("deliver", help="deliver contract goods from a docked ship")
    sp.add_argument("contract_id")
    sp.add_argument("ship")
    sp.add_argument("trade")
    sp.add_argument("units", type=int)
    sp.set_defaults(func=cmd_deliver)

    sp = sub.add_parser("buy-ship", help="purchase a ship at a shipyard waypoint")
    sp.add_argument("type", help="e.g. SHIP_MINING_DRONE")
    sp.add_argument("waypoint")
    sp.set_defaults(func=cmd_buy_ship)

    sp = sub.add_parser("transfer", help="transfer cargo between two ships")
    sp.add_argument("from_ship")
    sp.add_argument("to_ship")
    sp.add_argument("trade")
    sp.add_argument("units", type=int)
    sp.set_defaults(func=cmd_transfer)

    sp = sub.add_parser("chart", help="chart the current waypoint")
    sp.add_argument("ship")
    sp.set_defaults(func=cmd_chart)

    sp = sub.add_parser("siphon", help="siphon gas at a gas giant")
    sp.add_argument("ship")
    sp.set_defaults(func=cmd_siphon)

    sp = sub.add_parser("refine", help="refine raw goods aboard a refinery ship")
    sp.add_argument("ship")
    sp.add_argument("produce", help="e.g. IRON, COPPER, FUEL")
    sp.set_defaults(func=cmd_refine)

    sp = sub.add_parser("repair", help="repair a ship at a shipyard")
    sp.add_argument("ship")
    sp.add_argument("--quote", action="store_true", help="show cost only")
    sp.set_defaults(func=cmd_repair)

    sp = sub.add_parser("prices", help="show recorded market prices for a system")
    sp.add_argument("system", nargs="?")
    sp.set_defaults(func=cmd_prices)

    sp = sub.add_parser("routes", help="best known trade routes in a system")
    sp.add_argument("system", nargs="?")
    sp.add_argument("--min-margin", type=int, default=1)
    sp.add_argument("-n", type=int, default=15, help="show top N")
    sp.set_defaults(func=cmd_routes)

    sp = sub.add_parser("bot", help="run an autonomous bot on one ship")
    sp.add_argument("kind", choices=["mine", "trade", "contract", "probe"])
    sp.add_argument("ship")
    sp.add_argument("--contract", help="(mine) contract id to work")
    sp.add_argument("--min-margin", type=int, default=2, help="(trade) min profit/unit")
    sp.add_argument("--loops", type=int, help="stop after N cycles")
    sp.set_defaults(func=cmd_bot)

    sp = sub.add_parser("fleet", help="run role-based bots on every ship")
    sp.add_argument("--autobuy", help="ship type to buy with spare credits, e.g. SHIP_MINING_DRONE")
    sp.add_argument("--reserve", type=int, default=50_000, help="credits to keep before autobuy")
    sp.add_argument("--max-ships", type=int, default=10)
    sp.set_defaults(func=cmd_fleet)

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
