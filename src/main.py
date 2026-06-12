"""Main asyncio daemon orchestrating X → LLM → Toss pipeline."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from src.config import AppConfig, load_config
from src.db import StateDB
from src.llm_parser import LLMParser, TradeSignal
from src.notifier import Notifier
from src.order_executor import OrderExecutor
from src.pba_state import PBAStateManager
from src.position_sizer import PositionSizer
from src.safety import SafetyGuard
from src.stop_monitor import StopMonitor
from src.broker import create_broker
from src.analyze import run_historical_analysis
from src.x_monitor import Tweet, create_x_monitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pba-toss-sync")


class App:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.db = StateDB(config.data_dir / "state.db")
        self.bridge = create_broker(config)
        self.pba_state = PBAStateManager(config.data_dir / "pba_portfolio_state.json")
        self.parser = LLMParser(config)
        self.sizer = PositionSizer(config, self.bridge)
        self.safety = SafetyGuard(config, self.db)
        self.notifier = Notifier(config)
        self.executor = OrderExecutor(config, self.bridge, self.db, self.safety, self.notifier)
        self.stop_monitor = StopMonitor(config, self.bridge, self.db, self.executor, self.notifier)
        self.x_monitor = create_x_monitor(config, self.db)

    async def process_tweet(self, tweet: Tweet) -> None:
        if self.db.tweet_seen(tweet.id):
            return

        signal = self.parser.parse(tweet.text, self.pba_state.get_weights())
        signal_dict = {
            "action": signal.action,
            "symbol": signal.symbol,
            "market": signal.market,
            "target_weight_pct": signal.target_weight_pct,
            "entry_price": signal.entry_price,
            "stop_price": signal.stop_price,
            "confidence": signal.confidence,
            "reasoning": signal.reasoning,
        }
        await self.notifier.notify_signal(tweet.id, tweet.text, signal_dict)

        if signal.action == "portfolio_sync":
            self.pba_state.apply_signal(signal)
            self.db.mark_tweet(tweet.id, tweet.created_at, tweet.text, signal_dict, "portfolio_sync")
            return

        if not signal.passes_threshold(self.config.confidence_threshold):
            self.db.mark_tweet(tweet.id, tweet.created_at, tweet.text, signal_dict, "low_confidence")
            return

        target_weight = self.pba_state.apply_signal(signal)

        if signal.action == "stop_update" and signal.symbol and signal.stop_price:
            await self.executor.update_stop_only(signal.symbol, signal.stop_price, tweet.id)
            self.db.mark_tweet(tweet.id, tweet.created_at, tweet.text, signal_dict, "stop_update")
            return

        if not signal.symbol or target_weight is None:
            self.db.mark_tweet(tweet.id, tweet.created_at, tweet.text, signal_dict, "no_symbol_or_weight")
            return

        plan = self.sizer.plan_from_signal(
            signal.action,
            signal.symbol,
            target_weight,
            signal.entry_price,
        )
        if plan is None:
            self.db.mark_tweet(tweet.id, tweet.created_at, tweet.text, signal_dict, "no_plan")
            return

        result = await self.executor.execute_plan(plan, tweet.id, stop_price=signal.stop_price)
        self.db.mark_tweet(
            tweet.id,
            tweet.created_at,
            tweet.text,
            signal_dict,
            result.get("status", "unknown"),
        )

    async def run_daemon(self) -> None:
        auth = self.bridge.auth_status()
        if not auth.get("logged_in"):
            if self.config.broker == "alpaca":
                logger.warning("Alpaca not connected — set ALPACA_API_KEY / ALPACA_SECRET_KEY")
            else:
                logger.warning("tossctl not logged in — run: tossctl auth login")
        else:
            logger.info("%s session OK", auth.get("broker", self.config.broker))

        if self.config.x_source == "browser" and not Path(self.config.x_session_file).is_file():
            logger.error(
                "X browser session missing (%s). Run: bash scripts/x_auth_login.sh",
                self.config.x_session_file,
            )
            return

        dry = self.safety.is_dry_run()
        logger.info(
            "Starting daemon dry_run=%s pba=@%s x_source=%s poll=%ss",
            dry,
            self.config.pba_username,
            self.config.x_source,
            self.config.poll_interval_sec,
        )
        await self.notifier.send(
            f"PBA-Toss sync started (dry_run={dry}, user=@{self.config.pba_username})"
        )

        stop_task = asyncio.create_task(self.stop_monitor.run_loop())
        try:
            async for tweet in self.x_monitor.poll_loop():
                try:
                    await self.process_tweet(tweet)
                except Exception as exc:
                    logger.exception("Failed processing tweet %s: %s", tweet.id, exc)
                    await self.notifier.send(f"[ERROR] tweet {tweet.id}: {exc}")
        finally:
            stop_task.cancel()
            try:
                await stop_task
            except asyncio.CancelledError:
                pass

    async def run_backfill(self, limit: int) -> None:
        tweets = await self.x_monitor.fetch_recent_for_backfill(limit)
        logger.info("Backfill: %d tweets", len(tweets))
        for tweet in tweets:
            await self.process_tweet(tweet)

    async def run_process_tweet(self, url_or_id: str) -> None:
        if self.config.x_source != "browser":
            raise ValueError("process-tweet requires x.source=browser")
        from src.x_browser_monitor import parse_tweet_id

        tweet_id = parse_tweet_id(url_or_id)
        tweet = await self.x_monitor.fetch_tweet_by_id(tweet_id)
        signal = self.parser.parse(tweet.text, self.pba_state.get_weights())
        print(f"id: {tweet.id}")
        print(f"at: {tweet.created_at}")
        print(f"action: {signal.action} conf={signal.confidence}")
        print(f"symbol: {signal.symbol} target%: {signal.target_weight_pct}")
        print(f"entry: {signal.entry_price} stop: {signal.stop_price}")
        if signal.raw.get("portfolio_weights"):
            print(f"portfolio: {signal.raw['portfolio_weights']}")
        print(f"reasoning: {signal.reasoning}")

    async def run_x_auth_status(self) -> None:
        path = self.config.x_session_file
        exists = Path(path).is_file()
        print(f"X source: {self.config.x_source}")
        print(f"X session file: {path}")
        print(f"session exists: {exists}")
        if self.config.x_source == "browser" and not exists:
            print("Run: bash scripts/x_auth_login.sh")

    async def run_parse_only(self, text: str) -> TradeSignal:
        signal = self.parser.parse(text, self.pba_state.get_weights())
        print(asdict(signal))
        return signal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PBA X → Toss auto-sync")
    parser.add_argument("--settings", type=Path, default=None)
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("daemon", help="Run 24/7 monitor")
    analyze = sub.add_parser("analyze", help="Fetch N days of posts and write report")
    analyze.add_argument("--days", type=int, default=30)
    analyze.add_argument("--llm-delay", type=float, default=0.5, help="Seconds between LLM calls")
    analyze.add_argument(
        "--tweet-url",
        action="append",
        default=[],
        help="Supplement analysis with tweet id/URL (subscriber posts often missing from profile scroll)",
    )
    pt = sub.add_parser("process-tweet", help="Fetch and parse one tweet by status URL")
    pt.add_argument("url", help="https://x.com/i/status/ID or numeric id")
    backfill = sub.add_parser("backfill", help="Process recent tweets once")
    backfill.add_argument("--limit", type=int, default=20)
    parse = sub.add_parser("parse", help="Parse a tweet text (dry)")
    parse.add_argument("text", nargs="+")
    sub.add_parser("status", help="Show auth and config status")
    sub.add_parser("x-auth-status", help="Check X browser session file")

    args = parser.parse_args(argv)
    cmd = args.cmd or "daemon"
    config = load_config(args.settings)
    app = App(config)

    if cmd == "x-auth-status":
        asyncio.run(app.run_x_auth_status())
        return 0

    if cmd == "status":
        auth = app.bridge.auth_status()
        x_session = Path(config.x_session_file).is_file()
        print(f"x_source: {config.x_source}")
        print(f"x_session: {config.x_session_file} (exists={x_session})")
        if config.x_source == "browser" and not x_session:
            print("  → run: bash scripts/x_auth_login.sh")
        print(f"broker: {config.broker}")
        print(f"broker logged_in: {auth.get('logged_in')}")
        if config.broker == "alpaca":
            print(f"alpaca paper: {config.alpaca_paper}")
            if auth.get("equity"):
                print(f"alpaca equity: ${auth.get('equity')}")
        toss_cfg = Path(config.tossctl_config_dir) / "config.json"
        print(f"tossctl config: {toss_cfg} (exists={toss_cfg.is_file()})")
        if config.broker != "tossctl":
            print("tossctl: prepared but inactive (switch trading.broker to tossctl when ready)")
        elif auth.get("config_dir"):
            print(f"tossctl config_dir: {auth.get('config_dir')}")
        print(f"dry_run: {app.safety.is_dry_run()}")
        print(f"kill_switch: {app.safety.kill_switch_active()}")
        print(f"PBA weights: {app.pba_state.get_weights()}")
        print(f"active stops: {app.db.list_active_stops()}")
        return 0

    if cmd == "parse":
        text = " ".join(args.text)
        asyncio.run(app.run_parse_only(text))
        return 0

    if cmd == "analyze":
        asyncio.run(
            run_historical_analysis(
                config,
                days=args.days,
                llm_delay_sec=args.llm_delay,
                supplement_tweet_ids=args.tweet_url,
            )
        )
        return 0

    if cmd == "process-tweet":
        asyncio.run(app.run_process_tweet(args.url))
        return 0

    if cmd == "backfill":
        asyncio.run(app.run_backfill(args.limit))
        return 0

    asyncio.run(app.run_daemon())
    return 0


if __name__ == "__main__":
    sys.exit(main())
