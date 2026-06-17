"""US market session helpers (Toss Open API calendar)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")

UsSession = Literal["day_market", "pre_market", "regular", "after_market", "closed"]

_SESSION_KEYS: tuple[tuple[str, UsSession], ...] = (
    ("dayMarket", "day_market"),
    ("preMarket", "pre_market"),
    ("regularMarket", "regular"),
    ("afterMarket", "after_market"),
)


def allows_market_order(session: UsSession) -> bool:
    """Day market and regular session support market orders on Toss."""
    return session in {"day_market", "regular"}


def allows_amount_order(session: UsSession) -> bool:
    """US orderAmount (notional buy) is regular-session only per Open API."""
    return session == "regular"


def resolve_us_session(calendar: dict[str, Any], *, now: datetime | None = None) -> UsSession:
    """Pick active US session from GET /api/v1/market-calendar/US result."""
    now = now or datetime.now(_KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_KST)
    else:
        now = now.astimezone(_KST)

    for day_key in ("today", "previousBusinessDay", "nextBusinessDay"):
        day = calendar.get(day_key)
        if not isinstance(day, dict):
            continue
        session = _session_for_day(day, now)
        if session != "closed":
            return session
    return "closed"


def _session_for_day(day: dict[str, Any], now: datetime) -> UsSession:
    for api_key, name in _SESSION_KEYS:
        block = day.get(api_key)
        if not isinstance(block, dict):
            continue
        start = _parse_kst(block.get("startTime"))
        end = _parse_kst(block.get("endTime"))
        if start and end and start <= now < end:
            return name
    return "closed"


def _parse_kst(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(_KST)
    except ValueError:
        return None
