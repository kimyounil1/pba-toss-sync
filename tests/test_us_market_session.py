"""Tests for US market session helpers."""

from datetime import datetime
from zoneinfo import ZoneInfo

from src.us_market_session import allows_market_order, resolve_us_session

_KST = ZoneInfo("Asia/Seoul")


def test_allows_market_order_day_and_regular():
    assert allows_market_order("day_market") is True
    assert allows_market_order("regular") is True
    assert allows_market_order("pre_market") is False
    assert allows_market_order("after_market") is False


def test_resolve_us_session_day_market():
    calendar = {
        "today": {
            "date": "2026-03-25",
            "dayMarket": {
                "startTime": "2026-03-25T10:00:00+09:00",
                "endTime": "2026-03-25T18:00:00+09:00",
            },
            "preMarket": None,
            "regularMarket": None,
            "afterMarket": None,
        }
    }
    now = datetime(2026, 3, 25, 12, 0, tzinfo=_KST)
    assert resolve_us_session(calendar, now=now) == "day_market"


def test_resolve_us_session_regular():
    calendar = {
        "today": {
            "date": "2026-03-25",
            "dayMarket": None,
            "preMarket": None,
            "regularMarket": {
                "startTime": "2026-03-25T23:30:00+09:00",
                "endTime": "2026-03-26T06:00:00+09:00",
            },
            "afterMarket": None,
        }
    }
    now = datetime(2026, 3, 26, 1, 0, tzinfo=_KST)
    assert resolve_us_session(calendar, now=now) == "regular"
