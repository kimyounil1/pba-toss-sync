"""X monitor factory — browser (subscriber posts) or legacy API."""

from __future__ import annotations

from src.config import AppConfig
from src.db import StateDB
from src.x_types import Tweet

__all__ = ["Tweet", "create_x_monitor"]


def create_x_monitor(config: AppConfig, db: StateDB | None = None):
    if config.x_source == "browser":
        from src.x_browser_monitor import XBrowserMonitor

        return XBrowserMonitor(config, db)
    from src.x_api_monitor import XApiMonitor

    return XApiMonitor(config, db)
