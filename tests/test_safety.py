"""Tests for safety guard."""

import json
from pathlib import Path

from src.config import AppConfig
from src.db import StateDB
from src.position_sizer import OrderPlan
from src.safety import SafetyGuard


def test_kill_switch_when_live_disabled(tmp_path: Path):
    config_dir = tmp_path / "tossctl"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"trading": {"allow_live_order_actions": False}}),
        encoding="utf-8",
    )
    cfg = AppConfig(dry_run=False, tossctl_config_dir=str(config_dir), data_dir=tmp_path / "data")
    db = StateDB(tmp_path / "state.db")
    guard = SafetyGuard(cfg, db)
    assert guard.is_dry_run()


def test_daily_buy_limit(tmp_path: Path):
    cfg = AppConfig(daily_buy_limit_krw=100_000, data_dir=tmp_path / "data", dry_run=False)
    config_dir = tmp_path / "tossctl"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"trading": {"allow_live_order_actions": True}}),
        encoding="utf-8",
    )
    cfg.tossctl_config_dir = str(config_dir)
    db = StateDB(tmp_path / "state.db")
    db.add_daily_buy(90_000)
    guard = SafetyGuard(cfg, db)
    plan = OrderPlan(
        symbol="NVDA",
        side="buy",
        delta_krw=20_000,
        target_weight_pct=10,
        current_weight_pct=5,
        target_value_krw=100_000,
        current_value_krw=50_000,
        use_fractional=True,
    )
    assert guard.check_order(plan, "tweet-1") == "daily_buy_limit_exceeded"
