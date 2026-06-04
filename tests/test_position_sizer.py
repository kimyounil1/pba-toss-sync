"""Tests for position sizer."""

from src.config import AppConfig
from src.position_sizer import PositionSizer
from src.toss_bridge import TossBridge


class FakeBridge(TossBridge):
    def __init__(self) -> None:
        super().__init__("/bin/false", "/tmp")

    def account_summary(self):
        return {"total_eval_amount": 10_000_000}

    def portfolio_positions(self):
        return [{"symbol": "NVDA", "eval_amount": 500_000, "qty": 5}]

    def quote_get(self, symbol: str):
        return {"current_price": 100_000}


def test_build_plan_buy_delta():
    cfg = AppConfig(rebalance_tolerance_pct=0.5, min_order_krw=1000, max_position_pct=20)
    sizer = PositionSizer(cfg, FakeBridge())
    plan = sizer.build_plan("NVDA", target_weight_pct=10.0)
    assert plan.side == "buy"
    # 10% of 10M = 1M target, current 500k => buy ~500k
    assert plan.delta_krw == 500_000
    assert plan.should_execute


def test_build_plan_within_tolerance():
    cfg = AppConfig(rebalance_tolerance_pct=6.0, min_order_krw=1000)
    sizer = PositionSizer(cfg, FakeBridge())
    plan = sizer.build_plan("NVDA", target_weight_pct=5.0)
    assert plan.skip_reason == "within_tolerance"
    assert not plan.should_execute


def test_build_plan_sell():
    cfg = AppConfig(rebalance_tolerance_pct=0.5, min_order_krw=1000)
    sizer = PositionSizer(cfg, FakeBridge())
    plan = sizer.build_plan("NVDA", target_weight_pct=3.0)
    assert plan.side == "sell"
    assert plan.delta_krw == 200_000
