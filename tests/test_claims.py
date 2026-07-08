import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import claims


def route(good, a, b):
    return {"good": good, "buy_wp": a, "sell_wp": b}


def test_pick_unclaimed_spreads_traders():
    claims.clear()
    routes = [route("IRON", "A", "B"), route("GOLD", "C", "D")]
    r1 = claims.pick_unclaimed(routes, "ship1")
    assert r1["good"] == "IRON"
    claims.claim(r1, "ship1")
    # a second trader skips the claimed top route and takes the next
    r2 = claims.pick_unclaimed(routes, "ship2")
    assert r2["good"] == "GOLD"
    # the owner still sees its own claim as available
    assert claims.pick_unclaimed(routes, "ship1")["good"] == "IRON"


def test_release_frees_route():
    claims.clear()
    r = route("IRON", "A", "B")
    claims.claim(r, "s1")
    assert claims.claimed_by_other(r, "s2")
    claims.release(r, "s1")
    assert not claims.claimed_by_other(r, "s2")


def test_release_only_by_owner():
    claims.clear()
    r = route("IRON", "A", "B")
    claims.claim(r, "s1")
    claims.release(r, "s2")  # not the owner -> no-op
    assert claims.claimed_by_other(r, "s2")


def test_claim_expires_after_ttl():
    claims.clear()
    r = route("IRON", "A", "B")
    claims.claim(r, "s1", ttl=10, now=1000)
    assert claims.claimed_by_other(r, "s2", now=1005)       # still held
    assert not claims.claimed_by_other(r, "s2", now=1011)   # expired -> free


def test_all_claimed_returns_none():
    claims.clear()
    routes = [route("IRON", "A", "B")]
    claims.claim(routes[0], "s1")
    assert claims.pick_unclaimed(routes, "s2") is None
