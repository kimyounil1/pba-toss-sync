"""Auto-discover 2x long daily ETFs for an underlying ticker."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import AppConfig
from src.leverage_types import LeverageMapping

logger = logging.getLogger(__name__)

_EXCLUDE_NAME = (
    "SHORT",
    "INVERSE",
    "BEAR",
    "ULTRA-SHORT",
    "PSHORT",
    "-1X",
    " -1 ",
    "2X SHORT",
    "3X SHORT",
    "SEMIANNUAL",
    "QUARTERLY",
)
_LEVER_MARKERS = ("2X", "2 X", "200%", "TWO TIMES", "1.5X", "150%", "3X", "300%")
_LONG_MARKERS = ("LONG", "BULL", "LEVERAGED", "LEVERAGE")


@dataclass(frozen=True)
class DiscoveredCandidate:
    symbol: str
    multiplier: float
    score: float
    name: str
    source: str  # alpaca_assets | pattern


def parse_multiplier_from_name(name: str) -> float:
    upper = name.upper()
    if "3X" in upper or "300%" in upper:
        return 3.0
    if "1.5X" in upper or "150%" in upper:
        return 1.5
    return 2.0


def _underlying_in_text(text: str, underlying: str) -> bool:
    """Match underlying ticker as a token in ETF name (avoid single-letter noise)."""
    upper = text.upper()
    sym = underlying.upper()
    if len(sym) <= 1:
        return sym in upper.split()
    return bool(re.search(rf"(?:\b|\(){re.escape(sym)}(?:\b|\)|\s)", upper))


def score_leveraged_asset(asset: dict[str, Any], underlying: str) -> float:
    sym = str(asset.get("symbol") or "").upper()
    name = str(asset.get("name") or "")
    upper = name.upper()
    u = underlying.upper()

    if not sym or sym == u or not asset.get("tradable", True):
        return 0.0
    if not _underlying_in_text(upper, u):
        return 0.0
    if any(marker in upper for marker in _EXCLUDE_NAME):
        return 0.0
    if not any(marker in upper for marker in _LEVER_MARKERS):
        return 0.0

    score = 0.0
    if any(marker in upper for marker in _LONG_MARKERS):
        score += 5.0
    if "DAILY" in upper:
        score += 4.0
    if "ETF" in upper:
        score += 2.0
    if "2X" in upper or "200%" in upper:
        score += 6.0
    elif "1.5X" in upper or "150%" in upper:
        score += 4.0
    elif "3X" in upper:
        score += 3.0
    if "SINGLE" in upper and "STOCK" in upper:
        score += 3.0
    if u in sym:
        score += 1.0
    return score


def pattern_candidates(underlying: str) -> list[str]:
    """Heuristic ticker guesses when asset metadata is unavailable."""
    u = underlying.upper()
    templates = (
        "{u}L",
        "{u}U",
        "{u}X",
        "{u}XX",
        "{u}2X",
        "{u}DL",
        "{u}DS",
        "{u}G",
        "{u}B",
        "{u}T",
    )
    seen: set[str] = set()
    out: list[str] = []
    for tmpl in templates:
        sym = tmpl.format(u=u)
        if sym not in seen and sym != u:
            seen.add(sym)
            out.append(sym)
    return out


class AssetCatalog:
    """Cached Alpaca /v2/assets list for ETF name search."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.cache_path = config.data_dir / "leverage_asset_cache.json"
        self._assets: list[dict[str, Any]] | None = None
        self._loaded_at: float = 0.0

    def _cache_ttl_sec(self) -> int:
        return max(3600, int(self.config.leverage_discovery_cache_hours * 3600))

    def _load_disk_cache(self) -> list[dict[str, Any]] | None:
        if not self.cache_path.is_file():
            return None
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            fetched_at = float(payload.get("fetched_at", 0))
            if time.time() - fetched_at > self._cache_ttl_sec():
                return None
            assets = payload.get("assets")
            if isinstance(assets, list):
                return assets
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return None

    def _save_disk_cache(self, assets: list[dict[str, Any]]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(
                {
                    "fetched_at": time.time(),
                    "count": len(assets),
                    "assets": assets,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _fetch_alpaca_assets(self) -> list[dict[str, Any]]:
        if not self.config.alpaca_api_key or not self.config.alpaca_secret_key:
            return []
        from src.alpaca_bridge import AlpacaBridge

        bridge = AlpacaBridge(
            api_key=self.config.alpaca_api_key,
            secret_key=self.config.alpaca_secret_key,
            paper=self.config.alpaca_paper,
            base_url=self.config.alpaca_base_url,
            data_url=self.config.alpaca_data_url,
        )
        return bridge.list_us_equity_assets()

    def get_assets(self) -> list[dict[str, Any]]:
        if self._assets is not None and time.time() - self._loaded_at < self._cache_ttl_sec():
            return self._assets
        cached = self._load_disk_cache()
        if cached is not None:
            self._assets = cached
            self._loaded_at = time.time()
            return cached
        assets = self._fetch_alpaca_assets()
        if assets:
            self._save_disk_cache(assets)
        self._assets = assets
        self._loaded_at = time.time()
        return assets


class DiscoveredMappingStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            mappings = payload.get("mappings") or {}
            return {k.upper(): v for k, v in mappings.items() if isinstance(v, dict)}
        except (json.JSONDecodeError, TypeError):
            return {}

    def save(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "mappings": self._data,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def get(self, underlying: str) -> LeverageMapping | None:
        row = self._data.get(underlying.upper())
        if not row:
            return None
        tickers = tuple(str(t).upper() for t in (row.get("tickers") or []))
        if not tickers:
            return None
        return LeverageMapping(tickers=tickers, multiplier=float(row.get("multiplier", 2)))

    def set(self, underlying: str, candidates: list[DiscoveredCandidate]) -> LeverageMapping | None:
        if not candidates:
            return None
        tickers = tuple(c.symbol for c in candidates)
        multiplier = candidates[0].multiplier
        self._data[underlying.upper()] = {
            "tickers": list(tickers),
            "multiplier": multiplier,
            "source": candidates[0].source,
            "names": {c.symbol: c.name for c in candidates},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        self.save()
        return LeverageMapping(tickers=tickers, multiplier=multiplier)


class LeverageDiscoverer:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.catalog = AssetCatalog(config)
        discovered_path = config.data_dir / "leverage_discovered.json"
        self.discovered = DiscoveredMappingStore(discovered_path)

    def search_assets(self, underlying: str) -> list[DiscoveredCandidate]:
        underlying = underlying.upper()
        assets = self.catalog.get_assets()
        scored: list[DiscoveredCandidate] = []
        for asset in assets:
            score = score_leveraged_asset(asset, underlying)
            if score <= 0:
                continue
            name = str(asset.get("name") or "")
            scored.append(
                DiscoveredCandidate(
                    symbol=str(asset.get("symbol") or "").upper(),
                    multiplier=parse_multiplier_from_name(name),
                    score=score,
                    name=name,
                    source="alpaca_assets",
                )
            )
        scored.sort(key=lambda c: (-c.score, c.symbol))
        return scored

    def search_patterns(self, underlying: str, bridge: Any) -> list[DiscoveredCandidate]:
        found: list[DiscoveredCandidate] = []
        for sym in pattern_candidates(underlying):
            try:
                quote = bridge.quote_get(sym)
                price = float(bridge.quote_price_krw(quote))
            except Exception:
                continue
            if price <= 0:
                continue
            found.append(
                DiscoveredCandidate(
                    symbol=sym,
                    multiplier=2.0,
                    score=1.0,
                    name=f"pattern guess ({sym})",
                    source="pattern",
                )
            )
        return found

    def discover(
        self,
        underlying: str,
        bridge: Any,
        *,
        persist: bool = True,
    ) -> tuple[LeverageMapping | None, list[DiscoveredCandidate]]:
        """Return mapping + ranked candidates (metadata + quote-verified)."""
        underlying = underlying.upper()
        asset_hits = self.search_assets(underlying)
        pattern_hits = self.search_patterns(underlying, bridge)

        merged: dict[str, DiscoveredCandidate] = {}
        for hit in asset_hits + pattern_hits:
            if hit.symbol not in merged or hit.score > merged[hit.symbol].score:
                merged[hit.symbol] = hit

        ranked = sorted(merged.values(), key=lambda c: (-c.score, c.symbol))
        tradable: list[DiscoveredCandidate] = []
        for cand in ranked:
            try:
                quote = bridge.quote_get(cand.symbol)
                price = float(bridge.quote_price_krw(quote))
            except Exception:
                continue
            if price > 0:
                tradable.append(cand)

        if not tradable:
            logger.info("Auto-discover: no tradable 2x ETF for %s", underlying)
            return None, ranked

        mapping = LeverageMapping(
            tickers=tuple(c.symbol for c in tradable),
            multiplier=tradable[0].multiplier,
        )
        if persist:
            self.discovered.set(underlying, tradable)
            logger.info(
                "Auto-discover %s → %s (x%.1f, source=%s)",
                underlying,
                mapping.tickers[0],
                mapping.multiplier,
                tradable[0].source,
            )
        return mapping, ranked
