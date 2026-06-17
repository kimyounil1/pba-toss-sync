"""Persist PBA underlying symbol → actually traded symbol (e.g. SNDK → SNXX)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class SymbolAlias:
    underlying: str
    traded: str
    multiplier: float
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "underlying": self.underlying,
            "traded": self.traded,
            "multiplier": self.multiplier,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SymbolAlias:
        return cls(
            underlying=str(data["underlying"]).upper(),
            traded=str(data["traded"]).upper(),
            multiplier=float(data.get("multiplier", 2)),
            updated_at=str(data.get("updated_at", "")),
        )


class SymbolAliasStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._aliases: dict[str, SymbolAlias] = self._load()

    def _load(self) -> dict[str, SymbolAlias]:
        if not self.path.is_file():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        aliases = data.get("aliases") or {}
        return {
            k.upper(): SymbolAlias.from_dict(v if isinstance(v, dict) else {"underlying": k, **v})
            for k, v in aliases.items()
        }

    def save(self) -> None:
        payload = {
            "aliases": {k: v.to_dict() for k, v in self._aliases.items()},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def set(self, underlying: str, traded: str, multiplier: float) -> None:
        key = underlying.upper()
        self._aliases[key] = SymbolAlias(
            underlying=key,
            traded=traded.upper(),
            multiplier=multiplier,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.save()

    def get(self, underlying: str) -> SymbolAlias | None:
        return self._aliases.get(underlying.upper())

    def traded_for(self, underlying: str) -> tuple[str, float] | None:
        alias = self.get(underlying)
        if not alias:
            return None
        return alias.traded, alias.multiplier

    def underlying_for(self, traded: str) -> str | None:
        traded = traded.upper()
        for alias in self._aliases.values():
            if alias.traded == traded:
                return alias.underlying
        return None

    def all_aliases(self) -> dict[str, SymbolAlias]:
        return dict(self._aliases)

    def remove(self, underlying: str) -> None:
        self._aliases.pop(underlying.upper(), None)
        self.save()
