"""Construction supply-chain planning (pure, unit-tested).

The canonical endgame is building your system's jump gate: a construction site
lists the materials it still needs, and you ferry them in with the ``construct``
endpoint. These helpers turn a construction payload into a work list; the actual
hauling is done by ``tui.bots.ConstructionBot`` (which reuses the shared nav
machinery) and supervised by the orchestrator's ``construct`` goal.
"""

from __future__ import annotations


def materials_gap(construction: dict) -> dict[str, int]:
    """``{tradeSymbol: units_still_needed}`` for every unfinished material."""
    out: dict[str, int] = {}
    for m in construction.get("materials") or []:
        need = (m.get("required", 0) or 0) - (m.get("fulfilled", 0) or 0)
        sym = m.get("tradeSymbol")
        if sym and need > 0:
            out[sym] = need
    return out


def is_complete(construction: dict) -> bool:
    """True when the site reports complete, or nothing remains to deliver."""
    if construction.get("isComplete"):
        return True
    return not materials_gap(construction)


def cheapest_source(good: str, observations) -> tuple[str | None, int | None]:
    """From market observations, the ``(waypoint, purchase_price)`` with the
    lowest price to buy ``good``, or ``(None, None)`` if nobody sells it."""
    best: tuple[str, int] | None = None
    for o in observations:
        if o.get("symbol") != good:
            continue
        p = o.get("purchase_price")
        if p is None:
            continue
        if best is None or p < best[1]:
            best = (o.get("waypoint"), p)
    return best if best else (None, None)


def next_material(construction: dict, observations) -> str | None:
    """Pick the next material to source: prefer one we already know a market for,
    else just the first outstanding one (a scout can go find a market)."""
    gap = materials_gap(construction)
    if not gap:
        return None
    for good in gap:
        if cheapest_source(good, observations)[0] is not None:
            return good
    return next(iter(gap))
