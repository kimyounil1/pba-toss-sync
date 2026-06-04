"""Historical tweet analysis — parse 1 month and write summary report."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src.config import AppConfig
from src.db import StateDB
from src.llm_parser import LLMParser, TradeSignal
from src.pba_state import PBAStateManager
from src.position_sizer import PositionSizer
from src.toss_bridge import TossBridge
from src.x_monitor import Tweet, create_x_monitor

try:
    from src.x_browser_monitor import find_timeline_gaps
except ImportError:
    find_timeline_gaps = None  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)


def _plan_summary(
    sizer: PositionSizer, signal: TradeSignal, target_weight: float | None
) -> str:
    if signal.action in {"hold", "noise", "stop_update"} or not signal.symbol:
        return signal.action
    if target_weight is None:
        return "no_weight"
    plan = sizer.plan_from_signal(
        signal.action, signal.symbol, target_weight, signal.entry_price
    )
    if plan is None:
        return "no_plan"
    if plan.skip_reason:
        return f"skip:{plan.skip_reason}"
    return f"{plan.side} ~{int(plan.delta_krw):,} KRW → {plan.target_weight_pct}%"


async def _merge_tweets(primary: list[Tweet], extra: list[Tweet]) -> list[Tweet]:
    by_id = {t.id: t for t in primary}
    for tweet in extra:
        by_id.setdefault(tweet.id, tweet)
    return sorted(by_id.values(), key=lambda t: t.created_at or "")


async def run_historical_analysis(
    config: AppConfig,
    *,
    days: int = 30,
    llm_delay_sec: float = 0.0,
    supplement_tweet_ids: list[str] | None = None,
) -> Path:
    if config.x_source == "api" and not config.x_bearer_token:
        raise ValueError("X_BEARER_TOKEN required for api source")
    if config.x_source == "browser" and not Path(config.x_session_file).is_file():
        raise FileNotFoundError(
            f"X session missing: {config.x_session_file}. Run: bash scripts/x_auth_login.sh"
        )

    reports_dir = config.data_dir.parent / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = reports_dir / f"analysis_{config.pba_username}_{days}d_{stamp}.csv"
    json_path = reports_dir / f"analysis_{config.pba_username}_{days}d_{stamp}.json"
    md_path = reports_dir / f"analysis_{config.pba_username}_{days}d_{stamp}.md"

    # Fresh simulated PBA state (do not mutate production state file)
    sim_state_path = config.data_dir / "pba_portfolio_state.analysis.json"
    if sim_state_path.is_file():
        sim_state_path.unlink()
    pba_state = PBAStateManager(sim_state_path)
    parse_db = StateDB(config.data_dir / "llm_parse_cache.db")
    parser = LLMParser(config, parse_db=parse_db)
    bridge = TossBridge(config.tossctl_bin, config.tossctl_config_dir)
    sizer = PositionSizer(config, bridge)
    monitor = create_x_monitor(config, db=None)

    if config.x_source == "browser":
        tweets = await monitor.fetch_tweets_historical(
            days=days, supplement_tweet_ids=supplement_tweet_ids or None
        )
    else:
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            tweets = await monitor.fetch_tweets_historical(client, days=days)

    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    cache_hits = 0
    cache_misses = 0

    for i, tweet in enumerate(tweets):
        try:
            signal = parser.parse(
                tweet.text, pba_state.get_weights(), tweet_id=tweet.id
            )
        except Exception as exc:
            logger.warning("Parse failed %s: %s", tweet.id, exc)
            signal = TradeSignal(action="hold", confidence=0.0, reasoning=str(exc))

        if parser.last_parse_from_cache:
            cache_hits += 1
        else:
            cache_misses += 1
            if llm_delay_sec > 0 and cache_misses > 1:
                await asyncio.sleep(llm_delay_sec)

        target_weight: float | None = None
        plan_note = ""
        if signal.action == "portfolio_sync":
            pba_state.apply_signal(signal)
            plan_note = "portfolio_sync"
        elif signal.passes_threshold(config.confidence_threshold) and signal.action not in {
            "hold",
            "noise",
        }:
            target_weight = pba_state.apply_signal(signal)
            plan_note = _plan_summary(sizer, signal, target_weight)

        action = signal.action
        counts[action] = counts.get(action, 0) + 1

        rows.append(
            {
                "tweet_id": tweet.id,
                "created_at": tweet.created_at,
                "text": tweet.text[:500],
                "action": action,
                "symbol": signal.symbol,
                "confidence": signal.confidence,
                "target_weight_pct": target_weight if target_weight is not None else signal.target_weight_pct,
                "entry_price": signal.entry_price,
                "stop_price": signal.stop_price,
                "reasoning": signal.reasoning,
                "llm_provider": (
                    "cache"
                    if signal.raw.get("cache_hit")
                    else signal.raw.get("llm_provider", "")
                ),
                "parse_cached": signal.raw.get("cache_hit", False),
                "plan_note": plan_note,
                "pba_weights_after": json.dumps(pba_state.get_weights()),
            }
        )

    # CSV
    if rows:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    actionable = [r for r in rows if r["action"] not in ("noise", "hold") and r["confidence"] >= config.confidence_threshold]
    api_newest = tweets[-1].created_at if tweets else None
    api_oldest = tweets[0].created_at if tweets else None
    timeline_gaps = find_timeline_gaps(tweets) if find_timeline_gaps else []
    summary = {
        "username": config.pba_username,
        "days": days,
        "total_tweets": len(rows),
        "api_newest_tweet_at": api_newest,
        "api_oldest_tweet_at": api_oldest,
        "x_source": config.x_source,
        "api_lag_note": (
            "browser source uses logged-in session (includes subscriber posts). "
            "api source is public tweets only."
            if config.x_source == "browser"
            else "X API v2 is public tweets only; subscriber posts are excluded."
        ),
        "action_counts": counts,
        "actionable_signals": len(actionable),
        "final_pba_weights": pba_state.get_weights(),
        "final_pba_stops": dict(pba_state.state.stops),
        "llm_provider": config.llm_provider,
        "confidence_threshold": config.confidence_threshold,
        "timeline_gaps": timeline_gaps,
        "llm_cache_hits": cache_hits,
        "llm_cache_misses": cache_misses,
        "llm_persistent_cache": config.llm_persistent_cache,
    }
    json_path.write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    md_lines = [
        f"# PBA analysis @{config.pba_username} ({days} days)",
        "",
        f"- Total posts: **{summary['total_tweets']}**",
        f"- API newest tweet: **{api_newest or 'n/a'}** (UTC)",
        f"- API oldest tweet: **{api_oldest or 'n/a'}** (UTC)",
        f"- Actionable (conf ≥ {config.confidence_threshold}): **{summary['actionable_signals']}**",
        f"- LLM provider: `{config.llm_provider}`",
        f"- Parse cache: **{cache_hits}** hits / **{cache_misses}** LLM calls (`data/llm_parse_cache.db`)",
        "",
        f"> 수집 방식: `{config.x_source}` — browser면 구독 계정 세션으로 x.com 프로필을 스크래핑합니다.",
        "",
        "## Action counts",
        "",
    ]
    for action, n in sorted(counts.items(), key=lambda x: -x[1]):
        md_lines.append(f"- `{action}`: {n}")
    md_lines.extend(["", "## Final simulated PBA weights", "", "```json", json.dumps(summary["final_pba_weights"], indent=2), "```", ""])
    if actionable:
        md_lines.extend(["## Actionable signals", ""])
        for r in actionable:
            md_lines.append(
                f"- **{r['created_at'][:10]}** `{r['action']}` **{r['symbol']}** "
                f"({r['confidence']:.2f}) — {r['plan_note'] or r['reasoning'][:80]}"
            )
            md_lines.append(f"  > {r['text'][:120]}...")
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    logger.info("Wrote %s", csv_path)
    logger.info("Wrote %s", md_path)
    print(f"\n=== Analysis complete ===")
    print(f"Tweets: {summary['total_tweets']}, Actionable: {summary['actionable_signals']}")
    print(f"CSV:  {csv_path}")
    print(f"MD:   {md_path}")
    print(f"JSON: {json_path}")
    return md_path
