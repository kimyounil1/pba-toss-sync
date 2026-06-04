"""Shared X tweet model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Tweet:
    id: str
    text: str
    created_at: str
    author_id: str | None = None
    is_reply: bool = False
