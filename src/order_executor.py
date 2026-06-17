"""Execute orders via broker preview→confirm flow."""

from __future__ import annotations

import logging
from typing import Any

from src.config import AppConfig
from src.db import StateDB
from src.notifier import Notifier
from src.position_sizer import OrderPlan
from src.safety import SafetyGuard
from src.broker_errors import BrokerError

logger = logging.getLogger(__name__)


class OrderExecutor:
    def __init__(
        self,
        config: AppConfig,
        bridge: object,
        db: StateDB,
        safety: SafetyGuard,
        notifier: Notifier,
    ) -> None:
        self.config = config
        self.bridge = bridge
        self.db = db
        self.safety = safety
        self.notifier = notifier
        self.broker_name = safety.broker_name

    async def execute_plan(
        self,
        plan: OrderPlan,
        tweet_id: str,
        stop_price: float | None = None,
        leverage_meta: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        block = self.safety.check_order(plan, tweet_id)
        if block:
            logger.info("Order skipped (%s): %s %s", block, plan.symbol, plan.side)
            return {"status": "skipped", "reason": block}

        dry_run = self.safety.is_dry_run()
        plan_dict = {
            "symbol": plan.symbol,
            "side": plan.side,
            "delta_krw": plan.delta_krw,
            "target_weight_pct": plan.target_weight_pct,
            "use_fractional": plan.use_fractional,
            "qty": plan.qty,
            "limit_price_krw": plan.limit_price_krw,
        }
        if leverage_meta:
            plan_dict["leverage_meta"] = leverage_meta

        if dry_run:
            self.db.record_order(
                tweet_id=tweet_id,
                symbol=plan.symbol,
                side=plan.side,
                amount_krw=plan.delta_krw if plan.use_fractional else None,
                qty=plan.qty,
                price=plan.limit_price_krw,
                status="dry_run",
                dry_run=True,
                raw_json=plan_dict,
            )
            if stop_price and plan.side == "buy":
                self.db.upsert_stop(
                    plan.symbol, stop_price, plan.qty, tweet_id, broker=self.broker_name
                )
            await self.notifier.notify_order(
                {**plan_dict, "broker": self.broker_name},
                {"status": "dry_run"},
                dry_run=True,
            )
            return {"status": "dry_run", "plan": plan_dict}

        try:
            preview = self.bridge.order_preview(
                symbol=plan.symbol,
                side=plan.side,
                qty=plan.qty,
                price=plan.limit_price_krw,
                fractional=plan.use_fractional,
                amount_krw=plan.delta_krw if plan.use_fractional else None,
                market=self.config.market,
            )
            result = self.bridge.order_place(
                symbol=plan.symbol,
                side=plan.side,
                confirm_token=preview.confirm_token,
                qty=plan.qty,
                price=plan.limit_price_krw,
                fractional=plan.use_fractional,
                amount_krw=plan.delta_krw if plan.use_fractional else None,
                market=self.config.market,
            )
            order_id = str(result.get("order_id") or result.get("orderId") or "")
            self.db.record_order(
                tweet_id=tweet_id,
                symbol=plan.symbol,
                side=plan.side,
                amount_krw=plan.delta_krw if plan.use_fractional else None,
                qty=plan.qty,
                price=plan.limit_price_krw,
                status="submitted",
                confirm_token=preview.confirm_token,
                order_id=order_id,
                dry_run=False,
                raw_json=result,
            )
            if plan.side == "buy":
                self.safety.record_buy(plan.delta_krw)
            if stop_price and plan.side == "buy":
                self.db.upsert_stop(
                    plan.symbol, stop_price, plan.qty, tweet_id, broker=self.broker_name
                )
            if plan.side == "sell" and plan.target_weight_pct == 0:
                self.db.remove_stop(plan.symbol, broker=self.broker_name)
            await self.notifier.notify_order(
                {**plan_dict, "broker": self.broker_name}, result, dry_run=False
            )
            return {"status": "submitted", "order_id": order_id, "result": result}
        except BrokerError as exc:
            logger.exception("Order failed: %s", exc)
            self.db.record_order(
                tweet_id=tweet_id,
                symbol=plan.symbol,
                side=plan.side,
                amount_krw=plan.delta_krw if plan.use_fractional else None,
                qty=plan.qty,
                price=plan.limit_price_krw,
                status="failed",
                dry_run=False,
                raw_json={"error": str(exc)},
            )
            await self.notifier.send(f"[ORDER FAILED] {plan.symbol}: {exc}")
            return {"status": "failed", "error": str(exc)}

    async def update_stop_only(
        self,
        symbol: str,
        stop_price: float,
        tweet_id: str,
        leverage_meta: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        self.db.upsert_stop(symbol, stop_price, None, tweet_id, broker=self.broker_name)
        underlying = (leverage_meta or {}).get("underlying", symbol)
        note = f" (2x {underlying}→{symbol})" if leverage_meta and leverage_meta.get("leverage") else ""
        await self.notifier.send(
            f"[조건매도 등록][{self.broker_name}] {symbol} stop={stop_price}{note}"
        )
        return {"status": "stop_updated", "symbol": symbol, "stop_price": stop_price}
