"""Tests for tossctl bridge (mocked CLI — no live connection)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.toss_bridge import TossBridge, TossctlError


def test_auth_status_not_logged_in():
    bridge = TossBridge("/bin/tossctl", "/tmp/tossctl")
    proc = MagicMock(returncode=1, stdout="", stderr="not logged in")
    with patch("src.toss_bridge.subprocess.run", return_value=proc) as run:
        status = bridge.auth_status()
    assert status["logged_in"] is False
    assert status["broker"] == "tossctl"
    cmd = run.call_args.args[0]
    assert "--config-dir" in cmd
    assert "/tmp/tossctl" in cmd


def test_order_preview_fractional_buy():
    bridge = TossBridge("/bin/tossctl", "/tmp/tossctl")
    payload = {"confirm_token": "tok-abc"}
    proc = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
    with patch("src.toss_bridge.subprocess.run", return_value=proc) as run:
        preview = bridge.order_preview(
            symbol="NVDA",
            side="buy",
            fractional=True,
            amount_krw=150_000,
            market="us",
        )
    assert preview.confirm_token == "tok-abc"
    cmd = run.call_args.args[0]
    assert "order" in cmd and "preview" in cmd
    assert "--fractional" in cmd
    assert "150000" in cmd


def test_order_place_limit_sell():
    bridge = TossBridge("/bin/tossctl", "/tmp/tossctl")
    preview_proc = MagicMock(
        returncode=0, stdout=json.dumps({"confirm_token": "tok-sell"}), stderr=""
    )
    place_proc = MagicMock(returncode=0, stdout='{"order_id":"o-1"}', stderr="")
    with patch("src.toss_bridge.subprocess.run", side_effect=[preview_proc, place_proc]) as run:
        preview = bridge.order_preview(symbol="DOCN", side="sell", qty=2, price=157_000)
        result = bridge.order_place(
            symbol="DOCN",
            side="sell",
            confirm_token=preview.confirm_token,
            qty=2,
            price=157_000,
        )
    place_cmd = run.call_args_list[1].args[0]
    assert result["order_id"] == "o-1"
    assert preview.confirm_token == "tok-sell"
    assert "place" in place_cmd
    assert "--qty" in place_cmd
    assert "2" in place_cmd
    assert "--price" in place_cmd
    assert "157000" in place_cmd


def test_session_type_regular():
    bridge = TossBridge("/bin/tossctl", "/tmp/tossctl")
    with patch.object(bridge, "market_hours", return_value={"is_open": True}):
        assert bridge.session_type() == "regular"


def test_session_type_extended():
    bridge = TossBridge("/bin/tossctl", "/tmp/tossctl")
    with patch.object(bridge, "market_hours", return_value={"extended_open": True}):
        assert bridge.session_type() == "extended"


def test_run_raises_on_cli_error():
    bridge = TossBridge("/bin/tossctl", "/tmp/tossctl")
    proc = MagicMock(returncode=1, stdout="", stderr="boom")
    with patch("src.toss_bridge.subprocess.run", return_value=proc):
        with pytest.raises(TossctlError, match="boom"):
            bridge.quote_get("NVDA")
