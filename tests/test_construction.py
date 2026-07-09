import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from construction import cheapest_source, is_complete, materials_gap, next_material


def site(materials, complete=False):
    return {"symbol": "X1-HQ-GATE", "isComplete": complete,
            "materials": [{"tradeSymbol": s, "required": req, "fulfilled": ful}
                          for s, req, ful in materials]}


def obs(waypoint, symbol, buy):
    return {"waypoint": waypoint, "symbol": symbol, "purchase_price": buy}


def test_materials_gap_only_outstanding():
    con = site([("FAB_MATS", 4000, 1000), ("ADV_CIRCUITRY", 1200, 1200)])
    assert materials_gap(con) == {"FAB_MATS": 3000}


def test_is_complete_flag_and_empty_gap():
    assert is_complete(site([("FAB_MATS", 100, 100)])) is True
    assert is_complete(site([("FAB_MATS", 100, 0)], complete=True)) is True
    assert is_complete(site([("FAB_MATS", 100, 0)])) is False


def test_cheapest_source_picks_lowest_price():
    observations = [
        obs("X1-HQ-A", "FAB_MATS", 500),
        obs("X1-HQ-B", "FAB_MATS", 420),
        obs("X1-HQ-C", "OTHER", 10),
    ]
    assert cheapest_source("FAB_MATS", observations) == ("X1-HQ-B", 420)
    assert cheapest_source("NOPE", observations) == (None, None)


def test_next_material_prefers_known_market():
    con = site([("FAB_MATS", 100, 0), ("ADV_CIRCUITRY", 100, 0)])
    # only ADV_CIRCUITRY has a known market, so source it first
    observations = [obs("X1-HQ-B", "ADV_CIRCUITRY", 300)]
    assert next_material(con, observations) == "ADV_CIRCUITRY"


def test_next_material_none_when_done():
    assert next_material(site([("FAB_MATS", 100, 100)]), []) is None
