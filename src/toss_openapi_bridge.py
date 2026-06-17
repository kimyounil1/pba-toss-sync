"""Toss Securities Open API bridge (https://openapi.tossinvest.com)."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from src.broker_errors import BrokerError
from src.broker_types import OrderPreview
from src.us_market_session import (
    UsSession,
    allows_amount_order,
    allows_market_order,
    resolve_us_session,
)


class TossOpenApiError(BrokerError):
    pass


_BASE_URL = "https://openapi.tossinvest.com"


@dataclass
class _PendingOrder:
    symbol: str
    side: str
    qty: float | None
    price: float | None
    fractional: bool
    amount_krw: float | None
    market: str
    client_order_id: str


class TossOpenApiBridge:
    """REST client with the same surface as TossBridge / AlpacaBridge."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        account_seq: int | None = None,
        base_url: str = _BASE_URL,
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.account_seq = account_seq
        self.base_url = base_url.rstrip("/")
        self._access_token: str = ""
        self._token_expires_at: float = 0.0
        self._pending: dict[str, _PendingOrder] = {}
        self._calendar_cache: tuple[float, dict[str, Any]] | None = None

    def auth_status(self) -> dict[str, Any]:
        if not self.client_id or not self.client_secret:
            return {
                "logged_in": False,
                "broker": "toss",
                "error": "missing TOSS_CLIENT_ID / TOSS_CLIENT_SECRET",
            }
        try:
            self._ensure_token()
            accounts = self._api("GET", "/api/v1/accounts")
            seq = self._resolve_account_seq(accounts)
            return {
                "logged_in": True,
                "broker": "toss",
                "account_seq": seq,
                "accounts": accounts,
            }
        except TossOpenApiError as exc:
            return {"logged_in": False, "broker": "toss", "error": str(exc)}

    def us_session(self) -> UsSession:
        return resolve_us_session(self.market_hours())

    def session_type(self) -> str:
        """Backward-compat: regular | extended | closed."""
        session = self.us_session()
        if session == "regular":
            return "regular"
        if session == "closed":
            return "closed"
        return "extended"

    def allows_market_orders(self) -> bool:
        return allows_market_order(self.us_session())

    def allows_amount_orders(self) -> bool:
        return allows_amount_order(self.us_session())

    def account_summary(self) -> dict[str, Any]:
        holdings = self._holdings_overview()
        buying = self._buying_power()
        market_value = holdings.get("marketValue") or {}
        amount = (market_value.get("amount") or {}) if isinstance(market_value, dict) else {}
        cash = float(buying.get("cashBuyingPower") or 0)
        currency = str(buying.get("currency") or "USD").upper()
        usd_positions = float(amount.get("usd") or 0) if isinstance(amount, dict) else 0.0
        krw_positions = float(amount.get("krw") or 0) if isinstance(amount, dict) else 0.0
        if currency == "USD":
            total = usd_positions + cash
        else:
            total = krw_positions + cash
        return {
            "total_eval_amount": total,
            "orderable_amount": cash,
            "cash": cash,
            "currency": currency,
            "raw": {"holdings": holdings, "buying_power": buying},
        }

    def portfolio_positions(self) -> list[dict[str, Any]]:
        overview = self._holdings_overview()
        items = overview.get("items") or []
        if not isinstance(items, list):
            return []
        positions: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            qty = float(item.get("quantity") or 0)
            last = float(item.get("lastPrice") or 0)
            mv = item.get("marketValue") or {}
            eval_amount = 0.0
            if isinstance(mv, dict) and mv.get("amount") is not None:
                eval_amount = float(mv["amount"])
            positions.append(
                {
                    "symbol": item.get("symbol"),
                    "qty": qty,
                    "qty_available": qty,
                    "current_price": last,
                    "eval_amount": eval_amount,
                    "currency": item.get("currency"),
                    "market_country": item.get("marketCountry"),
                }
            )
        return positions

    def quote_get(self, symbol: str) -> dict[str, Any]:
        data = self._api("GET", "/api/v1/prices", params={"symbols": symbol.upper()})
        rows = data if isinstance(data, list) else []
        if not rows:
            return {}
        row = rows[0] if isinstance(rows[0], dict) else {}
        last = float(row.get("lastPrice") or 0)
        book = self._orderbook(symbol)
        return {
            "symbol": row.get("symbol") or symbol.upper(),
            "current_price": last,
            "last": last,
            "bid": book.get("bid", last),
            "ask": book.get("ask", last),
            "currency": row.get("currency"),
            "raw": row,
        }

    def quote_batch_live(self, symbols: list[str]) -> dict[str, Any]:
        if not symbols:
            return {}
        joined = ",".join(s.upper() for s in symbols)
        data = self._api("GET", "/api/v1/prices", params={"symbols": joined})
        rows = data if isinstance(data, list) else []
        out: dict[str, Any] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper()
            if not sym:
                continue
            last = float(row.get("lastPrice") or 0)
            out[sym] = {"current_price": last, "last": last, "symbol": sym}
        return out

    def market_hours(self) -> dict[str, Any]:
        now = time.time()
        if self._calendar_cache and now - self._calendar_cache[0] < 60:
            return self._calendar_cache[1]
        data = self._api("GET", "/api/v1/market-calendar/US")
        payload = data if isinstance(data, dict) else {}
        self._calendar_cache = (now, payload)
        return payload

    def order_preview(
        self,
        *,
        symbol: str,
        side: str,
        qty: float | None = None,
        price: float | None = None,
        fractional: bool = False,
        amount_krw: float | None = None,
        market: str = "us",
    ) -> OrderPreview:
        token = str(uuid.uuid4())
        client_order_id = _client_order_id(token)
        self._pending[token] = _PendingOrder(
            symbol=symbol.upper(),
            side=side.lower(),
            qty=qty,
            price=price,
            fractional=fractional,
            amount_krw=amount_krw,
            market=market.lower(),
            client_order_id=client_order_id,
        )
        return OrderPreview(
            confirm_token=token,
            raw={"broker": "toss", "confirm_token": token, "client_order_id": client_order_id},
        )

    def order_place(
        self,
        *,
        symbol: str,
        side: str,
        confirm_token: str,
        qty: float | None = None,
        price: float | None = None,
        fractional: bool = False,
        amount_krw: float | None = None,
        market: str = "us",
    ) -> dict[str, Any]:
        pending = self._pending.pop(confirm_token, None)
        if pending is None:
            pending = _PendingOrder(
                symbol=symbol.upper(),
                side=side.lower(),
                qty=qty,
                price=price,
                fractional=fractional,
                amount_krw=amount_krw,
                market=market.lower(),
                client_order_id=_client_order_id(confirm_token),
            )
        body = self._build_order_body(pending)
        result = self._api("POST", "/api/v1/orders", json_body=body, account=True)
        order_id = ""
        if isinstance(result, dict):
            order_id = str(result.get("orderId") or result.get("order_id") or "")
        return {
            "order_id": order_id,
            "orderId": order_id,
            "client_order_id": pending.client_order_id,
            "broker": "toss",
            "raw": result,
        }

    def orders_completed(self, market: str = "us") -> list[dict[str, Any]]:
        _ = market
        data = self._api(
            "GET",
            "/api/v1/orders",
            params={"status": "CLOSED"},
            account=True,
        )
        if isinstance(data, dict):
            orders = data.get("orders")
            if isinstance(orders, list):
                return orders
        if isinstance(data, list):
            return data
        return []

    def extract_total_value_krw(self, summary: dict[str, Any]) -> float:
        for key in (
            "total_eval_amount",
            "total_asset_amount",
            "total_asset_amount_krw",
            "total_eval_amount",
            "totalEvalAmount",
        ):
            if key in summary and summary[key] is not None:
                return float(summary[key])
        cash = self.extract_cash_krw(summary)
        return cash

    def extract_cash_krw(self, summary: dict[str, Any]) -> float:
        for key in (
            "orderable_amount",
            "orderableAmount",
            "cash",
            "available_cash",
            "availableCash",
            "withdrawable_amount",
        ):
            if key in summary and summary[key] is not None:
                return float(summary[key])
        return 0.0

    def position_value_krw(self, position: dict[str, Any]) -> float:
        for key in ("eval_amount", "evalAmount", "current_amount", "currentAmount", "amount"):
            if key in position and position[key] is not None:
                return float(position[key])
        qty = float(position.get("qty") or position.get("quantity") or 0)
        price = float(position.get("current_price") or position.get("currentPrice") or 0)
        return qty * price

    def position_symbol(self, position: dict[str, Any]) -> str:
        return str(
            position.get("symbol")
            or position.get("stock_code")
            or position.get("stockCode")
            or position.get("ticker")
            or ""
        ).upper()

    def position_qty(self, position: dict[str, Any]) -> float:
        for key in ("qty_available", "qty", "quantity", "share", "holdings"):
            if key in position and position[key] is not None:
                return float(position[key])
        return 0.0

    def quote_price_krw(self, quote: dict[str, Any]) -> float:
        for key in (
            "current_price",
            "currentPrice",
            "last",
            "reference_price",
            "price",
            "close",
        ):
            if key in quote and quote[key] is not None:
                return float(quote[key])
        return 0.0

    def _build_order_body(self, pending: _PendingOrder) -> dict[str, Any]:
        session = self.us_session()
        use_market = allows_market_order(session)
        side = "BUY" if pending.side == "buy" else "SELL"
        symbol = pending.symbol

        if pending.fractional and pending.side == "buy" and pending.amount_krw is not None:
            if allows_amount_order(session):
                return {
                    "clientOrderId": pending.client_order_id,
                    "symbol": symbol,
                    "side": side,
                    "orderType": "MARKET",
                    "orderAmount": _format_usd(pending.amount_krw),
                }
            quote = self.quote_get(symbol)
            ref = float(quote.get("ask") or quote.get("current_price") or 0)
            if ref <= 0:
                raise TossOpenApiError(f"no quote to size day-market buy for {symbol}")
            qty = pending.amount_krw / ref
            return {
                "clientOrderId": pending.client_order_id,
                "symbol": symbol,
                "side": side,
                "orderType": "MARKET",
                "quantity": _format_quantity(qty, market=pending.market),
            }

        if use_market and pending.price is None:
            if pending.qty is None or pending.qty <= 0:
                raise TossOpenApiError(f"market order requires qty for {symbol}")
            return {
                "clientOrderId": pending.client_order_id,
                "symbol": symbol,
                "side": side,
                "orderType": "MARKET",
                "quantity": _format_quantity(pending.qty, market=pending.market),
            }

        if pending.price is None or pending.price <= 0:
            raise TossOpenApiError(f"limit order requires price for {symbol}")
        if pending.qty is None or pending.qty <= 0:
            raise TossOpenApiError(f"limit order requires qty for {symbol}")
        return {
            "clientOrderId": pending.client_order_id,
            "symbol": symbol,
            "side": side,
            "orderType": "LIMIT",
            "timeInForce": "DAY",
            "quantity": _format_quantity(pending.qty, market=pending.market, integer_only=True),
            "price": _format_price(pending.price, market=pending.market),
        }

    def _holdings_overview(self) -> dict[str, Any]:
        data = self._api("GET", "/api/v1/holdings", account=True)
        return data if isinstance(data, dict) else {}

    def _buying_power(self) -> dict[str, Any]:
        data = self._api("GET", "/api/v1/buying-power", account=True)
        return data if isinstance(data, dict) else {}

    def _orderbook(self, symbol: str) -> dict[str, float]:
        data = self._api("GET", "/api/v1/orderbook", params={"symbol": symbol.upper()})
        if not isinstance(data, dict):
            return {}
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        bid = 0.0
        ask = 0.0
        if bids and isinstance(bids[0], dict):
            bid = float(bids[0].get("price") or 0)
        if asks and isinstance(asks[0], dict):
            ask = float(asks[0].get("price") or 0)
        return {"bid": bid, "ask": ask}

    def _resolve_account_seq(self, accounts: Any) -> int:
        if self.account_seq is not None:
            return int(self.account_seq)
        if isinstance(accounts, list) and accounts:
            first = accounts[0]
            if isinstance(first, dict) and first.get("accountSeq") is not None:
                self.account_seq = int(first["accountSeq"])
                return self.account_seq
        raise TossOpenApiError("No Toss account found — check Open API registration")

    def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token
        if not self.client_id or not self.client_secret:
            raise TossOpenApiError("TOSS_CLIENT_ID and TOSS_CLIENT_SECRET required")
        body = urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        )
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    f"{self.base_url}/oauth2/token",
                    content=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            raise TossOpenApiError(f"Toss token HTTP error: {exc}") from exc
        if response.status_code >= 400:
            raise TossOpenApiError(
                f"Toss token failed ({response.status_code}): {response.text.strip()}"
            )
        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            raise TossOpenApiError(f"Toss token missing in response: {payload}")
        expires_in = int(payload.get("expires_in") or 3600)
        self._access_token = token
        self._token_expires_at = time.time() + expires_in
        return token

    def _api(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        account: bool = False,
        timeout: float = 30.0,
    ) -> Any:
        token = self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        if account:
            if self.account_seq is None:
                accounts = self._api("GET", "/api/v1/accounts")
                self._resolve_account_seq(accounts)
            headers["X-Tossinvest-Account"] = str(self.account_seq)
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                )
        except httpx.HTTPError as exc:
            raise TossOpenApiError(f"Toss HTTP error: {exc}") from exc
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "1")
            raise TossOpenApiError(f"Toss rate limited — retry after {retry}s")
        if response.status_code >= 400:
            detail = _format_api_error(response)
            raise TossOpenApiError(f"Toss {method} {path} failed ({response.status_code}): {detail}")
        if not response.content:
            return {}
        payload = response.json()
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload


def _client_order_id(seed: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\-_]", "-", seed)[:36]
    return cleaned or f"pba-{uuid.uuid4().hex[:24]}"


def _format_api_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or response.reason_phrase
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            code = err.get("code") or ""
            message = err.get("message") or ""
            return f"{code}: {message}".strip(": ")
    return response.text.strip() or response.reason_phrase


def _format_usd(amount: float) -> str:
    if amount >= 1:
        return f"{amount:.2f}".rstrip("0").rstrip(".")
    return f"{amount:.4f}".rstrip("0").rstrip(".")


def _format_price(price: float, *, market: str) -> str:
    if market == "kr":
        return str(int(round(price)))
    return _format_usd(price)


def _format_quantity(qty: float, *, market: str, integer_only: bool = False) -> str:
    if market == "kr" or integer_only:
        return str(int(qty))
    if abs(qty - int(qty)) < 1e-9:
        return str(int(qty))
    if qty >= 1:
        return f"{qty:.2f}".rstrip("0").rstrip(".")
    return f"{qty:.4f}".rstrip("0").rstrip(".")
