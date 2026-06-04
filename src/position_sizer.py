"""Calculate order deltas from target portfolio weights."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.config import AppConfig
from src.toss_bridge import TossBridge


@dataclass
class OrderPlan:
    symbol: str
    side: str  # buy | sell
    delta_krw: float
    target_weight_pct: float
    current_weight_pct: float
    target_value_krw: float
    current_value_krw: float
    use_fractional: bool
    limit_price_krw: float | None = None
    qty: float | None = None
    skip_reason: str | None = None

    @property
    def should_execute(self) -> bool:
        return self.skip_reason is None and abs(self.delta_krw) > 0


class PositionSizer:
    def __init__(self, config: AppConfig, bridge: TossBridge) -> None:
        self.config = config
        self.bridge = bridge

    def build_plan(
        self,
        symbol: str,
        target_weight_pct: float,
        entry_price_krw: float | None = None,
    ) -> OrderPlan:
        summary = self.bridge.account_summary()
        positions = self.bridge.portfolio_positions()
        total_value = self.bridge.extract_total_value_krw(summary)
        if total_value <= 0:
            return OrderPlan(
                symbol=symbol,
                side="buy",
                delta_krw=0,
                target_weight_pct=target_weight_pct,
                current_weight_pct=0,
                target_value_krw=0,
                current_value_krw=0,
                use_fractional=self.config.use_fractional_buy,
                skip_reason="total_portfolio_value_zero",
            )

        current_value = 0.0
        current_qty = 0.0
        for pos in positions:
            if self.bridge.position_symbol(pos) == symbol.upper():
                current_value = self.bridge.position_value_krw(pos)
                current_qty = self.bridge.position_qty(pos)
                break

        capped_target_pct = min(target_weight_pct, self.config.max_position_pct)
        target_value = total_value * capped_target_pct / 100.0
        current_weight_pct = (current_value / total_value * 100.0) if total_value else 0.0
        delta = target_value - current_value

        plan = OrderPlan(
            symbol=symbol.upper(),
            side="buy" if delta > 0 else "sell",
            delta_krw=abs(delta),
            target_weight_pct=capped_target_pct,
            current_weight_pct=current_weight_pct,
            target_value_krw=target_value,
            current_value_krw=current_value,
            use_fractional=self.config.use_fractional_buy and delta > 0,
        )

        if abs(current_weight_pct - capped_target_pct) <= self.config.rebalance_tolerance_pct:
            plan.skip_reason = "within_tolerance"
            plan.delta_krw = 0
            return plan

        if abs(delta) < self.config.min_order_krw:
            plan.skip_reason = "below_min_order"
            plan.delta_krw = 0
            return plan

        if delta > 0:
            plan.use_fractional = self.config.use_fractional_buy
            if not plan.use_fractional and entry_price_krw:
                plan.limit_price_krw = entry_price_krw
                plan.qty = max(1, int(delta / entry_price_krw))
        else:
            plan.use_fractional = False
            quote = self.bridge.quote_get(symbol)
            price = entry_price_krw or self.bridge.quote_price_krw(quote)
            if price > 0:
                plan.limit_price_krw = price
                plan.qty = min(current_qty, max(1, int(abs(delta) / price)))
            elif current_qty > 0:
                plan.qty = current_qty

        return plan

    def plan_from_signal(
        self,
        action: str,
        symbol: str,
        target_weight_pct: float | None,
        entry_price_krw: float | None = None,
    ) -> OrderPlan | None:
        if action == "stop_update":
            return None
        if action in {"hold", "noise"}:
            return None
        if target_weight_pct is None and action == "sell":
            target_weight_pct = 0.0
        if target_weight_pct is None:
            return None
        return self.build_plan(symbol, target_weight_pct, entry_price_krw)
