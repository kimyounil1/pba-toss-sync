"""Alpaca Markets API bridge (paper or live)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from src.broker_errors import BrokerError
from src.order_qty import format_alpaca_qty
from src.broker_types import OrderPreview


class AlpacaError(BrokerError):
    pass


_ET = ZoneInfo("America/New_York")
_PRE_MARKET_START = time(4, 0)
_REGULAR_OPEN = time(9, 30)
_REGULAR_CLOSE = time(16, 0)
_AFTER_HOURS_END = time(20, 0)


@dataclass
class _PendingOrder:
    symbol: str
    side: str
    qty: float | None
    price: float | None
    fractional: bool
    amount_krw: float | None
    market: str


class AlpacaBridge:
    """Alpaca REST client with the same surface as TossBridge."""

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        paper: bool = True,
        base_url: str = "",
        data_url: str = "https://data.alpaca.markets",
        extended_hours: bool = True,
        limit_orders_only: bool = True,
    ) -> None:
        self.api_key = api_key.strip()
        self.secret_key = secret_key.strip()
        self.paper = paper
        self.extended_hours = extended_hours
        self.limit_orders_only = limit_orders_only
        self.base_url = (
            base_url.strip()
            or ("https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets")
        ).rstrip("/")
        self.data_url = data_url.rstrip("/")
        self._pending: dict[str, _PendingOrder] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> Any:
        if not self.api_key or not self.secret_key:
            raise AlpacaError("ALPACA_API_KEY and ALPACA_SECRET_KEY required")
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.request(method, url, headers=self._headers(), json=json_body)
        except httpx.HTTPError as exc:
            raise AlpacaError(f"Alpaca HTTP error: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text.strip() or response.reason_phrase
            raise AlpacaError(f"Alpaca {method} {url} failed ({response.status_code}): {detail}")
        if not response.content:
            return {}
        return response.json()

    def auth_status(self) -> dict[str, Any]:
        if not self.api_key or not self.secret_key:
            return {
                "logged_in": False,
                "broker": "alpaca",
                "paper": self.paper,
                "error": "missing API keys",
            }
        try:
            account = self._request("GET", f"{self.base_url}/v2/account")
            return {
                "logged_in": True,
                "broker": "alpaca",
                "paper": self.paper,
                "account_status": account.get("status"),
                "equity": account.get("equity"),
                "buying_power": account.get("buying_power"),
            }
        except AlpacaError as exc:
            return {"logged_in": False, "broker": "alpaca", "paper": self.paper, "error": str(exc)}

    def account_summary(self) -> dict[str, Any]:
        account = self._request("GET", f"{self.base_url}/v2/account")
        equity = float(account.get("equity") or 0)
        buying_power = float(account.get("buying_power") or 0)
        cash = float(account.get("cash") or 0)
        return {
            "total_eval_amount": equity,
            "orderable_amount": buying_power,
            "cash": cash,
            "currency": "USD",
            "raw": account,
        }

    def portfolio_positions(self) -> list[dict[str, Any]]:
        data = self._request("GET", f"{self.base_url}/v2/positions")
        if not isinstance(data, list):
            return []
        positions: list[dict[str, Any]] = []
        for pos in data:
            qty = float(pos.get("qty") or 0)
            qty_available = float(pos.get("qty_available") or qty)
            positions.append(
                {
                    "symbol": pos.get("symbol"),
                    "qty": qty,
                    "qty_available": qty_available,
                    "current_price": float(pos.get("current_price") or 0),
                    "eval_amount": float(pos.get("market_value") or 0),
                }
            )
        return positions

    def market_clock(self) -> dict[str, Any]:
        data = self._request("GET", f"{self.base_url}/v2/clock")
        return data if isinstance(data, dict) else {}

    def session_type(self) -> str:
        """regular | extended | closed (US equities, America/New_York)."""
        clock = self.market_clock()
        if clock.get("is_open"):
            return "regular"
        ts = clock.get("timestamp")
        if not ts:
            return "closed"
        now = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(_ET)
        if now.weekday() >= 5:
            return "closed"
        t = now.time()
        if _PRE_MARKET_START <= t < _REGULAR_OPEN:
            return "extended"
        if _REGULAR_CLOSE < t < _AFTER_HOURS_END:
            return "extended"
        return "closed"

    def quote_get(self, symbol: str) -> dict[str, Any]:
        sym = symbol.upper()
        bid = 0.0
        ask = 0.0
        last = 0.0
        try:
            quote = self._request("GET", f"{self.data_url}/v2/stocks/{sym}/quotes/latest")
            q = quote.get("quote") or {}
            bid = float(q.get("bp") or 0)
            ask = float(q.get("ap") or 0)
        except AlpacaError:
            pass
        try:
            trade = self._request("GET", f"{self.data_url}/v2/stocks/{sym}/trades/latest")
            last = float((trade.get("trade") or {}).get("p") or 0)
        except AlpacaError:
            pass
        mid = ask or bid or last
        return {
            "symbol": sym,
            "current_price": mid,
            "bid": bid or last,
            "ask": ask or last,
            "last": last,
            "currency": "USD",
        }

    def quote_batch_live(self, symbols: list[str]) -> dict[str, Any]:
        quotes: dict[str, Any] = {}
        for symbol in symbols:
            quotes[symbol.upper()] = self.quote_get(symbol)
        return quotes

    def list_us_equity_assets(self) -> list[dict[str, Any]]:
        """All active US equities — used for 2x ETF auto-discovery."""
        data = self._request(
            "GET",
            f"{self.base_url}/v2/assets?status=active&asset_class=us_equity",
            timeout=60.0,
        )
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

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
        self._pending[token] = _PendingOrder(
            symbol=symbol.upper(),
            side=side.lower(),
            qty=qty,
            price=price,
            fractional=fractional,
            amount_krw=amount_krw,
            market=market,
        )
        return OrderPreview(confirm_token=token, raw={"broker": "alpaca", "confirm_token": token})

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
                market=market,
            )

        session = self.session_type()
        # Regular session: market (unless limit_orders_only). Extended/closed: live-quote limit.
        use_limit = self.limit_orders_only or session != "regular"
        if use_limit:
            if pending.price is None or pending.price <= 0:
                raise AlpacaError(f"limit order requires price for {pending.symbol}")
            if pending.qty is None or pending.qty <= 0:
                raise AlpacaError(f"limit order requires qty for {pending.symbol}")
            tif = "gtc" if session == "closed" else "day"
            body: dict[str, Any] = {
                "symbol": pending.symbol,
                "side": pending.side,
                "type": "limit",
                "time_in_force": tif,
                "limit_price": f"{pending.price:.2f}",
                "qty": (
                    self._format_sell_qty(pending.symbol, pending.qty)
                    if pending.side == "sell"
                    else format_alpaca_qty(pending.qty)
                ),
            }
            if session == "extended":
                if not self.extended_hours:
                    raise AlpacaError("extended hours trading disabled")
                body["extended_hours"] = True
        else:
            body = {
                "symbol": pending.symbol,
                "side": pending.side,
                "time_in_force": "day",
            }
            if pending.fractional and pending.amount_krw is not None and pending.side == "buy":
                body["type"] = "market"
                body["notional"] = f"{pending.amount_krw:.2f}"
            elif pending.price is not None and pending.price > 0:
                body["type"] = "limit"
                body["limit_price"] = f"{pending.price:.2f}"
                body["qty"] = format_alpaca_qty(pending.qty or 0)
            else:
                body["type"] = "market"
                if pending.qty is not None and pending.qty > 0:
                    body["qty"] = self._format_sell_qty(pending.symbol, pending.qty)
                elif pending.amount_krw is not None and pending.side == "buy":
                    body["notional"] = f"{pending.amount_krw:.2f}"
                else:
                    raise AlpacaError(f"order missing qty/notional for {pending.symbol}")

        result = self._request("POST", f"{self.base_url}/v2/orders", json_body=body)
        return {
            "order_id": result.get("id"),
            "status": result.get("status"),
            "broker": "alpaca",
            "raw": result,
        }

    def _format_sell_qty(self, symbol: str, qty: float) -> str:
        available: float | None = None
        for pos in self.portfolio_positions():
            if self.position_symbol(pos) == symbol.upper():
                available = float(pos.get("qty_available") or pos.get("qty") or 0)
                break
        return format_alpaca_qty(qty, available=available)

    def orders_completed(self, market: str = "us") -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            f"{self.base_url}/v2/orders?status=closed&limit=50&direction=desc",
        )
        return data if isinstance(data, list) else []

    def extract_total_value_krw(self, summary: dict[str, Any]) -> float:
        for key in (
            "total_asset_amount",
            "total_eval_amount",
            "totalEvalAmount",
            "equity",
        ):
            if key in summary and summary[key] is not None:
                return float(summary[key])
        cash = self.extract_cash_krw(summary)
        positions_val = 0.0
        for k, v in summary.items():
            if "position" in k.lower() and isinstance(v, (int, float)):
                positions_val = float(v)
        return cash + positions_val

    def extract_cash_krw(self, summary: dict[str, Any]) -> float:
        for key in ("orderable_amount", "cash", "buying_power"):
            if key in summary and summary[key] is not None:
                return float(summary[key])
        return 0.0

    def position_value_krw(self, position: dict[str, Any]) -> float:
        for key in ("eval_amount", "market_value", "current_amount"):
            if key in position and position[key] is not None:
                return float(position[key])
        qty = float(position.get("qty") or 0)
        price = float(position.get("current_price") or 0)
        return qty * price

    def position_symbol(self, position: dict[str, Any]) -> str:
        return str(position.get("symbol") or position.get("ticker") or "").upper()

    def position_qty(self, position: dict[str, Any]) -> float:
        return float(position.get("qty") or position.get("quantity") or 0)

    def quote_price_krw(self, quote: dict[str, Any]) -> float:
        for key in ("current_price", "price", "close", "last"):
            if key in quote and quote[key] is not None:
                return float(quote[key])
        return 0.0
