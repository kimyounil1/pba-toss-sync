"""Shared broker datatypes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class OrderPreview:
    confirm_token: str
    raw: dict[str, Any]
