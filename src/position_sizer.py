"""Calculate order deltas from target portfolio weights."""

from __future__ import annotations

from dataclasses import dataclass

from src.config import AppConfig
from src.order_qty import floor_qty


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
    def __init__(self, config: AppConfig, bridge: object) -> None:
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

        if (
            capped_target_pct > 0
            and abs(current_weight_pct - capped_target_pct) <= self.config.rebalance_tolerance_pct
        ):
            plan.skip_reason = "within_tolerance"
            plan.delta_krw = 0
            return plan

        if abs(delta) < self.config.min_order_krw and capped_target_pct > 0:
            plan.skip_reason = "below_min_order"
            plan.delta_krw = 0
            return plan

        if self.config.broker == "alpaca":
            session = self._bridge_session(default="closed")
            if session == "regular" and not self.config.alpaca_limit_orders_only:
                return self._build_alpaca_market_plan(plan, current_qty=current_qty)
            # extended + overnight closed: live quote limit (not market)
            return self._build_alpaca_limit_plan(plan, current_qty=current_qty)

        if self.config.broker == "toss":
            if self._market_orders_allowed():
                return self._build_toss_market_plan(plan, current_qty=current_qty)
            return self._build_toss_limit_plan(plan, current_qty=current_qty)

        if self.config.broker == "tossctl":
            session = self._bridge_session(default="regular")
            if session == "regular":
                return self._build_toss_market_plan(plan, current_qty=current_qty)
            return self._build_toss_limit_plan(plan, current_qty=current_qty)

        if delta > 0:
            plan.use_fractional = self.config.use_fractional_buy
            if not plan.use_fractional and entry_price_krw:
                plan.limit_price_krw = entry_price_krw
                plan.qty = max(1, int(delta / entry_price_krw))
        else:
            quote = self.bridge.quote_get(symbol)
            price = entry_price_krw or self.bridge.quote_price_krw(quote)
            fractional_sell = current_qty < 1
            plan.use_fractional = fractional_sell

            if current_qty <= 0:
                plan.skip_reason = "no_position"
                plan.delta_krw = 0
                return plan

            if capped_target_pct <= 0:
                plan.qty = current_qty
                plan.limit_price_krw = None
            elif price > 0:
                shares_to_sell = min(current_qty, abs(delta) / price)
                plan.qty = shares_to_sell
                plan.limit_price_krw = None if fractional_sell else price
            else:
                plan.qty = current_qty
                plan.limit_price_krw = None

        return plan

    def _bridge_session(self, *, default: str) -> str:
        if hasattr(self.bridge, "session_type"):
            return self.bridge.session_type()
        return default

    def _market_orders_allowed(self) -> bool:
        if hasattr(self.bridge, "allows_market_orders"):
            return bool(self.bridge.allows_market_orders())
        session = self._bridge_session(default="closed")
        if self.config.broker == "alpaca":
            return session == "regular" and not self.config.alpaca_limit_orders_only
        return session == "regular"

    def _build_toss_market_plan(self, plan: OrderPlan, *, current_qty: float) -> OrderPlan:
        """Toss regular session: market (fractional buy / qty sell), live quote sizing."""
        built = self._build_alpaca_market_plan(plan, current_qty=current_qty)
        if built.side == "sell" and built.qty and built.qty < 1:
            built.use_fractional = True
        return built

    def _build_toss_limit_plan(self, plan: OrderPlan, *, current_qty: float) -> OrderPlan:
        """Toss extended session: integer KRW limit from live quote (not tweet entry)."""
        built = self._build_alpaca_limit_plan(plan, current_qty=current_qty)
        if built.limit_price_krw is not None:
            built.limit_price_krw = int(round(built.limit_price_krw))
        return built

    def _build_alpaca_market_plan(self, plan: OrderPlan, *, current_qty: float) -> OrderPlan:
        """Alpaca regular session: market orders sized from live quote (not tweet entry)."""
        quote = self.bridge.quote_get(plan.symbol)

        if plan.side == "buy":
            ref = float(quote.get("ask") or quote.get("current_price") or 0)
            if ref <= 0:
                plan.skip_reason = "no_quote"
                plan.delta_krw = 0
                return plan
            plan.use_fractional = self.config.use_fractional_buy
            plan.limit_price_krw = None
            if plan.use_fractional:
                return plan
            plan.qty = floor_qty(plan.delta_krw / ref)
            if plan.qty <= 0:
                plan.skip_reason = "qty_too_small"
                plan.delta_krw = 0
            return plan

        if current_qty <= 0:
            plan.skip_reason = "no_position"
            plan.delta_krw = 0
            return plan

        ref = float(quote.get("bid") or quote.get("current_price") or 0)
        if ref <= 0:
            plan.skip_reason = "no_quote"
            plan.delta_krw = 0
            return plan

        plan.use_fractional = False
        plan.limit_price_krw = None
        if plan.target_weight_pct <= 0:
            plan.qty = floor_qty(current_qty)
        else:
            plan.qty = floor_qty(min(current_qty, plan.delta_krw / ref))
        if plan.qty <= 0:
            plan.skip_reason = "qty_too_small"
            plan.delta_krw = 0
        return plan

    def _build_alpaca_limit_plan(self, plan: OrderPlan, *, current_qty: float) -> OrderPlan:
        """Alpaca: live quote limit orders (not tweet entry price)."""
        quote = self.bridge.quote_get(plan.symbol)
        plan.use_fractional = False

        if plan.side == "buy":
            ref = float(quote.get("ask") or quote.get("current_price") or 0)
            if ref <= 0:
                plan.skip_reason = "no_quote"
                plan.delta_krw = 0
                return plan
            limit = ref * (1 + self.config.limit_buy_buffer_pct / 100.0)
            plan.limit_price_krw = limit
            plan.qty = floor_qty(plan.delta_krw / limit)
            if plan.qty <= 0:
                plan.skip_reason = "qty_too_small"
                plan.delta_krw = 0
            return plan

        if current_qty <= 0:
            plan.skip_reason = "no_position"
            plan.delta_krw = 0
            return plan

        ref = float(quote.get("bid") or quote.get("current_price") or 0)
        if ref <= 0:
            plan.skip_reason = "no_quote"
            plan.delta_krw = 0
            return plan
        limit = ref * (1 - self.config.limit_sell_buffer_pct / 100.0)

        if plan.target_weight_pct <= 0:
            plan.qty = current_qty
        else:
            plan.qty = min(current_qty, plan.delta_krw / limit)
        plan.qty = floor_qty(plan.qty)
        plan.limit_price_krw = limit
        if plan.qty <= 0:
            plan.skip_reason = "qty_too_small"
            plan.delta_krw = 0
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
        # Live brokers ignore PBA tweet entry; use quote at order time.
        tweet_entry = None if self.config.broker in {"alpaca", "toss", "tossctl"} else entry_price_krw
        return self.build_plan(symbol, target_weight_pct, tweet_entry)
