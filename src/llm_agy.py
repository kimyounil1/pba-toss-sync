"""Antigravity CLI (agy) — local headless LLM via `agy -p` (not vLLM Gemma4)."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

DEFAULT_AGY_BIN = "agy"
DEFAULT_AGY_WORKDIR = str(Path(__file__).resolve().parents[1])
DEFAULT_AGY_TIMEOUT_SEC = 180.0
DEFAULT_AGY_MODEL = "Gemini 3.5 Flash (Low)"


def _env_flag(name: str, *, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes"}


def agy_enabled() -> bool:
    if not _env_flag("LLM_AGY_ENABLED", default="1"):
        return False
    return shutil.which(agy_bin()) is not None


def agy_bin() -> str:
    return os.getenv("LLM_AGY_BIN", DEFAULT_AGY_BIN).strip() or DEFAULT_AGY_BIN


def agy_workdir() -> Path:
    raw = os.getenv("LLM_AGY_WORKDIR", DEFAULT_AGY_WORKDIR).strip()
    return Path(raw).expanduser()


def agy_model_name() -> str:
    return os.getenv("LLM_AGY_MODEL", DEFAULT_AGY_MODEL).strip()


def agy_timeout_sec() -> float:
    try:
        return float(os.getenv("LLM_AGY_TIMEOUT", str(DEFAULT_AGY_TIMEOUT_SEC)))
    except ValueError:
        return DEFAULT_AGY_TIMEOUT_SEC


def _build_agy_cmd(prompt: str, *, model: str | None = None) -> list[str]:
    cmd = [
        agy_bin(),
        "-p",
        prompt,
        "--dangerously-skip-permissions",
        f"--print-timeout={int(agy_timeout_sec())}s",
    ]
    model_name = model or agy_model_name()
    if model_name:
        cmd.extend(["--model", model_name])
    extra = os.getenv("LLM_AGY_EXTRA_ARGS", "").strip()
    if extra:
        cmd.extend(extra.split())
    return cmd


def _run_agy_subprocess(cmd: list[str], workdir: Path) -> subprocess.CompletedProcess[str]:
    """agy -p writes to stdout only on a TTY; use script(1) when piped."""
    timeout = agy_timeout_sec() + 45.0
    script_bin = shutil.which("script")
    if script_bin:
        inner = " ".join(shlex.quote(part) for part in cmd)
        wrapper = [script_bin, "-q", "-c", inner, "/dev/null"]
        return subprocess.run(
            wrapper,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    return subprocess.run(
        cmd,
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _extract_response_text(stdout: str) -> str:
    # script(1) may inject control chars
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", stdout)
    text = text.replace("\r", "").strip()
    if not text:
        raise ValueError("empty agy stdout")

    matches = list(re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL))
    if matches:
        return matches[-1].group(0).strip()

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        return lines[-1]
    return text


def generate_agy_text(prompt: str, *, model: str | None = None) -> str:
    """Run `agy -p` in project workspace; return model text (JSON expected by parser)."""
    workdir = agy_workdir()
    if not workdir.is_dir():
        raise FileNotFoundError(f"LLM_AGY_WORKDIR not found: {workdir}")

    cmd = _build_agy_cmd(prompt, model=model)
    try:
        completed = _run_agy_subprocess(cmd, workdir)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"agy timed out after {agy_timeout_sec() + 45}s") from exc

    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()[:500]
        raise RuntimeError(f"agy exit {completed.returncode}: {err}")

    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    return _extract_response_text(combined)
