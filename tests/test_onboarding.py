import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import onboarding
from api import ApiError


class FakeClient:
    AGENT = {"symbol": "TESTER", "headquarters": "X1-Z9-A1",
             "credits": 175000, "shipCount": 2}

    def __init__(self, token=None):
        self.token = token

    def my_agent(self):
        if self.token in ("good-agent-token", "registered-token"):
            return self.AGENT
        raise ApiError(401, "invalid token")

    @classmethod
    def register(cls, symbol, faction, account_token=None):
        assert account_token, "register requires an account token"
        return {"token": "registered-token", "agent": cls.AGENT}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolate .env path and config globals so tests don't touch real state."""
    monkeypatch.setattr(onboarding, "ENV_PATH", tmp_path / ".env")
    for attr, val in [("AGENT_TOKEN", ""), ("AGENT_SYMBOL", ""),
                      ("HQ", ""), ("ACCOUNT_TOKEN", "")]:
        monkeypatch.setattr(config, attr, val)
    yield tmp_path


def scripted(answers):
    it = iter(answers)
    return lambda prompt="": next(it)


def test_write_env_preserves_and_updates(sandbox):
    env = onboarding.ENV_PATH
    env.write_text("# my config\nST_HQ=OLD\nUNRELATED=keepme\n")
    onboarding.write_env({"ST_AGENT_TOKEN": "abc", "ST_HQ": "NEW"})
    text = env.read_text()
    assert "# my config" in text          # comment preserved
    assert "UNRELATED=keepme" in text     # other keys preserved
    assert "ST_HQ=NEW" in text            # existing key updated in place
    assert "ST_HQ=OLD" not in text
    assert "ST_AGENT_TOKEN=abc" in text   # new key appended


def test_paste_token_flow(sandbox):
    out = []
    token = onboarding.run_wizard(
        input_fn=scripted(["1", "good-agent-token"]),
        output=out.append,
        client_factory=FakeClient,
    )
    assert token == "good-agent-token"
    text = onboarding.ENV_PATH.read_text()
    assert "ST_AGENT_TOKEN=good-agent-token" in text
    assert "ST_AGENT_SYMBOL=TESTER" in text   # auto-filled from the agent
    assert "ST_HQ=X1-Z9-A1" in text
    assert config.AGENT_TOKEN == "good-agent-token"  # runtime applied


def test_bad_token_rejected(sandbox):
    out = []
    token = onboarding.run_wizard(
        input_fn=scripted(["1", "nope"]),
        output=out.append,
        client_factory=FakeClient,
    )
    assert token is None
    assert not onboarding.ENV_PATH.exists()   # nothing written on failure
    assert any("rejected" in m for m in out)


def test_register_flow(sandbox):
    out = []
    # choice 2, account token, callsign, faction (blank -> COSMIC)
    token = onboarding.run_wizard(
        input_fn=scripted(["2", "acct-token", "TESTER", ""]),
        output=out.append,
        client_factory=FakeClient,
    )
    assert token == "registered-token"
    text = onboarding.ENV_PATH.read_text()
    assert "ST_AGENT_TOKEN=registered-token" in text
    assert "ST_ACCOUNT_TOKEN=acct-token" in text


def test_ensure_onboarded_noninteractive_exits(sandbox):
    with pytest.raises(SystemExit):
        onboarding.ensure_onboarded(interactive=False)


def test_ensure_onboarded_returns_existing_token(sandbox, monkeypatch):
    monkeypatch.setattr(config, "AGENT_TOKEN", "already-here")
    assert onboarding.ensure_onboarded() == "already-here"
