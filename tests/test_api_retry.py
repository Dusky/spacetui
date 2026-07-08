import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api
from api import ApiError, Client


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.reason = "error"

    def json(self):
        return self._body


class FakeSession:
    headers: dict = {}

    def __init__(self, sequence):
        self.sequence = sequence
        self.calls = 0

    def request(self, *a, **k):
        r = self.sequence[min(self.calls, len(self.sequence) - 1)]
        self.calls += 1
        return r


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda *_: None)


def test_retries_429_then_succeeds():
    c = Client(token="x")
    c.session = FakeSession([
        FakeResp(429, headers={"Retry-After": "0"}),
        FakeResp(429, headers={"Retry-After": "0"}),
        FakeResp(200, {"data": {"ok": True}}),
    ])
    assert c.get("/x") == {"data": {"ok": True}}
    assert c.session.calls == 3  # retried past both 429s


def test_retries_5xx_then_succeeds():
    c = Client(token="x")
    c.session = FakeSession([FakeResp(502, {}), FakeResp(200, {"data": 1})])
    assert c.get("/x") == {"data": 1}
    assert c.session.calls == 2


def test_business_4xx_not_retried():
    c = Client(token="x")
    c.session = FakeSession([FakeResp(400, {"error": {"code": 4001, "message": "nope"}})])
    with pytest.raises(ApiError) as ei:
        c.get("/x")
    assert ei.value.code == 4001
    assert c.session.calls == 1  # a real business error surfaces immediately


def test_network_error_retried_then_raised():
    import requests

    class Boom:
        headers: dict = {}

        def __init__(self):
            self.calls = 0

        def request(self, *a, **k):
            self.calls += 1
            raise requests.RequestException("conn reset")

    c = Client(token="x")
    c.session = Boom()
    with pytest.raises(ApiError):
        c.get("/x")
    assert c.session.calls == 10  # exhausted the retry budget before giving up
