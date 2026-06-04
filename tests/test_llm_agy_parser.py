"""Tests for Antigravity (agy) LLM parser backend."""

from unittest.mock import patch

from src.config import AppConfig
from src.llm_agy import _extract_response_text, generate_agy_text
from src.llm_parser import LLMParser


def test_extract_response_text_prefers_json():
    raw = "Some agent chatter\n{\"action\":\"noise\",\"confidence\":0.9}"
    assert "action" in _extract_response_text(raw)


def test_parse_uses_agy_when_provider_agy():
    cfg = AppConfig(llm_provider="agy", gemini_api_key="")
    parser = LLMParser(cfg)
    agy_json = (
        '{"action":"buy","symbol":"NVDA","market":"us","target_weight_pct":10,'
        '"entry_price":120,"stop_price":110,"confidence":0.95,'
        '"reasoning":"buy","is_new_position":true}'
    )
    with patch("src.llm_parser.agy_enabled", return_value=True), patch(
        "src.llm_parser.generate_agy_text", return_value=agy_json
    ):
        signal = parser.parse("Bought $NVDA at 120")
    assert signal.action == "buy"
    assert signal.raw.get("llm_provider") == "agy"


def test_generate_agy_text_invokes_subprocess(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    with patch.dict(
        "os.environ",
        {"LLM_AGY_WORKDIR": str(work), "LLM_AGY_BIN": "agy", "LLM_AGY_TIMEOUT": "60"},
        clear=False,
    ), patch("src.llm_agy._run_agy_subprocess") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = '{"action":"noise","confidence":0.99}'
        mock_run.return_value.stderr = ""
        text = generate_agy_text("tweet")
    assert "noise" in text
    mock_run.assert_called_once()
    assert mock_run.call_args[0][1] == work
