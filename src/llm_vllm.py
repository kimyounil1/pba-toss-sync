"""Local vLLM (OpenAI-compatible) — same pattern as trading-bot llm_vllm.py."""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_VLLM_BASE_URL = ""
DEFAULT_VLLM_MODEL = "gemma4-26B"
DEFAULT_VLLM_TIMEOUT_SEC = 120.0


def _env_flag(name: str, *, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes"}


def vllm_enabled() -> bool:
    if not _env_flag("LLM_VLLM_ENABLED", default="0"):
        return False
    return bool(vllm_base_url())


def vllm_base_url() -> str:
    return os.getenv("LLM_VLLM_BASE_URL", DEFAULT_VLLM_BASE_URL).strip().rstrip("/")


def vllm_model_name() -> str:
    return os.getenv("LLM_VLLM_MODEL", DEFAULT_VLLM_MODEL).strip() or DEFAULT_VLLM_MODEL


def vllm_timeout_sec() -> float:
    try:
        return float(os.getenv("LLM_VLLM_TIMEOUT", str(DEFAULT_VLLM_TIMEOUT_SEC)))
    except ValueError:
        return DEFAULT_VLLM_TIMEOUT_SEC


def should_fallback_to_vllm(exc: BaseException) -> bool:
    """True when Gemini errors are worth retrying on local vLLM."""
    if not vllm_enabled():
        return False
    message = str(exc).lower()
    type_name = type(exc).__name__.lower()
    auth_markers = (
        "invalid api key",
        "api key not valid",
        "permission denied",
        "unauthenticated",
        "401",
        "403",
    )
    if any(marker in message for marker in auth_markers):
        return False

    markers = (
        "resource_exhausted",
        "quota",
        "rate limit",
        "429",
        "503",
        "500",
        "internal",
        "unavailable",
        "deadline",
        "timeout",
        "connection",
        "servererror",
        "clienterror",
        "too many requests",
        "limit: 0",
    )
    return any(marker in message or marker in type_name for marker in markers)


def _extract_chat_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise ValueError("vLLM response missing choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if content is None:
        raise ValueError("vLLM response missing message content")
    text = str(content).strip()
    if not text:
        raise ValueError("empty vLLM response")
    return text


def generate_vllm_text(prompt: str, *, model: str | None = None) -> str:
    """Call local vLLM OpenAI-compatible chat/completions endpoint."""
    base_url = vllm_base_url()
    model_name = model or vllm_model_name()
    url = f"{base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("LLM_VLLM_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": float(os.getenv("LLM_VLLM_TEMPERATURE", "0.2")),
        "max_tokens": int(os.getenv("LLM_VLLM_MAX_TOKENS", "512")),
    }

    with httpx.Client(timeout=vllm_timeout_sec()) as client:
        response = client.post(url, json=body, headers=headers)
    if response.status_code >= 400:
        raise RuntimeError(f"vLLM HTTP {response.status_code}: {response.text[:300]}")
    return _extract_chat_content(response.json())


def probe_vllm_models() -> list[str]:
    """List model ids from GET /v1/models (health/diagnostics)."""
    url = f"{vllm_base_url()}/models"
    with httpx.Client(timeout=min(10.0, vllm_timeout_sec())) as client:
        response = client.get(url)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or []
    return [str(item.get("id", "")) for item in data if item.get("id")]
