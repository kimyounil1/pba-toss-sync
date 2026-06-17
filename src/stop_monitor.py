"""Monitor PBA 조건매도 (conditional sell) prices via live quotes.

PBA "stop" is not an immediate sell — it is the price level at which we exit
when the quote falls to or below stop_price (no native broker stop orders).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from src.broker_runtime import BrokerRuntime
from src.config import AppConfig
from src.db import StateDB
from src.notifier import Notifier
from src.order_qty import floor_qty
from src.position_sizer import OrderPlan
from src.broker_errors import BrokerError

logger = logging.getLogger(__name__)


class StopMonitor:
    def __init__(
        self,
        config: AppConfig,
        runtime: BrokerRuntime,
        db: StateDB,
        notifier: Notifier,
    ) -> None:
        self.config = config
        self.runtime = runtime
        self.bridge = runtime.bridge
        self.executor = runtime.executor
        self.broker_name = runtime.name
        self.db = db
        self.notifier = notifier
        self._triggered: set[str] = set()
        self._failed_at: dict[str, float] = {}
        self._fail_cooldown_sec = 300

    async def run_loop(self) -> None:
        while True:
            try:
                await self._check_stops()
            except Exception as exc:
                logger.exception("Stop monitor error [%s]: %s", self.broker_name, exc)
            await asyncio.sleep(self.config.stop_poll_interval_sec)

    def _trigger_key(self, symbol: str) -> str:
        return f"{self.broker_name}:{symbol}"

    async def _check_stops(self) -> None:
        stops = self.db.list_active_stops(broker=self.broker_name)
        if not stops:
            return

        symbols = [s["symbol"] for s in stops]
        try:
            quotes = self.bridge.quote_batch_live(symbols)
        except BrokerError as exc:
            logger.warning("Quote fetch failed [%s]: %s", self.broker_name, exc)
            return

        for stop in stops:
            symbol = stop["symbol"]
            key = self._trigger_key(symbol)
            if key in self._triggered:
                continue
            last_fail = self._failed_at.get(key, 0.0)
            if time.time() - last_fail < self._fail_cooldown_sec:
                continue
            stop_price = float(stop["stop_price"])
            quote = self._quote_for_symbol(quotes, symbol)
            if not quote:
                quote = self.bridge.quote_get(symbol)
            current = self.bridge.quote_price_krw(quote)
            if current <= 0:
                continue
            if current <= stop_price:
                await self._trigger_stop(symbol, stop_price, current, stop)

    def _quote_for_symbol(self, quotes: dict[str, Any], symbol: str) -> dict[str, Any]:
        if symbol in quotes and isinstance(quotes[symbol], dict):
            return quotes[symbol]
        for key in ("quotes", "items", "data"):
            bucket = quotes.get(key)
            if isinstance(bucket, dict) and symbol in bucket:
                val = bucket[symbol]
                return val if isinstance(val, dict) else {}
            if isinstance(bucket, list):
                for item in bucket:
                    if isinstance(item, dict):
                        sym = self.bridge.position_symbol(item)
                        if sym == symbol:
                            return item
        return {}

    async def _trigger_stop(
        self, symbol: str, stop_price: float, current: float, stop_row: dict[str, Any]
    ) -> None:
        key = self._trigger_key(symbol)
        self._triggered.add(key)
        positions = self.bridge.portfolio_positions()
        qty = float(stop_row.get("qty") or 0)
        for pos in positions:
            if self.bridge.position_symbol(pos) == symbol:
                qty = self.bridge.position_qty(pos)
                break
        qty = floor_qty(qty)
        if qty <= 0:
            logger.warning("Stop triggered but no qty for %s [%s]", symbol, self.broker_name)
            self.db.remove_stop(symbol, broker=self.broker_name)
            return

        sell_limit: float | None = None
        if self.broker_name in {"alpaca", "toss", "tossctl"}:
            session = (
                self.bridge.session_type()
                if hasattr(self.bridge, "session_type")
                else "extended"
            )
            use_limit = True
            if hasattr(self.bridge, "allows_market_orders"):
                use_limit = not self.bridge.allows_market_orders()
            elif self.broker_name == "alpaca" and session == "regular":
                use_limit = self.config.alpaca_limit_orders_only
            elif session == "regular":
                use_limit = False
            if use_limit:
                quote = self.bridge.quote_get(symbol)
                bid = float(
                    quote.get("bid")
                    or quote.get("last")
                    or quote.get("current_price")
                    or self.bridge.quote_price_krw(quote)
                    or current
                )
                buffer_pct = self.config.limit_sell_buffer_pct / 100.0
                sell_limit = bid * (1 - buffer_pct)

        plan = OrderPlan(
            symbol=symbol,
            side="sell",
            delta_krw=qty * (sell_limit or stop_price),
            target_weight_pct=0,
            current_weight_pct=0,
            target_value_krw=0,
            current_value_krw=0,
            use_fractional=False,
            limit_price_krw=sell_limit,
            qty=qty,
        )
        await self.notifier.notify_stop_triggered(symbol, stop_price, current)
        result = await self.executor.execute_plan(
            plan, tweet_id=f"stop-{self.broker_name}-{symbol}", stop_price=None
        )
        if result.get("status") in {"submitted", "dry_run"}:
            self.db.remove_stop(symbol, broker=self.broker_name)
            self._failed_at.pop(key, None)
        else:
            self._triggered.discard(key)
            self._failed_at[key] = time.time()
            logger.warning(
                "Stop sell failed for %s [%s]; cooldown %ss before retry",
                symbol,
                self.broker_name,
                self._fail_cooldown_sec,
            )
