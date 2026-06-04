import json

from x_auth_helper.cli import build_storage_state


def test_build_storage_state_has_required_cookies():
    state = build_storage_state("abc123", "csrf456")
    names = {c["name"] for c in state["cookies"]}
    assert "auth_token" in names
    assert "ct0" in names
    assert any(c["domain"] == ".x.com" for c in state["cookies"])


def test_build_storage_state_serializable():
    state = build_storage_state("t", "c")
    raw = json.dumps(state)
    assert "auth_token" in raw
