"""Tests for position sizer."""

import pytest

from src.config import AppConfig
from src.order_qty import floor_qty
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


def test_build_plan_full_sell_uses_position_qty():
    class SmallPosBridge(FakeBridge):
        def portfolio_positions(self):
            return [{"symbol": "DOCN", "eval_amount": 166, "qty": 1.0046}]

        def account_summary(self):
            return {"total_eval_amount": 10_000}

        def quote_get(self, symbol: str):
            return {"bid": 157.0, "ask": 157.1, "current_price": 157.0}

        def session_type(self):
            return "extended"

    cfg = AppConfig(
        broker="alpaca",
        rebalance_tolerance_pct=0.5,
        min_order_krw=10,
        limit_sell_buffer_pct=0.0,
        alpaca_limit_orders_only=False,
    )
    plan = PositionSizer(cfg, SmallPosBridge()).build_plan("DOCN", target_weight_pct=0.0)
    assert plan.side == "sell"
    assert plan.qty == pytest.approx(1.0046)
    assert plan.limit_price_krw == pytest.approx(157.0)


def test_alpaca_regular_session_buy_uses_market_notional():
    class QuoteBridge(FakeBridge):
        def account_summary(self):
            return {"total_eval_amount": 10_000}

        def portfolio_positions(self):
            return []

        def quote_get(self, symbol: str):
            return {"bid": 1700.0, "ask": 1705.0, "current_price": 1702.0}

        def session_type(self):
            return "regular"

    cfg = AppConfig(
        broker="alpaca",
        rebalance_tolerance_pct=0.5,
        min_order_krw=100,
        max_position_pct=20.0,
        alpaca_limit_orders_only=False,
        use_fractional_buy=True,
    )
    plan = PositionSizer(cfg, QuoteBridge()).build_plan(
        "SNDK", target_weight_pct=16.0, entry_price_krw=1706.64
    )
    assert plan.side == "buy"
    assert plan.use_fractional is True
    assert plan.limit_price_krw is None
    assert plan.delta_krw == pytest.approx(1600.0)


def test_alpaca_closed_session_sell_uses_limit():
    class QuoteBridge(FakeBridge):
        def portfolio_positions(self):
            return [{"symbol": "NBIS", "eval_amount": 1000, "qty": 4.5}]

        def account_summary(self):
            return {"total_eval_amount": 10_000}

        def quote_get(self, symbol: str):
            return {"bid": 218.0, "ask": 222.0, "current_price": 220.0}

        def session_type(self):
            return "closed"

    cfg = AppConfig(
        broker="alpaca",
        rebalance_tolerance_pct=0.5,
        min_order_krw=10,
        limit_sell_buffer_pct=0.0,
        alpaca_limit_orders_only=False,
    )
    plan = PositionSizer(cfg, QuoteBridge()).build_plan("NBIS", target_weight_pct=0.0)
    assert plan.side == "sell"
    assert plan.limit_price_krw == pytest.approx(218.0)
    assert plan.qty == pytest.approx(4.5)


def test_alpaca_closed_session_uses_limit_not_market():
    class QuoteBridge(FakeBridge):
        def account_summary(self):
            return {"total_eval_amount": 10_000}

        def portfolio_positions(self):
            return []

        def quote_get(self, symbol: str):
            return {"bid": 218.0, "ask": 222.0, "current_price": 220.0}

        def session_type(self):
            return "closed"

    cfg = AppConfig(
        broker="alpaca",
        rebalance_tolerance_pct=0.5,
        min_order_krw=100,
        max_position_pct=20.0,
        limit_buy_buffer_pct=0.0,
        alpaca_limit_orders_only=False,
    )
    plan = PositionSizer(cfg, QuoteBridge()).build_plan("NBIS", target_weight_pct=10.0)
    assert plan.side == "buy"
    assert plan.limit_price_krw == pytest.approx(222.0)
    assert plan.use_fractional is False
    assert plan.qty is not None and plan.qty > 0


def test_alpaca_buy_uses_live_ask_not_tweet_price():
    class QuoteBridge(FakeBridge):
        def account_summary(self):
            return {"total_eval_amount": 10_000}

        def portfolio_positions(self):
            return []

        def quote_get(self, symbol: str):
            return {"bid": 1700.0, "ask": 1705.0, "current_price": 1702.0}

        def session_type(self):
            return "extended"

    cfg = AppConfig(
        broker="alpaca",
        rebalance_tolerance_pct=0.5,
        min_order_krw=100,
        max_position_pct=20.0,
        limit_buy_buffer_pct=0.0,
        alpaca_limit_orders_only=False,
    )
    plan = PositionSizer(cfg, QuoteBridge()).build_plan(
        "SNDK", target_weight_pct=16.0, entry_price_krw=1706.64
    )
    assert plan.side == "buy"
    assert plan.limit_price_krw == pytest.approx(1705.0)
    assert plan.limit_price_krw != pytest.approx(1706.64)
    assert plan.qty == pytest.approx(floor_qty(1600 / 1705.0))


def test_toss_regular_session_buy_uses_market_notional():
    class TossQuoteBridge(FakeBridge):
        def account_summary(self):
            return {"total_eval_amount": 10_000_000}

        def portfolio_positions(self):
            return []

        def quote_get(self, symbol: str):
            return {"bid": 170_000, "ask": 171_000, "current_price": 170_500}

        def session_type(self):
            return "regular"

    cfg = AppConfig(broker="tossctl", rebalance_tolerance_pct=0.5, min_order_krw=100_000)
    plan = PositionSizer(cfg, TossQuoteBridge()).build_plan(
        "SNDK", target_weight_pct=10.0, entry_price_krw=175_000
    )
    assert plan.side == "buy"
    assert plan.use_fractional is True
    assert plan.limit_price_krw is None


def test_toss_extended_session_buy_uses_live_limit_krw():
    class TossQuoteBridge(FakeBridge):
        def account_summary(self):
            return {"total_eval_amount": 10_000_000}

        def portfolio_positions(self):
            return []

        def quote_get(self, symbol: str):
            return {"bid": 170_000, "ask": 171_000, "current_price": 170_500}

        def session_type(self):
            return "extended"

    cfg = AppConfig(
        broker="tossctl",
        rebalance_tolerance_pct=0.5,
        min_order_krw=100_000,
        limit_buy_buffer_pct=0.0,
    )
    plan = PositionSizer(cfg, TossQuoteBridge()).build_plan(
        "SNDK", target_weight_pct=10.0, entry_price_krw=175_000
    )
    assert plan.limit_price_krw == 171_000
    assert plan.limit_price_krw != 175_000


def test_build_plan_full_sell_bypasses_tolerance():
    class DustBridge(FakeBridge):
        def portfolio_positions(self):
            return [{"symbol": "BB", "eval_amount": 1.6, "qty": 0.18}]

        def account_summary(self):
            return {"total_eval_amount": 10_000}

        def quote_get(self, symbol: str):
            return {"bid": 8.9, "ask": 9.0, "current_price": 8.9}

    cfg = AppConfig(
        broker="alpaca",
        rebalance_tolerance_pct=1.0,
        min_order_krw=100,
        limit_sell_buffer_pct=0.0,
    )
    plan = PositionSizer(cfg, DustBridge()).build_plan("BB", target_weight_pct=0.0)
    assert plan.side == "sell"
    assert plan.qty == pytest.approx(0.18)
    assert plan.limit_price_krw == pytest.approx(8.9)
    assert plan.should_execute
