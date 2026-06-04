"""Persistent LLM parse cache by tweet_id."""

from src.config import AppConfig
from src.db import StateDB
from src.llm_parser import LLMParser, TradeSignal


def test_parse_cache_skips_llm(tmp_path):
    db = StateDB(tmp_path / "cache.db")
    cfg = AppConfig(llm_cache_only=True, llm_persistent_cache=True)
    parser = LLMParser(cfg, parse_db=db)

    tweet_id = "999"
    text = "Bought $NVDA at 120. Stop at 110. Position 10%."
    first = parser.parse(text, {}, tweet_id=tweet_id)
    assert first.action == "buy"
    assert not parser.last_parse_from_cache

    parser2 = LLMParser(cfg, parse_db=db)
    second = parser2.parse(text, {}, tweet_id=tweet_id)
    assert second.action == "buy"
    assert parser2.last_parse_from_cache
    assert second.raw.get("cache_hit")

    db.set_llm_parse_cache(
        tweet_id,
        text,
        TradeSignal(action="sell", symbol="NVDA", confidence=0.99).to_cache_dict(),
    )
    third = LLMParser(cfg, parse_db=db).parse(text, {}, tweet_id=tweet_id)
    assert third.action == "sell"
