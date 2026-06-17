"""Shared types for 2x leverage resolution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LeverageMapping:
    tickers: tuple[str, ...]
    multiplier: float
    source: str = "leverage_map"


@dataclass(frozen=True)
class LeverageChoice:
    underlying: str
    traded: str
    multiplier: float
    source: str = "leverage_map"
