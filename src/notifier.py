"""Telegram and audit notifications."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src.config import AppConfig

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def send(self, message: str) -> None:
        logger.info("NOTIFY: %s", message)
        self._audit("notification", {"message": message})
        if not self.config.telegram_enabled:
            return
        if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            return
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(
                    url,
                    json={"chat_id": self.config.telegram_chat_id, "text": message[:4000]},
                )
        except httpx.HTTPError as exc:
            logger.warning("Telegram send failed: %s", exc)

    def _audit(self, event: str, payload: dict[str, Any]) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **payload,
        }
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        path = self.config.audit_dir / f"audit_{day}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def audit(self, event: str, **payload: Any) -> None:
        self._audit(event, payload)

    async def notify_signal(self, tweet_id: str, text: str, signal: dict[str, Any]) -> None:
        msg = (
            f"[PBA Signal] tweet={tweet_id}\n"
            f"action={signal.get('action')} symbol={signal.get('symbol')}\n"
            f"confidence={signal.get('confidence')}\n"
            f"{signal.get('reasoning', '')}"
        )
        await self.send(msg)
        self.audit("signal", tweet_id=tweet_id, text=text, signal=signal)

    async def notify_order(self, plan: dict[str, Any], result: dict[str, Any], dry_run: bool) -> None:
        prefix = "[DRY-RUN]" if dry_run else "[ORDER]"
        msg = f"{prefix} {plan.get('side')} {plan.get('symbol')} delta={plan.get('delta_krw')} KRW"
        await self.send(msg)
        self.audit("order", plan=plan, result=result, dry_run=dry_run)

    async def notify_stop_triggered(self, symbol: str, stop_price: float, sell_price: float) -> None:
        msg = f"[STOP TRIGGERED] {symbol} stop={stop_price} sell_limit={sell_price}"
        await self.send(msg)
        self.audit("stop_triggered", symbol=symbol, stop_price=stop_price, sell_price=sell_price)
