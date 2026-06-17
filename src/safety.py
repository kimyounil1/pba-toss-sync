"""Safety limits, idempotency, and kill switch."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from src.config import AppConfig
from src.db import StateDB
from src.position_sizer import OrderPlan

logger = logging.getLogger(__name__)


class SafetyGuard:
    def __init__(self, config: AppConfig, db: StateDB, *, broker_name: str | None = None) -> None:
        self.config = config
        self.db = db
        self.broker_name = (broker_name or config.broker or "toss").lower()

    def kill_switch_active(self) -> bool:
        if self.broker_name in {"alpaca", "toss"}:
            return not self.config.live_trading_enabled
        config_path = Path(self.config.tossctl_config_dir) / "config.json"
        if not config_path.is_file():
            return True
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            trading = data.get("trading") or {}
            return not bool(trading.get("allow_live_order_actions", False))
        except (json.JSONDecodeError, OSError):
            return True

    def check_order(self, plan: OrderPlan, tweet_id: str) -> str | None:
        if self.db.tweet_seen(tweet_id):
            return "duplicate_tweet"

        if plan.skip_reason:
            return plan.skip_reason

        if not plan.should_execute:
            return "nothing_to_do"

        if plan.side == "buy":
            daily = self.db.get_daily_buy_total()
            if daily + plan.delta_krw > self.config.daily_buy_limit_krw:
                return "daily_buy_limit_exceeded"

            if plan.target_weight_pct > self.config.max_position_pct:
                return "max_position_pct_exceeded"

        return None

    def record_buy(self, amount_krw: float) -> None:
        self.db.add_daily_buy(amount_krw)

    def is_dry_run(self) -> bool:
        return self.config.dry_run or self.kill_switch_active()
