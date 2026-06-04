"""Monitor PBA 조건매도 (conditional sell) prices via live quotes.

PBA "stop" is not an immediate sell — it is the price level at which we exit
when the quote falls to or below stop_price (tossctl has no native stop orders).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.config import AppConfig
from src.db import StateDB
from src.notifier import Notifier
from src.order_executor import OrderExecutor
from src.position_sizer import OrderPlan
from src.toss_bridge import TossBridge, TossctlError

logger = logging.getLogger(__name__)


class StopMonitor:
    def __init__(
        self,
        config: AppConfig,
        bridge: TossBridge,
        db: StateDB,
        executor: OrderExecutor,
        notifier: Notifier,
    ) -> None:
        self.config = config
        self.bridge = bridge
        self.db = db
        self.executor = executor
        self.notifier = notifier
        self._triggered: set[str] = set()

    async def run_loop(self) -> None:
        while True:
            try:
                await self._check_stops()
            except Exception as exc:
                logger.exception("Stop monitor error: %s", exc)
            await asyncio.sleep(self.config.stop_poll_interval_sec)

    async def _check_stops(self) -> None:
        stops = self.db.list_active_stops()
        if not stops:
            return

        symbols = [s["symbol"] for s in stops]
        try:
            quotes = self.bridge.quote_batch_live(symbols)
        except TossctlError as exc:
            logger.warning("Quote fetch failed: %s", exc)
            return

        for stop in stops:
            symbol = stop["symbol"]
            if symbol in self._triggered:
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
        self._triggered.add(symbol)
        buffer_pct = self.config.sell_price_buffer_pct / 100.0
        sell_price = stop_price * (1 - buffer_pct)
        positions = self.bridge.portfolio_positions()
        qty = float(stop_row.get("qty") or 0)
        for pos in positions:
            if self.bridge.position_symbol(pos) == symbol:
                qty = self.bridge.position_qty(pos)
                break
        if qty <= 0:
            logger.warning("Stop triggered but no qty for %s", symbol)
            self.db.remove_stop(symbol)
            return

        plan = OrderPlan(
            symbol=symbol,
            side="sell",
            delta_krw=qty * sell_price,
            target_weight_pct=0,
            current_weight_pct=0,
            target_value_krw=0,
            current_value_krw=0,
            use_fractional=False,
            limit_price_krw=sell_price,
            qty=qty,
        )
        await self.notifier.notify_stop_triggered(symbol, stop_price, sell_price)
        result = await self.executor.execute_plan(plan, tweet_id=f"stop-{symbol}", stop_price=None)
        if result.get("status") in {"submitted", "dry_run"}:
            self.db.remove_stop(symbol)
        else:
            self._triggered.discard(symbol)
