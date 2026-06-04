"""Tests for PBA state manager."""

from pathlib import Path

from src.llm_parser import TradeSignal
from src.pba_state import PBAStateManager


def test_apply_buy_and_stop(tmp_path: Path):
    mgr = PBAStateManager(tmp_path / "state.json")
    signal = TradeSignal(
        action="buy",
        symbol="NVDA",
        target_weight_pct=10.0,
        stop_price=110.0,
        confidence=0.9,
    )
    target = mgr.apply_signal(signal)
    assert target == 10.0
    assert mgr.get_weights()["NVDA"] == 10.0
    assert mgr.get_stop("NVDA") == 110.0


def test_apply_sell_clears_stop(tmp_path: Path):
    mgr = PBAStateManager(tmp_path / "state.json")
    mgr.apply_signal(
        TradeSignal(action="buy", symbol="TSLA", target_weight_pct=5.0, stop_price=200.0, confidence=0.9)
    )
    mgr.apply_signal(
        TradeSignal(action="sell", symbol="TSLA", target_weight_pct=0.0, confidence=0.9)
    )
    assert mgr.get_weights().get("TSLA") == 0.0
    assert mgr.get_stop("TSLA") is None
