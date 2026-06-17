"""Resolve PBA underlying tickers to 2x leveraged ETFs and adjust stop prices."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from src.config import AppConfig
from src.leverage_discovery import LeverageDiscoverer
from src.leverage_types import LeverageChoice, LeverageMapping
from src.symbol_aliases import SymbolAliasStore

logger = logging.getLogger(__name__)


def load_leverage_map(path: Path) -> dict[str, LeverageMapping]:
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mappings: dict[str, LeverageMapping] = {}
    for sym, spec in (raw.get("mappings") or {}).items():
        if not isinstance(spec, dict):
            continue
        tickers = tuple(str(t).upper() for t in (spec.get("tickers") or []))
        if not tickers:
            continue
        mappings[sym.upper()] = LeverageMapping(
            tickers=tickers,
            multiplier=float(spec.get("multiplier", 2)),
            source="leverage_map",
        )
    return mappings


def underlying_drop_pct(
    *,
    underlying: str,
    stop_price: float,
    entry_price: float | None,
    bridge: Any,
) -> float | None:
    """PBA 원주 기준 스탑까지 하락 % (양수)."""
    if stop_price <= 0:
        return None
    if entry_price and entry_price > stop_price:
        return (entry_price - stop_price) / entry_price
    quote = bridge.quote_get(underlying)
    ref = float(bridge.quote_price_krw(quote))
    if ref > stop_price:
        return (ref - stop_price) / ref
    return None


def compute_lever_stop(
    bridge: Any,
    traded: str,
    drop_pct: float,
    multiplier: float,
) -> float | None:
    """2x ETF 체결가(ask) 기준 조정 스탑."""
    if drop_pct <= 0:
        return None
    quote = bridge.quote_get(traded)
    fill = float(bridge.quote_price_krw(quote) or quote.get("ask") or quote.get("current_price") or 0)
    if fill <= 0:
        return None
    lever_drop = drop_pct * multiplier
    if lever_drop >= 1.0:
        return None
    return fill * (1.0 - lever_drop)


class LeverageResolver:
    def __init__(self, config: AppConfig, aliases: SymbolAliasStore) -> None:
        self.config = config
        self.aliases = aliases
        map_path = Path(config.leverage_map_path)
        if not map_path.is_absolute():
            from src.config import ROOT

            map_path = ROOT / map_path
        self._map = load_leverage_map(map_path)
        self._discoverer = LeverageDiscoverer(config)

    def trade_symbol_for(self, underlying: str) -> str:
        alias = self.aliases.traded_for(underlying)
        return alias[0] if alias else underlying.upper()

    def multiplier_for(self, underlying: str) -> float:
        alias = self.aliases.traded_for(underlying)
        if alias:
            return alias[1]
        underlying = underlying.upper()
        static = self._map.get(underlying)
        if static:
            return static.multiplier
        cached = self._discoverer.discovered.get(underlying)
        if cached:
            return cached.multiplier
        return 2.0

    def is_tradable(self, bridge: Any, symbol: str) -> bool:
        try:
            quote = bridge.quote_get(symbol)
            price = float(bridge.quote_price_krw(quote))
            return price > 0
        except Exception as exc:
            logger.debug("Quote check failed for %s: %s", symbol, exc)
            return False

    def _mapping_for(self, underlying: str, bridge: Any | None) -> LeverageMapping | None:
        underlying = underlying.upper()
        static = self._map.get(underlying)
        if static:
            return static
        cached = self._discoverer.discovered.get(underlying)
        if cached:
            return LeverageMapping(
                tickers=cached.tickers,
                multiplier=cached.multiplier,
                source="discovered_cache",
            )
        if bridge is not None and self.config.leverage_auto_discover:
            discovered, _ = self._discoverer.discover(underlying, bridge, persist=True)
            if discovered:
                return LeverageMapping(
                    tickers=discovered.tickers,
                    multiplier=discovered.multiplier,
                    source="auto_discover",
                )
        return None

    def resolve_buy(self, bridge: Any, underlying: str) -> LeverageChoice | None:
        if not self.config.use_2x_leverage:
            return None
        mapping = self._mapping_for(underlying.upper(), bridge)
        if not mapping:
            return None
        for ticker in mapping.tickers:
            if self.is_tradable(bridge, ticker):
                return LeverageChoice(
                    underlying=underlying.upper(),
                    traded=ticker,
                    multiplier=mapping.multiplier,
                    source=mapping.source,
                )
        if self.config.leverage_fallback == "underlying":
            logger.info("No tradable 2x ETF for %s; fallback to underlying", underlying)
        return None

    def adjust_stop(
        self,
        bridge: Any,
        *,
        underlying: str,
        traded: str,
        stop_price: float,
        entry_price: float | None,
        multiplier: float,
    ) -> float | None:
        drop = underlying_drop_pct(
            underlying=underlying,
            stop_price=stop_price,
            entry_price=entry_price,
            bridge=bridge,
        )
        if drop is None:
            return None
        lever_stop = compute_lever_stop(bridge, traded, drop, multiplier)
        if lever_stop is None:
            return None
        logger.info(
            "Lever stop %s: underlying %s stop=%s drop=%.2f%% → %s stop=%.4f (x%.1f)",
            underlying,
            underlying,
            stop_price,
            drop * 100,
            traded,
            lever_stop,
            multiplier,
        )
        return lever_stop

    def discover_only(self, bridge: Any, underlying: str) -> dict[str, Any]:
        """Full discovery report without persisting (for CLI)."""
        underlying = underlying.upper()
        static = self._map.get(underlying)
        cached = self._discoverer.discovered.get(underlying)
        mapping, ranked = self._discoverer.discover(
            underlying, bridge, persist=False
        )
        return {
            "underlying": underlying,
            "static_map": (
                {"tickers": list(static.tickers), "multiplier": static.multiplier}
                if static
                else None
            ),
            "discovered_cache": (
                {"tickers": list(cached.tickers), "multiplier": cached.multiplier}
                if cached
                else None
            ),
            "auto_discover_preview": (
                {"tickers": list(mapping.tickers), "multiplier": mapping.multiplier}
                if mapping
                else None
            ),
            "candidates": [
                {
                    "symbol": c.symbol,
                    "multiplier": c.multiplier,
                    "score": c.score,
                    "name": c.name,
                    "source": c.source,
                    "tradable": self.is_tradable(bridge, c.symbol),
                }
                for c in ranked
            ],
        }

    def check_symbol(self, bridge: Any, underlying: str) -> dict[str, Any]:
        """CLI/status: report 2x availability for one underlying."""
        underlying = underlying.upper()
        static = self._map.get(underlying)
        alias = self.aliases.get(underlying)
        if static:
            mapping: LeverageMapping | None = static
            discovery: dict[str, Any] = {
                "skipped": "static_map_configured",
                "static_map": {
                    "tickers": list(static.tickers),
                    "multiplier": static.multiplier,
                },
            }
        else:
            mapping = self._mapping_for(underlying, bridge)
            discovery = self.discover_only(bridge, underlying)
        result: dict[str, Any] = {
            "underlying": underlying,
            "use_2x_leverage": self.config.use_2x_leverage,
            "leverage_auto_discover": self.config.leverage_auto_discover,
            "active_alias": alias.to_dict() if alias else None,
            "mapping_source": mapping.source if mapping else None,
            "candidates": [],
            "chosen": None,
            "discovery": discovery,
        }
        if not mapping:
            result["error"] = "no_2x_etf_found"
            return result
        for ticker in mapping.tickers:
            tradable = self.is_tradable(bridge, ticker)
            quote = {}
            if tradable:
                try:
                    quote = bridge.quote_get(ticker)
                except Exception:
                    tradable = False
            result["candidates"].append(
                {
                    "ticker": ticker,
                    "tradable": tradable,
                    "price": float(bridge.quote_price_krw(quote)) if tradable else None,
                    "multiplier": mapping.multiplier,
                    "source": mapping.source,
                }
            )
        choice = self.resolve_buy(bridge, underlying) if self.config.use_2x_leverage else None
        if choice:
            result["chosen"] = {
                "traded": choice.traded,
                "multiplier": choice.multiplier,
                "source": choice.source,
            }
        return result
