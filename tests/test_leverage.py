"""Tests for 2x leverage resolution and stop adjustment."""

from pathlib import Path

import pytest

from src.config import AppConfig
from src.leverage import (
    LeverageResolver,
    compute_lever_stop,
    load_leverage_map,
    underlying_drop_pct,
)
from src.symbol_aliases import SymbolAliasStore


class FakeBridge:
    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = {k.upper(): v for k, v in prices.items()}

    def quote_get(self, symbol: str) -> dict:
        price = self.prices[symbol.upper()]
        return {"ask": price, "bid": price, "current_price": price}

    def quote_price_krw(self, quote: dict) -> float:
        return float(quote.get("current_price") or 0)


@pytest.fixture
def leverage_map_path(tmp_path: Path) -> Path:
    path = tmp_path / "leverage_map.yaml"
    path.write_text(
        """
mappings:
  SNDK:
    tickers: [SNXX, SNDG]
    multiplier: 2
  FAKE:
    tickers: [NOPE]
    multiplier: 2
""",
        encoding="utf-8",
    )
    return path


def test_load_leverage_map(leverage_map_path: Path) -> None:
    m = load_leverage_map(leverage_map_path)
    assert m["SNDK"].tickers == ("SNXX", "SNDG")
    assert m["SNDK"].multiplier == 2


def test_underlying_drop_pct_from_entry() -> None:
    bridge = FakeBridge({"SNDK": 1000})
    drop = underlying_drop_pct(
        underlying="SNDK",
        stop_price=950,
        entry_price=1000,
        bridge=bridge,
    )
    assert drop == pytest.approx(0.05)


def test_compute_lever_stop_doubles_drop() -> None:
    bridge = FakeBridge({"SNXX": 80})
    stop = compute_lever_stop(bridge, "SNXX", 0.05, 2.0)
    assert stop == pytest.approx(72.0)


def test_resolve_buy_picks_first_tradable(leverage_map_path: Path, tmp_path: Path) -> None:
    cfg = AppConfig(
        use_2x_leverage=True,
        leverage_map_path=str(leverage_map_path),
    )
    aliases = SymbolAliasStore(tmp_path / "aliases.json")
    resolver = LeverageResolver(cfg, aliases)
    bridge = FakeBridge({"SNXX": 35.0, "SNDG": 0})
    choice = resolver.resolve_buy(bridge, "SNDK")
    assert choice is not None
    assert choice.traded == "SNXX"
    assert choice.multiplier == 2


def test_resolve_buy_fallback_when_no_quote(leverage_map_path: Path, tmp_path: Path) -> None:
    cfg = AppConfig(
        use_2x_leverage=True,
        leverage_map_path=str(leverage_map_path),
    )
    resolver = LeverageResolver(cfg, SymbolAliasStore(tmp_path / "aliases.json"))
    bridge = FakeBridge({"NOPE": 0})
    assert resolver.resolve_buy(bridge, "FAKE") is None


def test_adjust_stop_uses_entry(leverage_map_path: Path, tmp_path: Path) -> None:
    cfg = AppConfig(
        use_2x_leverage=True,
        leverage_map_path=str(leverage_map_path),
    )
    resolver = LeverageResolver(cfg, SymbolAliasStore(tmp_path / "aliases.json"))
    bridge = FakeBridge({"SNDK": 2000, "SNXX": 100})
    stop = resolver.adjust_stop(
        bridge,
        underlying="SNDK",
        traded="SNXX",
        stop_price=1707.99,
        entry_price=1721.16,
        multiplier=2,
    )
    drop = (1721.16 - 1707.99) / 1721.16
    assert stop == pytest.approx(100 * (1 - 2 * drop))


def test_trade_symbol_uses_alias(leverage_map_path: Path, tmp_path: Path) -> None:
    aliases = SymbolAliasStore(tmp_path / "aliases.json")
    aliases.set("SNDK", "SNXX", 2)
    resolver = LeverageResolver(
        AppConfig(leverage_map_path=str(leverage_map_path)),
        aliases,
    )
    assert resolver.trade_symbol_for("SNDK") == "SNXX"


def test_symbol_alias_store_roundtrip(tmp_path: Path) -> None:
    store = SymbolAliasStore(tmp_path / "aliases.json")
    store.set("SNDK", "SNXX", 2)
    reloaded = SymbolAliasStore(tmp_path / "aliases.json")
    assert reloaded.traded_for("SNDK") == ("SNXX", 2.0)
