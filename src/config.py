"""Load settings from YAML, env, and .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SETTINGS = ROOT / "config" / "settings.yaml"


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


@dataclass
class AppConfig:
    pba_username: str = "PBA"
    include_replies: bool = True
    x_source: str = "browser"  # browser | api
    poll_interval_sec: int = 25
    use_stream: bool = False
    x_session_file: str = field(
        default_factory=lambda: _expand("${HOME}/.config/pba-toss-sync/x-storage-state.json")
    )
    x_headless: bool = True
    x_scroll_rounds: int = 25
    x_scroll_delay_sec: float = 1.5
    x_include_superfollows: bool = True
    llm_provider: str = "agy"  # agy | vllm | gemini | auto
    llm_model: str = "gemini-2.5-flash"
    llm_agy_model: str = ""
    confidence_threshold: float = 0.85
    llm_cache_only: bool = False
    llm_persistent_cache: bool = True
    dry_run: bool = True
    market: str = "us"
    use_fractional_buy: bool = True
    min_order_krw: int = 10_000
    max_position_pct: float = 15.0
    daily_buy_limit_krw: int = 2_000_000
    rebalance_tolerance_pct: float = 1.0
    stop_poll_interval_sec: int = 10
    sell_price_buffer_pct: float = 0.5
    tossctl_bin: str = field(default_factory=lambda: _expand("${HOME}/.local/bin/tossctl"))
    tossctl_config_dir: str = field(default_factory=lambda: _expand("${HOME}/.config/tossctl"))
    telegram_enabled: bool = False
    audit_dir: Path = field(default_factory=lambda: ROOT / "logs" / "audit")
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    x_bearer_token: str = ""
    gemini_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


def load_config(settings_path: Path | None = None) -> AppConfig:
    load_dotenv(ROOT / ".env")
    path = settings_path or DEFAULT_SETTINGS
    raw: dict[str, Any] = {}
    if path.is_file():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    pba = raw.get("pba", {})
    x_cfg = raw.get("x", raw.get("x_api", {}))
    llm = raw.get("llm", {})
    trading = raw.get("trading", {})
    stop = raw.get("stop_monitor", {})
    toss = raw.get("tossctl", {})
    notif = raw.get("notifications", {})
    logging_cfg = raw.get("logging", {})

    dry_run_env = os.getenv("PBA_DRY_RUN", "").strip().lower()
    dry_run = trading.get("dry_run", True)
    if dry_run_env in {"0", "false", "no"}:
        dry_run = False
    elif dry_run_env in {"1", "true", "yes"}:
        dry_run = True

    cfg = AppConfig(
        pba_username=str(pba.get("username", "PBA")).lstrip("@"),
        include_replies=bool(pba.get("include_replies", True)),
        x_source=str(x_cfg.get("source", os.getenv("X_SOURCE", "browser"))).lower(),
        poll_interval_sec=int(x_cfg.get("poll_interval_sec", 25)),
        use_stream=bool(x_cfg.get("use_stream", False)),
        x_session_file=_expand(
            x_cfg.get("session_file", os.getenv("X_SESSION_FILE", "${HOME}/.config/pba-toss-sync/x-storage-state.json"))
        ),
        x_headless=str(x_cfg.get("headless", os.getenv("X_HEADLESS", "true"))).lower()
        in {"1", "true", "yes"},
        x_scroll_rounds=int(x_cfg.get("scroll_rounds", 25)),
        x_scroll_delay_sec=float(x_cfg.get("scroll_delay_sec", 1.5)),
        x_include_superfollows=str(x_cfg.get("include_superfollows", "true")).lower()
        in {"1", "true", "yes"},
        llm_provider=str(llm.get("provider", os.getenv("LLM_PROVIDER", "agy"))).lower(),
        llm_model=str(llm.get("model", os.getenv("LLM_MODEL", "gemini-2.5-flash"))),
        llm_agy_model=str(llm.get("agy_model", os.getenv("LLM_AGY_MODEL", ""))),
        confidence_threshold=float(llm.get("confidence_threshold", 0.85)),
        llm_cache_only=bool(llm.get("cache_only", False)),
        llm_persistent_cache=str(llm.get("persistent_cache", "true")).lower()
        in {"1", "true", "yes"},
        dry_run=dry_run,
        market=str(trading.get("market", "us")),
        use_fractional_buy=bool(trading.get("use_fractional_buy", True)),
        min_order_krw=int(trading.get("min_order_krw", 10_000)),
        max_position_pct=float(trading.get("max_position_pct", 15.0)),
        daily_buy_limit_krw=int(trading.get("daily_buy_limit_krw", 2_000_000)),
        rebalance_tolerance_pct=float(trading.get("rebalance_tolerance_pct", 1.0)),
        stop_poll_interval_sec=int(stop.get("poll_interval_sec", 10)),
        sell_price_buffer_pct=float(stop.get("sell_price_buffer_pct", 0.5)),
        tossctl_bin=_expand(toss.get("binary", os.getenv("TOSSCTL_BIN", "${HOME}/.local/bin/tossctl"))),
        tossctl_config_dir=_expand(
            toss.get("config_dir", os.getenv("TOSSCTL_CONFIG_DIR", "${HOME}/.config/tossctl"))
        ),
        telegram_enabled=bool(notif.get("telegram_enabled", False)),
        audit_dir=ROOT / logging_cfg.get("audit_dir", "logs/audit"),
        data_dir=ROOT / "data",
        x_bearer_token=os.getenv("X_BEARER_TOKEN", ""),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )
    cfg.audit_dir.mkdir(parents=True, exist_ok=True)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
