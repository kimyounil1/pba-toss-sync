from datetime import datetime, timezone

from src.x_browser_monitor import _parse_created_at


def test_parse_created_at_iso_z():
    dt = _parse_created_at("2026-06-04T12:00:00.000Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.year == 2026


def test_parse_created_at_empty():
    assert _parse_created_at("") is None
    assert _parse_created_at("not-a-date") is None
