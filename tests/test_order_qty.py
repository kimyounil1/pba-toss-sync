"""Tests for order quantity helpers."""

import pytest

from src.order_qty import floor_qty, format_alpaca_qty


def test_floor_qty_no_round_up():
    assert floor_qty(0.862124518) == 0.862124


def test_format_alpaca_qty_caps_available():
    assert format_alpaca_qty(0.862125, available=0.862124518) == "0.862124"


def test_format_alpaca_qty_zero_raises():
    with pytest.raises(ValueError):
        format_alpaca_qty(0.0000004)
