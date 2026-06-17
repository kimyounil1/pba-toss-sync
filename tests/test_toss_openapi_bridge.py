"""Tests for Toss Open API bridge (mocked HTTP)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.toss_openapi_bridge import TossOpenApiBridge, TossOpenApiError


def _response(status: int, payload: dict) -> MagicMock:
    resp = MagicMock(status_code=status, content=json.dumps(payload).encode())
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    resp.reason_phrase = "error"
    resp.headers = {}
    return resp


def test_auth_status_missing_keys():
    bridge = TossOpenApiBridge(client_id="", client_secret="")
    status = bridge.auth_status()
    assert status["logged_in"] is False
    assert "missing" in status["error"]


def test_order_place_market_sell():
    bridge = TossOpenApiBridge(client_id="id", client_secret="secret", account_seq=1)
    token_resp = _response(200, {"access_token": "tok", "expires_in": 3600})
    order_resp = _response(200, {"result": {"orderId": "ord-1", "clientOrderId": "cid-1"}})
    with patch("src.toss_openapi_bridge.httpx.Client") as client_cls, patch.object(
        bridge, "us_session", return_value="day_market"
    ):
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = token_resp
        client.request.return_value = order_resp
        preview = bridge.order_preview(symbol="NVDA", side="sell", qty=2)
        result = bridge.order_place(
            symbol="NVDA",
            side="sell",
            confirm_token=preview.confirm_token,
            qty=2,
        )
    assert result["order_id"] == "ord-1"
    order_call = client.request.call_args_list[-1]
    body = order_call.kwargs["json"]
    assert body["orderType"] == "MARKET"
    assert body["side"] == "SELL"
    assert body["quantity"] == "2"


def test_order_place_limit_when_pre_market():
    bridge = TossOpenApiBridge(client_id="id", client_secret="secret", account_seq=1)
    token_resp = _response(200, {"access_token": "tok", "expires_in": 3600})
    order_resp = _response(200, {"result": {"orderId": "ord-2"}})
    with patch("src.toss_openapi_bridge.httpx.Client") as client_cls, patch.object(
        bridge, "us_session", return_value="pre_market"
    ):
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = token_resp
        client.request.return_value = order_resp
        preview = bridge.order_preview(symbol="NVDA", side="sell", qty=1, price=120.5)
        result = bridge.order_place(
            symbol="NVDA",
            side="sell",
            confirm_token=preview.confirm_token,
            qty=1,
            price=120.5,
        )
    assert result["order_id"] == "ord-2"
    body = client.request.call_args_list[-1].kwargs["json"]
    assert body["orderType"] == "LIMIT"
    assert body["price"] == "120.5"


def test_quote_get_uses_prices_and_orderbook():
    bridge = TossOpenApiBridge(client_id="id", client_secret="secret")
    token_resp = _response(200, {"access_token": "tok", "expires_in": 3600})
    with patch("src.toss_openapi_bridge.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = token_resp
        client.request.side_effect = [
            _response(
                200,
                {
                    "result": [
                        {
                            "symbol": "AAPL",
                            "lastPrice": "190.25",
                            "currency": "USD",
                        }
                    ]
                },
            ),
            _response(
                200,
                {
                    "result": {
                        "bids": [{"price": "190.2", "volume": "10"}],
                        "asks": [{"price": "190.3", "volume": "12"}],
                    }
                },
            ),
        ]
        quote = bridge.quote_get("AAPL")
    assert quote["current_price"] == 190.25
    assert quote["bid"] == 190.2
    assert quote["ask"] == 190.3


def test_api_error_message():
    bridge = TossOpenApiBridge(client_id="id", client_secret="secret")
    token_resp = _response(200, {"access_token": "tok", "expires_in": 3600})
    err_resp = _response(
        422,
        {
            "error": {
                "code": "order-hours-closed",
                "message": "주문 접수 불가",
            }
        },
    )
    with patch("src.toss_openapi_bridge.httpx.Client") as client_cls:
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = token_resp
        client.request.return_value = err_resp
        with pytest.raises(TossOpenApiError, match="order-hours-closed"):
            bridge.quote_get("NVDA")
