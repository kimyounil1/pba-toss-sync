"""LLM-based trade signal parser for PBA posts.

PBA vocabulary (critical):
- "stop" / "스탑" = 조건매도 가격 (conditional sell trigger). NOT an immediate sell.
  When price falls to/below stop_price, exit the position.
- "Stopped $TICKER" / stopped out = already exited (past tense) => sell now, not a stop order.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any

from google import genai

from src.config import AppConfig
from src.db import StateDB
from src.llm_agy import agy_enabled, generate_agy_text
from src.llm_vllm import (
    generate_vllm_text,
    should_fallback_to_vllm,
    vllm_enabled,
)

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"buy", "sell", "reduce", "add", "stop_update", "hold", "noise", "portfolio_sync"}


@dataclass
class TradeSignal:
    action: str
    symbol: str | None = None
    market: str = "us"
    target_weight_pct: float | None = None
    entry_price: float | None = None
    stop_price: float | None = None  # PBA 조건매도 가격; sell when quote <= this
    confidence: float = 0.0
    reasoning: str = ""
    is_new_position: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        return self.action not in {"hold", "noise"} and self.confidence >= 0.0

    def passes_threshold(self, threshold: float) -> bool:
        return self.action not in {"hold", "noise"} and self.confidence >= threshold

    def to_cache_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "symbol": self.symbol,
            "market": self.market,
            "target_weight_pct": self.target_weight_pct,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "is_new_position": self.is_new_position,
            "raw": self.raw,
        }

    @classmethod
    def from_cache_dict(cls, data: dict[str, Any]) -> TradeSignal:
        raw = dict(data.get("raw") or {})
        raw["cache_hit"] = True
        return cls(
            action=str(data.get("action", "hold")),
            symbol=data.get("symbol"),
            market=str(data.get("market", "us")),
            target_weight_pct=data.get("target_weight_pct"),
            entry_price=data.get("entry_price"),
            stop_price=data.get("stop_price"),
            confidence=float(data.get("confidence", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            is_new_position=bool(data.get("is_new_position", False)),
            raw=raw,
        )


SYSTEM_PROMPT = """You parse stock-trading posts from influencer PBA on X (Twitter).

PBA vocabulary:
- stop_price field = PBA's "stop" / "스탑" = 조건매도 가격 (broker conditional sell level).
  Meaning: if the stock trades at or below this price, sell. It is NOT an immediate sell command.
- "Stopped $TICKER" / "stopped out" = position already closed (past tense) => action sell, stop_price null.

Return ONLY valid JSON matching this schema:
{
  "action": "buy|sell|reduce|add|stop_update|hold|noise",
  "symbol": "TICKER or null",
  "market": "us|kr",
  "target_weight_pct": number or null,
  "entry_price": number or null,
  "stop_price": number or null,
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation",
  "is_new_position": boolean
}

Rules:
- "bought/매수했다/rebought/re-entered" => buy (follow immediately)
- "Rebought $1721.16, stop yesterday low $1707.99" => buy, entry_price=1721.16, stop_price=1707.99
- Any "stop $PRICE" / "stop is $PRICE" on a buy post => stop_price=PRICE (조건매도), still action buy
- "buy at/매수해라" => buy
- "stop 110/스탑 110" / "raising stop to $X" with NO new buy => stop_update (set 조건매도 to X)
- "raising stop to" / "stop flat" (breakeven) => stop_update (NOT sell)
- "Stopped $TICKER" / "stopped out" (past tense, position exited) => sell, target_weight_pct=0
- "if $TICKER stops me" / "if stopped" / "in trouble lol" (hypothetical) => hold or noise, NOT sell
- "weight 15% -> 10%/비중 15%에서 10%" => reduce, target_weight_pct=10
- "trimmed/reduced/Sold 1/2" => reduce (infer target weight from context or prior %)
- "sold/exited/전량 매도" (explicit exit, not stop-out slang) => sell
- "added/비중 올렸다" => add
- "Longs by Cost/Size" block listing $TICKER ... N% => action=portfolio_sync, put map in raw.portfolio_weights
- Cashtags like $NVDA => symbol NVDA
- If not a trading post => action=noise, confidence high
- If ambiguous => action=hold, confidence below 0.85
"""

FEW_SHOT = [
    {
        "tweet": "Bought $NVDA at 120. Stop at 110. Position 10%.",
        "output": {
            "action": "buy",
            "symbol": "NVDA",
            "market": "us",
            "target_weight_pct": 10.0,
            "entry_price": 120.0,
            "stop_price": 110.0,
            "confidence": 0.95,
            "reasoning": "New buy with stop and 10% weight",
            "is_new_position": True,
        },
    },
    {
        "tweet": "Trimmed $TSLA from 15% to 10%",
        "output": {
            "action": "reduce",
            "symbol": "TSLA",
            "market": "us",
            "target_weight_pct": 10.0,
            "entry_price": None,
            "stop_price": None,
            "confidence": 0.93,
            "reasoning": "Weight reduced to 10%",
            "is_new_position": False,
        },
    },
    {
        "tweet": "Rebought $1721.16, stop yesterday low $1707.99. $SNDK",
        "output": {
            "action": "buy",
            "symbol": "SNDK",
            "market": "us",
            "target_weight_pct": 12.0,
            "entry_price": 1721.16,
            "stop_price": 1707.99,
            "confidence": 0.97,
            "reasoning": "Re-entry buy; stop 1707.99 is PBA conditional sell price (조건매도)",
            "is_new_position": True,
        },
    },
    {
        "tweet": "Good morning everyone! Hope you have a great day.",
        "output": {
            "action": "noise",
            "symbol": None,
            "market": "us",
            "target_weight_pct": None,
            "entry_price": None,
            "stop_price": None,
            "confidence": 0.99,
            "reasoning": "Not a trading post",
            "is_new_position": False,
        },
    },
]


class LLMParser:
    def __init__(self, config: AppConfig, parse_db: StateDB | None = None) -> None:
        self.config = config
        self._parse_db = parse_db
        self._client: genai.Client | None = None
        self._cache: dict[str, TradeSignal] = {}
        self.last_parse_from_cache = False

    def _get_client(self) -> genai.Client:
        if self._client is None:
            if not self.config.gemini_api_key:
                raise ValueError("GEMINI_API_KEY required for LLM parsing")
            self._client = genai.Client(api_key=self.config.gemini_api_key)
        return self._client

    def _build_prompt(self, tweet_text: str, pba_state: dict[str, float]) -> str:
        examples = "\n\n".join(
            f"Tweet: {ex['tweet']}\nJSON: {json.dumps(ex['output'])}"
            for ex in FEW_SHOT
        )
        state_str = json.dumps(pba_state) if pba_state else "{}"
        return (
            f"{SYSTEM_PROMPT}\n\nExamples:\n{examples}\n\n"
            f"Current PBA portfolio state (symbol -> weight%%): {state_str}\n\n"
            f"Tweet to parse:\n{tweet_text}\n\nJSON:"
        )

    def parse(
        self,
        tweet_text: str,
        pba_state: dict[str, float] | None = None,
        *,
        tweet_id: str | None = None,
    ) -> TradeSignal:
        self.last_parse_from_cache = False
        cache_key = tweet_text.strip()
        if cache_key in self._cache:
            self.last_parse_from_cache = True
            return self._cache[cache_key]

        if (
            tweet_id
            and self._parse_db is not None
            and self.config.llm_persistent_cache
        ):
            stored = self._parse_db.get_llm_parse_cache(tweet_id)
            if stored and stored["text"] == cache_key:
                signal = TradeSignal.from_cache_dict(stored["signal"])
                self._cache[cache_key] = signal
                self.last_parse_from_cache = True
                return signal

        rebuy = parse_entry_with_stop(tweet_text, pba_state or {})
        if rebuy:
            signal = refine_trade_signal(tweet_text, rebuy)
            self._cache[cache_key] = signal
            self._persist_parse_cache(tweet_id, cache_key, signal)
            return signal

        portfolio = extract_portfolio_weights(tweet_text)
        if portfolio:
            signal = TradeSignal(
                action="portfolio_sync",
                confidence=0.99,
                reasoning="portfolio snapshot from Longs by Cost/Size",
                raw={"portfolio_weights": portfolio, "llm_provider": "rules"},
            )
            self._cache[cache_key] = signal
            self._persist_parse_cache(tweet_id, cache_key, signal)
            return signal

        if self.config.llm_cache_only:
            signal = self._parse_heuristic(tweet_text, pba_state or {})
        else:
            try:
                signal = self._parse_llm(tweet_text, pba_state or {})
            except Exception as exc:
                logger.warning("LLM parse failed, falling back to heuristic: %s", exc)
                signal = self._parse_heuristic(tweet_text, pba_state or {})

        signal = refine_trade_signal(tweet_text, signal)
        self._cache[cache_key] = signal
        self._persist_parse_cache(tweet_id, cache_key, signal)
        return signal

    def _persist_parse_cache(
        self, tweet_id: str | None, cache_key: str, signal: TradeSignal
    ) -> None:
        if not tweet_id or self._parse_db is None or not self.config.llm_persistent_cache:
            return
        self._parse_db.set_llm_parse_cache(tweet_id, cache_key, signal.to_cache_dict())

    def _generate_text(self, prompt: str) -> tuple[str, str]:
        """Return (text, provider) where provider is agy | gemini | vllm."""
        provider = self.config.llm_provider
        gemini_error: Exception | None = None
        agy_error: Exception | None = None

        def _call_agy() -> str:
            return generate_agy_text(prompt, model=self.config.llm_agy_model or None)

        def _call_vllm() -> str:
            return generate_vllm_text(prompt)

        def _call_gemini() -> str:
            client = self._get_client()
            response = client.models.generate_content(
                model=self.config.llm_model,
                contents=prompt,
            )
            text = (response.text or "").strip()
            if not text:
                raise ValueError("empty Gemini response")
            return text

        if provider == "agy":
            if not agy_enabled():
                raise ValueError("LLM provider=agy but LLM_AGY_ENABLED=0 or agy binary missing")
            return _call_agy(), "agy"

        if provider == "vllm":
            if not vllm_enabled():
                raise ValueError("LLM provider=vllm but LLM_VLLM_ENABLED/BASE_URL not set")
            return _call_vllm(), "vllm"

        if provider == "gemini":
            if not self.config.gemini_api_key:
                raise ValueError("LLM provider=gemini but GEMINI_API_KEY not set")
            return _call_gemini(), "gemini"

        # auto: local agy first, then Gemini API; vLLM only if explicitly enabled
        if agy_enabled():
            try:
                return _call_agy(), "agy"
            except Exception as exc:
                agy_error = exc
                logger.warning("AGY failed, trying fallbacks: %s", exc)

        if self.config.gemini_api_key:
            try:
                return _call_gemini(), "gemini"
            except Exception as exc:
                gemini_error = exc
                if not should_fallback_to_vllm(exc):
                    raise

        if vllm_enabled():
            try:
                return _call_vllm(), "vllm"
            except Exception as vllm_exc:
                parts = [f"agy ({agy_error})", f"gemini ({gemini_error})", f"vllm ({vllm_exc})"]
                raise RuntimeError("All LLM backends failed: " + "; ".join(parts)) from vllm_exc

        if agy_error and gemini_error:
            raise RuntimeError(f"agy failed ({agy_error}); gemini failed ({gemini_error})")
        if agy_error:
            raise agy_error
        if gemini_error:
            raise gemini_error
        raise ValueError(
            "No LLM backend (set LLM_AGY_ENABLED=1, GEMINI_API_KEY, or LLM_VLLM_ENABLED=1)"
        )

    def _parse_llm(self, tweet_text: str, pba_state: dict[str, float]) -> TradeSignal:
        prompt = self._build_prompt(tweet_text, pba_state)
        text, provider = self._generate_text(prompt)
        data = self._extract_json(text)
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict):
            raise ValueError(f"LLM JSON must be object, got {type(data).__name__}")
        signal = self._from_dict(data)
        signal.reasoning = f"[{provider}] {signal.reasoning}".strip()
        signal.raw = {**signal.raw, "llm_provider": provider}
        return signal

    def _extract_json(self, text: str) -> dict[str, Any]:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                return json.loads(match.group())
            raise

    def _from_dict(self, data: dict[str, Any]) -> TradeSignal:
        action = str(data.get("action", "hold")).lower()
        if action not in VALID_ACTIONS:
            action = "hold"
        symbol = data.get("symbol")
        if symbol:
            symbol = str(symbol).upper().lstrip("$")
        return TradeSignal(
            action=action,
            symbol=symbol,
            market=str(data.get("market", "us")).lower(),
            target_weight_pct=(
                float(data["target_weight_pct"])
                if data.get("target_weight_pct") is not None
                else None
            ),
            entry_price=float(data["entry_price"]) if data.get("entry_price") is not None else None,
            stop_price=float(data["stop_price"]) if data.get("stop_price") is not None else None,
            confidence=float(data.get("confidence", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            is_new_position=bool(data.get("is_new_position", False)),
            raw=data,
        )

    def _parse_heuristic(self, tweet_text: str, pba_state: dict[str, float]) -> TradeSignal:
        portfolio = extract_portfolio_weights(tweet_text)
        if portfolio:
            return TradeSignal(
                action="portfolio_sync",
                confidence=0.99,
                reasoning="heuristic portfolio snapshot",
                raw={"portfolio_weights": portfolio, "heuristic": True},
            )
        text = tweet_text.upper()
        symbols = re.findall(r"\$([A-Z]{1,5})\b", tweet_text)
        symbol = symbols[0] if symbols else None

        action = "noise"
        confidence = 0.5
        target_weight: float | None = None
        stop_price: float | None = None
        entry_price: float | None = None

        weight_match = re.search(
            r"(\d+(?:\.\d+)?)\s*%\s*(?:TO|→|->|에서)\s*(\d+(?:\.\d+)?)\s*%", tweet_text, re.I
        )
        if weight_match:
            target_weight = float(weight_match.group(2))
            action = "reduce"
            confidence = 0.8
        else:
            pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", tweet_text)
            if pct_match:
                target_weight = float(pct_match.group(1))

        if re.search(r"\b(RE-?BOUGHT|RE-?ENTER(?:ED)?|BOUGHT|매수|ENTERED|BUY AT)\b", text):
            action = "buy"
            confidence = 0.75
            rebuy_signal = parse_entry_with_stop(tweet_text, pba_state)
            if rebuy_signal:
                return rebuy_signal
        elif re.search(r"\bSOLD\s+1/2\b", text):
            action = "reduce"
            if symbol and symbol in pba_state:
                target_weight = max(pba_state[symbol] * 0.5, 0.0)
            confidence = 0.8
        elif re.search(r"\bSTOPPED\s+\$?[A-Z]{1,5}\b", text) and not re.search(
            r"\bIF\b", text
        ):
            action = "sell"
            target_weight = 0.0
            confidence = 0.85
        elif re.search(r"\bSTOP\s+FLAT\b", text):
            action = "stop_update"
            confidence = 0.8
        elif re.search(r"\b(SOLD|EXIT|전량|매도)\b", text) and "STOP" not in text:
            action = "sell"
            target_weight = 0.0
            confidence = 0.75
        elif re.search(r"\b(TRIM|REDUC|비중.*내|내렸)\b", text):
            action = "reduce"
            confidence = 0.7
        elif re.search(r"\b(RAISING\s+STOP|STOP\s+IS|스탑)\b", text, re.I):
            action = "stop_update"
            confidence = 0.7

        stop_price = extract_conditional_stop_price(tweet_text)

        price_match = re.search(r"(?:AT|@)\s*(\d+(?:\.\d+)?)", tweet_text, re.I)
        if price_match and action == "buy":
            entry_price = float(price_match.group(1))

        if not symbol and not re.search(r"\b(BOUGHT|SOLD|STOP|매수|매도|스탑|비중)\b", text):
            action = "noise"
            confidence = 0.9

        return TradeSignal(
            action=action,
            symbol=symbol,
            target_weight_pct=target_weight,
            entry_price=entry_price,
            stop_price=stop_price,
            confidence=confidence,
            reasoning="heuristic parse",
            is_new_position=action == "buy",
            raw={"heuristic": True},
        )


def extract_conditional_stop_price(tweet_text: str) -> float | None:
    """PBA 'stop' = 조건매도 가격. Returns null for past-tense stop-out tweets."""
    upper = tweet_text.upper()
    if re.search(r"\bSTOPPED\b", upper) and not re.search(
        r"\b(?:RE-?BOUGHT|BOUGHT|매수)\b", upper
    ):
        return None
    patterns = (
        r"\bstop\b[^$\d\n]{0,80}\$\s*(\d+(?:\.\d+)?)",
        r"\bstop\s+(?:is|at|@)\s*\$?\s*(\d+(?:\.\d+)?)",
        r"\braising\s+stop\s+to\s+\$?\s*(\d+(?:\.\d+)?)",
        r"\b(?:스탑)\s*(?:@|:)?\s*(\d+(?:\.\d+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, tweet_text, re.I | re.DOTALL)
        if match:
            return float(match.group(1))
    return None


def _extract_entry_price(tweet_text: str, symbol: str | None) -> float | None:
    bought = re.search(
        r"\b(?:re-?bought|re-?enter(?:ed)?|bought|매수|entered)\s+\$?\s*(\d+(?:\.\d+)?)",
        tweet_text,
        re.I,
    )
    if bought:
        return float(bought.group(1))
    at_price = re.search(r"\bat\s+\$?\s*(\d+(?:\.\d+)?)", tweet_text, re.I)
    if at_price:
        return float(at_price.group(1))
    if symbol:
        ticker_price = re.search(
            rf"\${symbol}\s+\$?\s*(\d+(?:\.\d+)?)",
            tweet_text,
            re.I,
        )
        if ticker_price:
            return float(ticker_price.group(1))
    return None


def parse_entry_with_stop(tweet_text: str, pba_state: dict[str, float]) -> TradeSignal | None:
    """
    Buy/rebuy post with PBA stop = 조건매도 가격 (register for software monitor, not sell now).
    """
    if not re.search(
        r"\b(?:re-?bought|re-?enter(?:ed)?|bought|매수|entered)\b", tweet_text, re.I
    ):
        return None
    if re.search(r"\bif\b", tweet_text, re.I):
        return None
    if re.match(r"^\s*stopped\b", tweet_text.strip(), re.I):
        return None

    stop_price = extract_conditional_stop_price(tweet_text)
    if stop_price is None:
        return None

    symbol = _first_cashtag(tweet_text)
    if not symbol:
        return None

    entry_price = _extract_entry_price(tweet_text, symbol)

    weight = pba_state.get(symbol)
    if weight is None or weight <= 0:
        pct_match = re.search(
            r"(?:position\s+)?(\d+(?:\.\d+)?)\s*%(?:\s*size)?", tweet_text, re.I
        )
        if pct_match:
            weight = float(pct_match.group(1))

    entry_note = f" @ {entry_price}" if entry_price else ""
    return TradeSignal(
        action="buy",
        symbol=symbol,
        target_weight_pct=weight,
        entry_price=entry_price,
        stop_price=stop_price,
        confidence=0.97,
        reasoning=(
            f"Buy {symbol}{entry_note}; stop {stop_price} = PBA 조건매도 "
            "(sell when price <= stop)"
        ),
        is_new_position=True,
        raw={"rule": "entry_with_stop", "llm_provider": "rules"},
    )


def extract_portfolio_weights(tweet_text: str) -> dict[str, float] | None:
    """Parse PBA 'Longs by Cost/Size' snapshot lines like '$SNDK $1721.16 12%'."""
    header = re.search(r"longs\s+by\s+cost", tweet_text, re.I)
    if not header:
        return None
    section = tweet_text[header.start() :]
    weights: dict[str, float] = {}
    # X often breaks lines: "$SNDK\n  $1721.16 12%" (price and % not on ticker line)
    pattern = re.compile(
        r"\$([A-Za-z]{1,5})\b[\s\S]{0,160}?(\d+(?:\.\d+)?)\s*%",
        re.IGNORECASE,
    )
    for match in pattern.finditer(section):
        sym = match.group(1).upper()
        pct = float(match.group(2))
        if sym == "CASH" or not (0 < pct <= 100):
            continue
        weights[sym] = pct
    return weights or None


def refine_trade_signal(tweet_text: str, signal: TradeSignal) -> TradeSignal:
    """Rule-based fixes for common PBA phrasing the LLM misreads."""
    text = tweet_text
    upper = text.upper()

    if signal.action == "portfolio_sync":
        return signal

    rebuy = parse_entry_with_stop(text, {})
    if rebuy and signal.action in {"sell", "stop_update", "hold", "noise"}:
        sym = rebuy.symbol or signal.symbol
        return TradeSignal(
            action="buy",
            symbol=sym,
            target_weight_pct=signal.target_weight_pct or rebuy.target_weight_pct,
            entry_price=rebuy.entry_price,
            stop_price=rebuy.stop_price,
            confidence=max(signal.confidence, rebuy.confidence),
            reasoning=f"[refine] rebuy+stop order ({rebuy.reasoning})",
            is_new_position=True,
            raw={**signal.raw, "refined_from": signal.action},
        )

    # Scenario / commentary tweets (multiple "if", "in trouble lol")
    if signal.action == "sell" and (
        len(re.findall(r"\bif\b", text, re.I)) >= 2
        or re.search(r"\bin\s+trouble\b", text, re.I)
    ):
        return TradeSignal(
            action="hold",
            symbol=signal.symbol,
            confidence=0.75,
            reasoning=f"[refine] scenario commentary, not a trade ({signal.reasoning})",
            raw={**signal.raw, "refined_from": signal.action},
        )

    # Hypothetical: "if $CGNX hits ... if $SNDK stops me flat"
    if signal.action == "sell" and re.search(
        r"\bif\b.{0,120}\b(stops?\s+me|stopped|hits)\b", text, re.I | re.DOTALL
    ):
        return TradeSignal(
            action="hold",
            symbol=signal.symbol,
            confidence=0.7,
            reasoning=f"[refine] hypothetical stop scenario, not an exit ({signal.reasoning})",
            raw={**signal.raw, "refined_from": signal.action},
        )

    # "stop flat" = breakeven stop, not exit
    if signal.action == "sell" and re.search(r"\bstop\s+flat\b", text, re.I):
        sym = signal.symbol or _first_cashtag(text)
        return TradeSignal(
            action="stop_update",
            symbol=sym,
            stop_price=signal.stop_price,
            target_weight_pct=signal.target_weight_pct,
            confidence=max(signal.confidence, 0.85),
            reasoning=f"[refine] stop flat = raise stop, not sell ({signal.reasoning})",
            raw={**signal.raw, "refined_from": "sell"},
        )

    # Short "Stopped $TICKER" posts (past tense stop-out), not "if ... stops me"
    if signal.action in {"hold", "noise"} and re.search(r"\bSTOPPED\b", upper):
        if not re.search(r"\bIF\b", upper):
            sym = _first_cashtag(text)
            if sym:
                return TradeSignal(
                    action="sell",
                    symbol=sym,
                    target_weight_pct=0.0,
                    confidence=0.9,
                    reasoning="[refine] stopped out of position",
                    raw={**signal.raw, "refined_from": signal.action},
                )

    # Fill stop_price from tweet when LLM omitted PBA 조건매도
    if signal.action in {"buy", "add", "stop_update"} and signal.stop_price is None:
        cond_stop = extract_conditional_stop_price(text)
        if cond_stop is not None:
            signal = replace(
                signal,
                stop_price=cond_stop,
                reasoning=f"{signal.reasoning} [refine] stop={cond_stop} 조건매도",
            )

    # Buy + stop misread as sell (stop is conditional, not exit now)
    if signal.action == "sell" and extract_conditional_stop_price(text) is not None:
        if re.search(r"\b(?:re-?bought|bought|매수|entered)\b", text, re.I):
            buy_fix = parse_entry_with_stop(text, {})
            if buy_fix:
                return buy_fix

    # stop_update with explicit size: "Stop is $1729.90, it's 11% size"
    if signal.action == "stop_update" and signal.symbol:
        pct_match = re.search(
            rf"\${signal.symbol}[^\n%]*?(\d+(?:\.\d+)?)\s*%\s*size",
            text,
            re.I,
        )
        if not pct_match:
            pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*size", text, re.I)
        if pct_match:
            signal.target_weight_pct = float(pct_match.group(1))

    return signal


def _first_cashtag(text: str) -> str | None:
    m = re.search(r"\$([A-Za-z]{1,5})\b", text)
    return m.group(1).upper() if m else None
