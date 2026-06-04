"""Tests for vLLM-backed LLM parser."""

from unittest.mock import patch

from src.config import AppConfig
from src.llm_parser import LLMParser


def test_parse_uses_vllm_when_provider_vllm():
    cfg = AppConfig(llm_provider="vllm", gemini_api_key="")
    parser = LLMParser(cfg)
    vllm_json = (
        '{"action":"buy","symbol":"NVDA","market":"us","target_weight_pct":10,'
        '"entry_price":120,"stop_price":110,"confidence":0.95,'
        '"reasoning":"buy signal","is_new_position":true}'
    )
    with patch("src.llm_parser.vllm_enabled", return_value=True), patch(
        "src.llm_parser.generate_vllm_text", return_value=vllm_json
    ):
        signal = parser.parse("Bought $NVDA at 120")
    assert signal.action == "buy"
    assert signal.symbol == "NVDA"
    assert signal.raw.get("llm_provider") == "vllm"


def test_auto_falls_back_to_vllm_on_gemini_quota():
    cfg = AppConfig(llm_provider="auto", gemini_api_key="fake-key")
    parser = LLMParser(cfg)
    vllm_json = (
        '{"action":"noise","symbol":null,"market":"us","target_weight_pct":null,'
        '"entry_price":null,"stop_price":null,"confidence":0.99,'
        '"reasoning":"not trading","is_new_position":false}'
    )

    class QuotaError(Exception):
        pass

    exc = QuotaError("429 RESOURCE_EXHAUSTED quota exceeded")

    with patch("src.llm_parser.agy_enabled", return_value=False), patch(
        "src.llm_parser.vllm_enabled", return_value=True
    ), patch.object(
        parser, "_get_client"
    ) as mock_client, patch(
        "src.llm_parser.generate_vllm_text", return_value=vllm_json
    ), patch(
        "src.llm_parser.should_fallback_to_vllm", return_value=True
    ):
        mock_client.return_value.models.generate_content.side_effect = exc
        signal = parser.parse("Good morning")
    assert signal.action == "noise"
    assert signal.raw.get("llm_provider") == "vllm"
