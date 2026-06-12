"""Alpaca-specific safety tests."""

from src.config import AppConfig
from src.db import StateDB
from src.safety import SafetyGuard


def test_alpaca_kill_switch_off_when_live_enabled(tmp_path):
    cfg = AppConfig(
        broker="alpaca",
        dry_run=False,
        live_trading_enabled=True,
        data_dir=tmp_path / "data",
    )
    db = StateDB(tmp_path / "state.db")
    guard = SafetyGuard(cfg, db)
    assert not guard.kill_switch_active()
    assert not guard.is_dry_run()


def test_alpaca_dry_run_when_live_disabled(tmp_path):
    cfg = AppConfig(
        broker="alpaca",
        dry_run=False,
        live_trading_enabled=False,
        data_dir=tmp_path / "data",
    )
    db = StateDB(tmp_path / "state.db")
    guard = SafetyGuard(cfg, db)
    assert guard.kill_switch_active()
    assert guard.is_dry_run()
