"""Track inferred PBA portfolio weights from parsed signals."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.llm_parser import TradeSignal


@dataclass
class PBAState:
    weights: dict[str, float] = field(default_factory=dict)
    stops: dict[str, float] = field(default_factory=dict)
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": self.weights,
            "stops": self.stops,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PBAState:
        return cls(
            weights={k.upper(): float(v) for k, v in (data.get("weights") or {}).items()},
            stops={k.upper(): float(v) for k, v in (data.get("stops") or {}).items()},
            updated_at=str(data.get("updated_at", "")),
        )


class PBAStateManager:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load()

    def _load(self) -> PBAState:
        if not self.state_path.is_file():
            return PBAState()
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        return PBAState.from_dict(data)

    def save(self) -> None:
        self.state.updated_at = datetime.now(timezone.utc).isoformat()
        self.state_path.write_text(
            json.dumps(self.state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_weights(self) -> dict[str, float]:
        return dict(self.state.weights)

    def apply_signal(self, signal: TradeSignal) -> float | None:
        """Update PBA state from signal; return resolved target weight %."""
        if signal.action == "portfolio_sync":
            weights = signal.raw.get("portfolio_weights") or {}
            if weights:
                self.state.weights = {
                    str(sym).upper(): float(pct) for sym, pct in weights.items()
                }
            self.save()
            return None

        if not signal.symbol:
            return signal.target_weight_pct

        sym = signal.symbol.upper()
        current = self.state.weights.get(sym, 0.0)
        target = signal.target_weight_pct

        if signal.action == "sell":
            target = 0.0
        elif signal.action in {"buy", "add"} and target is None:
            target = current if current > 0 else 5.0
        elif signal.action == "reduce" and target is None:
            target = max(current * 0.5, 0.0)
        elif signal.action == "stop_update":
            if signal.stop_price is not None:
                self.state.stops[sym] = signal.stop_price
            self.save()
            return self.state.weights.get(sym)

        if target is not None:
            self.state.weights[sym] = target
        if signal.stop_price is not None:
            self.state.stops[sym] = signal.stop_price
        if target == 0.0:
            self.state.stops.pop(sym, None)

        self.save()
        return target

    def get_stop(self, symbol: str) -> float | None:
        return self.state.stops.get(symbol.upper())
