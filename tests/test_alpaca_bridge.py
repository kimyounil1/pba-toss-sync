"""Tests for Alpaca bridge."""

from unittest.mock import MagicMock, patch

from src.alpaca_bridge import AlpacaBridge


def test_auth_status_ok():
    bridge = AlpacaBridge(api_key="key", secret_key="secret", paper=True)
    with patch.object(bridge, "_request", return_value={"status": "ACTIVE", "equity": "100000"}) as req:
        status = bridge.auth_status()
    assert status["logged_in"] is True
    assert status["broker"] == "alpaca"
    assert status["paper"] is True
    req.assert_called_once()


def test_account_summary_usd():
    bridge = AlpacaBridge(api_key="key", secret_key="secret")
    with patch.object(
        bridge,
        "_request",
        return_value={"equity": "100000.50", "buying_power": "200000", "cash": "50000"},
    ):
        summary = bridge.account_summary()
    assert summary["total_eval_amount"] == 100000.50
    assert summary["currency"] == "USD"


def test_order_place_limit_extended_hours():
    bridge = AlpacaBridge(api_key="key", secret_key="secret", extended_hours=True)
    preview = bridge.order_preview(
        symbol="NVDA",
        side="buy",
        qty=2.5,
        price=180.25,
    )
    with (
        patch.object(bridge, "session_type", return_value="extended"),
        patch.object(bridge, "_request", return_value={"id": "ord-1", "status": "accepted"}) as req,
    ):
        result = bridge.order_place(
            symbol="NVDA",
            side="buy",
            confirm_token=preview.confirm_token,
        )
    assert result["order_id"] == "ord-1"
    body = req.call_args.kwargs["json_body"]
    assert body["symbol"] == "NVDA"
    assert body["type"] == "limit"
    assert body["limit_price"] == "180.25"
    assert body["qty"] == "2.5"
    assert body["extended_hours"] is True


def test_order_place_limit_sell_when_market_closed():
    bridge = AlpacaBridge(api_key="key", secret_key="secret")
    with patch.object(bridge, "portfolio_positions", return_value=[{"symbol": "NBIS", "qty": 4.32, "qty_available": 4.32}]):
        preview = bridge.order_preview(symbol="NBIS", side="sell", qty=4.32, price=218.5)
        with (
            patch.object(bridge, "session_type", return_value="closed"),
            patch.object(bridge, "_request", return_value={"id": "ord-sell", "status": "accepted"}) as req,
        ):
            result = bridge.order_place(
                symbol="NBIS",
                side="sell",
                confirm_token=preview.confirm_token,
                qty=4.32,
                price=218.5,
            )
    assert result["order_id"] == "ord-sell"
    body = req.call_args.kwargs["json_body"]
    assert body["side"] == "sell"
    assert body["type"] == "limit"
    assert body["time_in_force"] == "gtc"


def test_order_place_limit_when_market_closed():
    bridge = AlpacaBridge(api_key="key", secret_key="secret", extended_hours=True)
    preview = bridge.order_preview(symbol="NBIS", side="buy", qty=4.32, price=222.67)
    with (
        patch.object(bridge, "session_type", return_value="closed"),
        patch.object(bridge, "_request", return_value={"id": "ord-3", "status": "accepted"}) as req,
    ):
        result = bridge.order_place(
            symbol="NBIS",
            side="buy",
            confirm_token=preview.confirm_token,
        )
    assert result["order_id"] == "ord-3"
    body = req.call_args.kwargs["json_body"]
    assert body["type"] == "limit"
    assert body["limit_price"] == "222.67"
    assert body["time_in_force"] == "gtc"
    assert "extended_hours" not in body


def test_order_place_market_regular_hours():
    bridge = AlpacaBridge(api_key="key", secret_key="secret", limit_orders_only=False)
    preview = bridge.order_preview(
        symbol="NVDA",
        side="buy",
        fractional=True,
        amount_krw=500.0,
    )
    with (
        patch.object(bridge, "session_type", return_value="regular"),
        patch.object(bridge, "_request", return_value={"id": "ord-2", "status": "accepted"}) as req,
    ):
        result = bridge.order_place(
            symbol="NVDA",
            side="buy",
            confirm_token=preview.confirm_token,
        )
    assert result["order_id"] == "ord-2"
    body = req.call_args.kwargs["json_body"]
    assert body["type"] == "market"
    assert body["notional"] == "500.00"
    assert "extended_hours" not in body
