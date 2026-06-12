"""Order quantity helpers (fractional-safe, no round-up)."""

from __future__ import annotations

import math


def floor_qty(qty: float, decimals: int = 6) -> float:
    factor = 10**decimals
    return math.floor(qty * factor) / factor


def format_alpaca_qty(qty: float, available: float | None = None) -> str:
    q = min(qty, available) if available is not None else qty
    q = floor_qty(q)
    if q <= 0:
        raise ValueError("order qty must be positive after floor")
    text = f"{q:.6f}".rstrip("0").rstrip(".")
    return text or "0"
