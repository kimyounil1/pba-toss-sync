"""Tests for 2x ETF auto-discovery."""

from pathlib import Path

import pytest

from src.config import AppConfig
from src.leverage_discovery import (
    DiscoveredMappingStore,
    LeverageDiscoverer,
    parse_multiplier_from_name,
    pattern_candidates,
    score_leveraged_asset,
)


def test_parse_multiplier_from_name() -> None:
    assert parse_multiplier_from_name("Tradr 2X Long SNDK Daily ETF") == 2.0
    assert parse_multiplier_from_name("Direxion Daily TSLA Bull 1.5X Shares") == 1.5
    assert parse_multiplier_from_name("3X Long XYZ ETF") == 3.0


def test_score_leveraged_asset_positive() -> None:
    asset = {
        "symbol": "SNXX",
        "name": "Tradr 2X Long SNDK Daily ETF",
        "tradable": True,
    }
    assert score_leveraged_asset(asset, "SNDK") > 0


def test_score_leveraged_asset_rejects_short() -> None:
    asset = {
        "symbol": "NVDS",
        "name": "2X Short NVDA Daily ETF",
        "tradable": True,
    }
    assert score_leveraged_asset(asset, "NVDA") == 0


def test_score_leveraged_asset_rejects_underlying_itself() -> None:
    asset = {"symbol": "SNDK", "name": "Sandisk Corp", "tradable": True}
    assert score_leveraged_asset(asset, "SNDK") == 0


def test_pattern_candidates_dedupes() -> None:
    cands = pattern_candidates("NVDA")
    assert "NVDA" not in cands
    assert "NVDAL" in cands
    assert len(cands) == len(set(cands))


class FakeBridge:
    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = {k.upper(): v for k, v in prices.items()}

    def quote_get(self, symbol: str) -> dict:
        price = self.prices.get(symbol.upper(), 0)
        return {"ask": price, "current_price": price}

    def quote_price_krw(self, quote: dict) -> float:
        return float(quote.get("current_price") or 0)


def test_discoverer_uses_asset_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = AppConfig(
        data_dir=tmp_path,
        leverage_auto_discover=True,
        alpaca_api_key="",
        alpaca_secret_key="",
    )
    discoverer = LeverageDiscoverer(cfg)

    assets = [
        {
            "symbol": "SNXX",
            "name": "Tradr 2X Long SNDK Daily ETF",
            "tradable": True,
        }
    ]
    monkeypatch.setattr(discoverer.catalog, "get_assets", lambda: assets)

    bridge = FakeBridge({"SNXX": 35.0})
    mapping, ranked = discoverer.discover("SNDK", bridge, persist=True)
    assert mapping is not None
    assert mapping.tickers[0] == "SNXX"
    assert ranked[0].source == "alpaca_assets"
    assert discoverer.discovered.get("SNDK") is not None


def test_discoverer_pattern_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = AppConfig(data_dir=tmp_path, leverage_auto_discover=True)
    discoverer = LeverageDiscoverer(cfg)
    monkeypatch.setattr(discoverer.catalog, "get_assets", lambda: [])

    bridge = FakeBridge({"NBISL": 12.5})
    mapping, ranked = discoverer.discover("NBIS", bridge, persist=False)
    assert mapping is not None
    assert "NBISL" in mapping.tickers


def test_discovered_mapping_store_roundtrip(tmp_path: Path) -> None:
    from src.leverage_discovery import DiscoveredCandidate

    store = DiscoveredMappingStore(tmp_path / "discovered.json")
    store.set(
        "SNDK",
        [
            DiscoveredCandidate(
                symbol="SNXX",
                multiplier=2.0,
                score=10.0,
                name="Tradr 2X Long SNDK Daily ETF",
                source="alpaca_assets",
            )
        ],
    )
    reloaded = DiscoveredMappingStore(tmp_path / "discovered.json")
    mapping = reloaded.get("SNDK")
    assert mapping is not None
    assert mapping.tickers == ("SNXX",)
