"""Flask app factory for the spacetui web dashboard."""

from __future__ import annotations

import json
import queue
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, request, send_from_directory

import config
import onboarding
import store
from api import ApiError, Client
from .hub import Hub

_STATIC = Path(__file__).resolve().parent / "static"


def create_app(
    client: Client | None = None, *, start_poller: bool = True,
    token: str | None = None, client_factory=Client,
) -> Flask:
    app = Flask(__name__, static_folder=None)
    _state = {"hub": None}

    def ensure_hub(c) -> Hub:
        h = Hub(c)
        _state["hub"] = h
        app.hub = h
        if start_poller:
            h.refresh()
            h.start_poller()
        return h

    def hub():
        return _state["hub"]

    # Start with a client if we have one; otherwise run in setup mode until the
    # browser posts credentials to /api/setup.
    if client is not None:
        ensure_hub(client)
    elif config.AGENT_TOKEN:
        ensure_hub(client_factory(token=config.AGENT_TOKEN))

    # -- optional token auth (for LAN / phone access) ----------------------
    def _authorized() -> bool:
        return token in (
            request.headers.get("X-Auth-Token"),
            request.cookies.get("st_token"),
            request.args.get("token"),
        )

    @app.before_request
    def _guard():
        if not token:
            return None
        # a valid ?token on the page load sets a cookie, then drops the query so
        # the token doesn't linger in the URL / history
        if (request.path == "/" and request.args.get("token") == token
                and request.cookies.get("st_token") != token):
            resp = redirect("/")
            resp.set_cookie("st_token", token, httponly=True, samesite="Lax")
            return resp
        if _authorized():
            return None
        if request.path == "/":
            return ("Unauthorized. Open this page with ?token=YOUR_TOKEN appended.", 401)
        return jsonify({"error": "unauthorized"}), 401

    # -- static SPA --------------------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(_STATIC, "index.html")

    @app.get("/<path:name>")
    def static_files(name):
        return send_from_directory(_STATIC, name)

    # -- first-run setup ---------------------------------------------------
    @app.post("/api/setup")
    def api_setup():
        if hub() is not None:
            return jsonify({"ok": True, "configured": True})
        body = request.get_json(silent=True) or {}
        try:
            if body.get("mode") == "register":
                agent = onboarding.register_agent(
                    body.get("account_token", "").strip(),
                    body.get("callsign", "").strip(),
                    body.get("faction", "COSMIC").strip(),
                    client_factory=client_factory,
                )
            else:
                agent = onboarding.save_agent_token(
                    body.get("token", "").strip(), client_factory=client_factory)
        except ApiError as e:
            return jsonify({"ok": False, "error": e.message}), 400
        except Exception as e:  # noqa
            return jsonify({"ok": False, "error": str(e)}), 400
        ensure_hub(client_factory(token=config.AGENT_TOKEN))
        return jsonify({"ok": True, "agent": agent})

    # -- read API ----------------------------------------------------------
    @app.get("/api/state")
    def api_state():
        if hub() is None:
            return jsonify({"configured": False})
        return jsonify({"configured": True, **hub().snapshot()})

    @app.get("/api/log")
    def api_log():
        if hub() is None:
            return jsonify([])
        return jsonify(hub().log_lines(int(request.args.get("limit", 100))))

    @app.get("/api/stream")
    def api_stream():
        h = hub()
        if h is None:
            return jsonify({"error": "not configured"}), 409

        def _sse(event, data):
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        def gen():
            q = h.subscribe()
            try:
                yield _sse("state", h.snapshot())  # prime the client
                while True:
                    try:
                        item = q.get(timeout=15)
                        yield _sse(item["event"], item["data"])
                    except queue.Empty:
                        yield ": ping\n\n"  # heartbeat keeps the connection open
            finally:
                h.unsubscribe(q)

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/deals")
    def api_deals():
        routes = store.best_routes(
            system=request.args.get("system") or None,
            min_profit=int(request.args.get("min_profit", 1)),
            max_hops=int(request.args.get("max_hops", 0)),
        )
        return jsonify(routes[: int(request.args.get("limit", 25))])

    @app.get("/api/stats")
    def api_stats():
        watch = []
        for sym in store.tracked_goods(limit=12):
            series = store.price_series(sym, limit=60)
            sells = [r["sell_price"] for r in series if r["sell_price"] is not None]
            if sells:
                watch.append({"good": sym, "last": sells[-1],
                              "delta": sells[-1] - sells[0], "spark": sells})
        return jsonify({
            "credits": [{"t": r["observed_at"], "v": r["credits"]}
                        for r in store.credit_series(limit=400)],
            "pnl": store.pnl_summary(),
            "pnl_by_good": store.pnl_by_good(limit=8),
            "activity": store.activity_breakdown(),
            "watchlist": watch,
            "routes": store.best_routes(min_profit=1, max_hops=0)[:10],
        })

    @app.get("/api/price/<good>")
    def api_price(good):
        return jsonify(store.price_series(good, limit=300))

    @app.get("/api/shiptypes")
    def api_shiptypes():
        if hub() is None:
            return jsonify([])
        return jsonify(hub().ship_types())

    @app.get("/api/system/<system>")
    def api_system(system):
        if hub() is None:
            return jsonify({"error": "not configured"}), 409
        try:
            wps = hub().system_waypoints(system)
        except Exception as e:  # noqa
            return jsonify({"error": str(e)}), 502
        # jump-gate links originating in this system (for drawing)
        links = [{"from": e["from_gate"], "to": e["to_gate"], "to_system": e["to_system"]}
                 for e in store.jump_edges() if e["from_system"] == system]
        return jsonify({"system": system, "waypoints": wps, "links": links})

    @app.get("/api/market/<waypoint>")
    def api_market(waypoint):
        if hub() is None:
            return jsonify({"error": "not configured"}), 409
        system = "-".join(waypoint.split("-")[:2])
        try:
            m = hub().world.get_market(system, waypoint)
        except Exception as e:  # noqa
            return jsonify({"error": str(e)}), 502
        if m is None:
            return jsonify({"error": "no market at waypoint"}), 404
        return jsonify(m)

    # -- control API -------------------------------------------------------
    def _need_hub():
        return jsonify({"ok": False, "error": "not configured"}), 409

    @app.post("/api/orchestrator")
    def api_orch():
        if hub() is None:
            return _need_hub()
        body = request.get_json(silent=True) or {}
        if body.get("action") == "start":
            hub().start_orch(body)
        else:
            hub().stop_orch()
        return jsonify(hub().snapshot()["orchestrator"])

    @app.post("/api/bot")
    def api_bot():
        if hub() is None:
            return _need_hub()
        body = request.get_json(silent=True) or {}
        ship, kind = body.get("ship"), body.get("kind")
        if not ship:
            return jsonify({"ok": False, "error": "ship required"}), 400
        if kind == "stop":
            hub().stop_bot(ship)
        else:
            hub().start_bot(ship, kind or "trade")
        return jsonify({"ok": True})

    @app.post("/api/fleet")
    def api_fleet():
        if hub() is None:
            return _need_hub()
        body = request.get_json(silent=True) or {}
        ship = body.get("ship")
        if not ship:
            return jsonify({"ok": False, "error": "ship required"}), 400
        result = hub().fleet_action(ship, body.get("action", ""), body.get("waypoint", ""))
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.post("/api/contract")
    def api_contract():
        if hub() is None:
            return _need_hub()
        body = request.get_json(silent=True) or {}
        result = hub().accept_contract(body.get("id", ""))
        return jsonify(result), (200 if result.get("ok") else 400)

    return app
