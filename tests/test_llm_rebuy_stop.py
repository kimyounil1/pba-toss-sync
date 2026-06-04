"""Rebought + stop price = buy entry + PBA 조건매도 (conditional sell)."""

from src.llm_parser import (
    LLMParser,
    extract_conditional_stop_price,
    parse_entry_with_stop,
)
from src.config import AppConfig


REBUY_TWEET = "Rebought $1721.16, stop yesterday low $1707.99. $SNDK"


def test_extract_conditional_stop_price():
    assert extract_conditional_stop_price(REBUY_TWEET) == 1707.99
    assert extract_conditional_stop_price("Stopped $SNDK") is None
    assert extract_conditional_stop_price("Stop is $1729.90, it's 11% size") == 1729.90


def test_parse_entry_with_stop_rebought():
    signal = parse_entry_with_stop(REBUY_TWEET, {"SNDK": 12.0})
    assert signal is not None
    assert signal.action == "buy"
    assert signal.symbol == "SNDK"
    assert signal.entry_price == 1721.16
    assert signal.stop_price == 1707.99
    assert signal.confidence >= 0.9


def test_parser_rebuy_not_sell():
    parser = LLMParser(AppConfig(llm_cache_only=True))
    signal = parser.parse(REBUY_TWEET, {"SNDK": 12.0})
    assert signal.action == "buy"
    assert signal.stop_price == 1707.99
    assert signal.entry_price == 1721.16
