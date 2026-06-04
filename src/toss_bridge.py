"""tossctl CLI wrapper."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any


class TossctlError(RuntimeError):
    pass


@dataclass
class OrderPreview:
    confirm_token: str
    raw: dict[str, Any]


class TossBridge:
    def __init__(self, binary: str, config_dir: str) -> None:
        self.binary = binary
        self.config_dir = config_dir

    def _run(self, *args: str, timeout: int = 120) -> dict[str, Any] | list[Any] | Any:
        cmd = [self.binary, *args, "--output", "json"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=None,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            raise TossctlError(f"tossctl failed ({' '.join(args)}): {stderr}")
        stdout = result.stdout.strip()
        if not stdout:
            return {}
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise TossctlError(f"Invalid JSON from tossctl: {stdout[:500]}") from exc

    def auth_status(self) -> dict[str, Any]:
        cmd = [self.binary, "auth", "status", "--output", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {"logged_in": False, "error": result.stderr.strip()}
        try:
            data = json.loads(result.stdout)
            data["logged_in"] = True
            return data
        except json.JSONDecodeError:
            return {"logged_in": True, "raw": result.stdout}

    def account_summary(self) -> dict[str, Any]:
        data = self._run("account", "summary")
        return data if isinstance(data, dict) else {}

    def portfolio_positions(self) -> list[dict[str, Any]]:
        data = self._run("portfolio", "positions")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("positions", "items", "data"):
                if key in data and isinstance(data[key], list):
                    return data[key]
        return []

    def quote_get(self, symbol: str) -> dict[str, Any]:
        data = self._run("quote", "get", symbol)
        return data if isinstance(data, dict) else {}

    def quote_batch_live(self, symbols: list[str]) -> dict[str, Any]:
        if not symbols:
            return {}
        data = self._run("quote", "batch", *symbols, "--live")
        return data if isinstance(data, dict) else {}

    def market_hours(self) -> dict[str, Any]:
        data = self._run("market", "hours")
        return data if isinstance(data, dict) else {}

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
        args = ["order", "preview", "--symbol", symbol, "--side", side]
        if market == "kr":
            args.extend(["--market", "kr"])
        if fractional and amount_krw is not None:
            args.extend(["--fractional", "--amount", str(int(amount_krw)), "--qty", "0"])
        else:
            if qty is not None:
                args.extend(["--qty", str(qty)])
            if price is not None:
                args.extend(["--price", str(int(price))])
        data = self._run(*args)
        if not isinstance(data, dict):
            raise TossctlError(f"Unexpected preview response: {data}")
        token = (
            data.get("confirm_token")
            or data.get("confirmToken")
            or (data.get("preview") or {}).get("confirm_token")
        )
        if not token:
            raise TossctlError(f"No confirm_token in preview: {data}")
        return OrderPreview(confirm_token=str(token), raw=data)

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
        args = [
            "order",
            "place",
            "--symbol",
            symbol,
            "--side",
            side,
            "--execute",
            "--confirm",
            confirm_token,
        ]
        if market == "kr":
            args.extend(["--market", "kr"])
        if fractional and amount_krw is not None:
            args.extend(["--fractional", "--amount", str(int(amount_krw)), "--qty", "0"])
        else:
            if qty is not None:
                args.extend(["--qty", str(qty)])
            if price is not None:
                args.extend(["--price", str(int(price))])
        data = self._run(*args)
        return data if isinstance(data, dict) else {"raw": data}

    def orders_completed(self, market: str = "us") -> list[dict[str, Any]]:
        data = self._run("orders", "completed", "--market", market)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("orders", "items", "data"):
                if key in data and isinstance(data[key], list):
                    return data[key]
        return []

    def extract_total_value_krw(self, summary: dict[str, Any]) -> float:
        for key in (
            "total_asset_amount",
            "total_asset_amount_krw",
            "total_eval_amount",
            "totalEvalAmount",
            "total_amount",
            "totalAmount",
        ):
            if key in summary and summary[key] is not None:
                return float(summary[key])
        for key in ("total_asset", "totalAsset", "eval_amount", "evalAmount"):
            if key in summary and summary[key] is not None:
                return float(summary[key])
        cash = self.extract_cash_krw(summary)
        positions_val = 0.0
        for k, v in summary.items():
            if "position" in k.lower() and isinstance(v, (int, float)):
                positions_val = float(v)
        return cash + positions_val

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
        qty = float(position.get("qty") or position.get("quantity") or position.get("share") or 0)
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
        return float(
            position.get("qty")
            or position.get("quantity")
            or position.get("share")
            or position.get("holdings")
            or 0
        )

    def quote_price_krw(self, quote: dict[str, Any]) -> float:
        for key in ("current_price", "currentPrice", "price", "close", "last"):
            if key in quote and quote[key] is not None:
                return float(quote[key])
        return 0.0
