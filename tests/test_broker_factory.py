"""Tests for broker factory."""

from src.broker import create_broker
from src.config import AppConfig


def test_create_toss_openapi_bridge():
    cfg = AppConfig(
        broker="toss",
        toss_client_id="cid",
        toss_client_secret="secret",
    )
    bridge = create_broker(cfg)
    assert bridge.__class__.__name__ == "TossOpenApiBridge"


def test_create_tossctl_bridge():
    cfg = AppConfig(broker="tossctl")
    bridge = create_broker(cfg)
    assert bridge.__class__.__name__ == "TossBridge"


def test_create_alpaca_bridge():
    cfg = AppConfig(broker="alpaca", alpaca_api_key="k", alpaca_secret_key="s")
    bridge = create_broker(cfg)
    assert bridge.__class__.__name__ == "AlpacaBridge"
