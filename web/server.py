"""Flask app factory for the spacetui web dashboard."""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import config
import store
from api import Client
from .hub import Hub

_STATIC = Path(__file__).resolve().parent / "static"


def create_app(client: Client | None = None, *, start_poller: bool = True) -> Flask:
    app = Flask(__name__, static_folder=None)
    hub = Hub(client or Client(token=config.require_agent_token()))
    app.hub = hub  # exposed for tests
    if start_poller:
        hub.refresh()
        hub.start_poller()

    # -- static SPA --------------------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(_STATIC, "index.html")

    @app.get("/<path:name>")
    def static_files(name):
        return send_from_directory(_STATIC, name)

    # -- read API ----------------------------------------------------------
    @app.get("/api/state")
    def api_state():
        return jsonify(hub.snapshot())

    @app.get("/api/log")
    def api_log():
        return jsonify(hub.log_lines(int(request.args.get("limit", 100))))

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
            "credits": [r["credits"] for r in store.credit_series(limit=400)],
            "pnl": store.pnl_summary(),
            "pnl_by_good": store.pnl_by_good(limit=8),
            "activity": store.activity_breakdown(),
            "watchlist": watch,
            "routes": store.best_routes(min_profit=1, max_hops=0)[:10],
        })

    @app.get("/api/market/<waypoint>")
    def api_market(waypoint):
        system = "-".join(waypoint.split("-")[:2])
        try:
            m = hub.c.market(system, waypoint)
        except Exception as e:  # noqa
            return jsonify({"error": str(e)}), 502
        store.record_market(m)
        return jsonify(m)

    # -- control API -------------------------------------------------------
    @app.post("/api/orchestrator")
    def api_orch():
        body = request.get_json(silent=True) or {}
        if body.get("action") == "start":
            hub.start_orch(body)
        else:
            hub.stop_orch()
        return jsonify(hub.snapshot()["orchestrator"])

    @app.post("/api/bot")
    def api_bot():
        body = request.get_json(silent=True) or {}
        ship, kind = body.get("ship"), body.get("kind")
        if not ship:
            return jsonify({"ok": False, "error": "ship required"}), 400
        if kind == "stop":
            hub.stop_bot(ship)
        else:
            hub.start_bot(ship, kind or "trade")
        return jsonify({"ok": True})

    @app.post("/api/fleet")
    def api_fleet():
        body = request.get_json(silent=True) or {}
        ship = body.get("ship")
        if not ship:
            return jsonify({"ok": False, "error": "ship required"}), 400
        result = hub.fleet_action(ship, body.get("action", ""), body.get("waypoint", ""))
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.post("/api/contract")
    def api_contract():
        body = request.get_json(silent=True) or {}
        result = hub.accept_contract(body.get("id", ""))
        return jsonify(result), (200 if result.get("ok") else 400)

    return app
