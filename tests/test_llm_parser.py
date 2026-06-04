"""Tests for LLM parser (heuristic mode, no API)."""

from src.config import AppConfig
from src.llm_parser import LLMParser


def _parser() -> LLMParser:
    cfg = AppConfig(gemini_api_key="", llm_cache_only=True)
    return LLMParser(cfg)


def test_parse_buy_with_stop():
    p = _parser()
    signal = p.parse("Bought $NVDA at 120. Stop at 110. Position 10%.")
    assert signal.action == "buy"
    assert signal.symbol == "NVDA"
    assert signal.stop_price == 110.0
    assert signal.target_weight_pct == 10.0


def test_parse_reduce_weight():
    p = _parser()
    signal = p.parse("Trimmed $TSLA from 15% to 10%")
    assert signal.action == "reduce"
    assert signal.symbol == "TSLA"
    assert signal.target_weight_pct == 10.0


def test_parse_noise():
    p = _parser()
    signal = p.parse("Good morning everyone!")
    assert signal.action == "noise"
    assert signal.confidence >= 0.85


def test_parse_sell():
    p = _parser()
    signal = p.parse("Sold all $AAPL. Exited completely.")
    assert signal.action == "sell"
    assert signal.symbol == "AAPL"
    assert signal.target_weight_pct == 0.0
