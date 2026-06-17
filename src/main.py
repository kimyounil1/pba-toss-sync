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
from src.leverage import LeverageResolver
from src.llm_parser import LLMParser, TradeSignal
from src.notifier import Notifier
from src.order_executor import OrderExecutor
from src.pba_state import PBAStateManager
from src.position_sizer import PositionSizer
from src.safety import SafetyGuard
from src.stop_monitor import StopMonitor
from src.symbol_aliases import SymbolAliasStore
from src.broker import create_broker
from src.broker_runtime import BrokerRuntime, create_broker_runtime
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
        self.pba_state = PBAStateManager(config.data_dir / "pba_portfolio_state.json")
        self.parser = LLMParser(config)
        self.notifier = Notifier(config)
        self.runtimes: list[BrokerRuntime] = [
            create_broker_runtime(config, name, self.db, self.notifier)
            for name in config.active_brokers
        ]
        self.leverage = LeverageResolver(
            config, SymbolAliasStore(config.data_dir / "symbol_aliases.json")
        )
        self.stop_monitors = [
            StopMonitor(config, rt, self.db, self.notifier) for rt in self.runtimes
        ]
        self.x_monitor = create_x_monitor(config, self.db)
        # backward compat: primary runtime
        self.bridge = self.runtimes[0].bridge if self.runtimes else create_broker(config)
        self.sizer = self.runtimes[0].sizer if self.runtimes else PositionSizer(config, self.bridge)
        self.safety = self.runtimes[0].safety if self.runtimes else SafetyGuard(config, self.db)
        self.executor = self.runtimes[0].executor if self.runtimes else None

    def _resolve_trade_context(
        self, signal: TradeSignal, bridge: object
    ) -> tuple[str, float | None, dict[str, object]]:
        """Map PBA underlying → traded symbol; adjust stop for 2x ETF."""
        if not signal.symbol:
            return "", signal.stop_price, {}

        underlying = signal.symbol.upper()
        trade_symbol = underlying
        adjusted_stop = signal.stop_price
        meta: dict[str, object] = {"underlying": underlying}

        if signal.action in {"buy", "add"}:
            choice = self.leverage.resolve_buy(self.bridge, underlying)
            if choice:
                trade_symbol = choice.traded
                meta.update(
                    {
                        "leverage": True,
                        "traded": choice.traded,
                        "multiplier": choice.multiplier,
                    }
                )
                if signal.stop_price is not None:
                    adjusted_stop = self.leverage.adjust_stop(
                        self.bridge,
                        underlying=underlying,
                        traded=trade_symbol,
                        stop_price=signal.stop_price,
                        entry_price=signal.entry_price,
                        multiplier=choice.multiplier,
                    )
        elif signal.action in {"sell", "reduce", "stop_update"}:
            traded = self.leverage.trade_symbol_for(underlying)
            if traded != underlying:
                mult = self.leverage.multiplier_for(underlying)
                trade_symbol = traded
                meta.update({"leverage": True, "traded": traded, "multiplier": mult})
                if signal.stop_price is not None:
                    adjusted_stop = self.leverage.adjust_stop(
                        self.bridge,
                        underlying=underlying,
                        traded=trade_symbol,
                        stop_price=signal.stop_price,
                        entry_price=signal.entry_price,
                        multiplier=mult,
                    )

        return trade_symbol, adjusted_stop, meta

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
            statuses: list[str] = []
            for rt in self.runtimes:
                trade_symbol, adjusted_stop, lever_meta = self._resolve_trade_context(
                    signal, rt.bridge
                )
                stop_price = adjusted_stop if adjusted_stop is not None else signal.stop_price
                result = await rt.executor.update_stop_only(
                    trade_symbol, stop_price, tweet.id, lever_meta
                )
                statuses.append(f"{rt.name}:{result.get('status')}")
                if lever_meta.get("leverage"):
                    await self.notifier.send(
                        f"[2x STOP][{rt.name}] {signal.symbol} → {trade_symbol} "
                        f"underlying_stop={signal.stop_price} lever_stop={stop_price:.4f}"
                    )
            self.db.mark_tweet(
                tweet.id, tweet.created_at, tweet.text, signal_dict, ",".join(statuses)
            )
            return

        if not signal.symbol or target_weight is None:
            self.db.mark_tweet(tweet.id, tweet.created_at, tweet.text, signal_dict, "no_symbol_or_weight")
            return

        statuses = []
        any_leverage = False
        for rt in self.runtimes:
            trade_symbol, adjusted_stop, lever_meta = self._resolve_trade_context(
                signal, rt.bridge
            )
            plan = rt.sizer.plan_from_signal(
                signal.action,
                trade_symbol,
                target_weight,
                signal.entry_price,
            )
            if plan is None:
                statuses.append(f"{rt.name}:no_plan")
                continue

            if lever_meta.get("leverage"):
                any_leverage = True
                await self.notifier.send(
                    f"[2x LEVER][{rt.name}] {signal.symbol} → {trade_symbol} "
                    f"(x{lever_meta.get('multiplier')}) stop: {signal.stop_price} → {adjusted_stop}"
                )

            result = await rt.executor.execute_plan(
                plan, tweet.id, stop_price=adjusted_stop, leverage_meta=lever_meta
            )
            statuses.append(f"{rt.name}:{result.get('status', 'unknown')}")

            if result.get("status") in {"submitted", "dry_run"}:
                if lever_meta.get("leverage") and signal.action in {"buy", "add"}:
                    self.leverage.aliases.set(
                        str(lever_meta["underlying"]),
                        str(lever_meta["traded"]),
                        float(lever_meta["multiplier"]),  # type: ignore[arg-type]
                    )
                if signal.action == "sell" and target_weight == 0:
                    self.leverage.aliases.remove(signal.symbol.upper())

        if not any_leverage and len(self.runtimes) == 1:
            pass  # no 2x notification

        self.db.mark_tweet(
            tweet.id,
            tweet.created_at,
            tweet.text,
            signal_dict,
            ",".join(statuses) if statuses else "no_runtime",
        )

    async def run_daemon(self) -> None:
        for rt in self.runtimes:
            auth = rt.bridge.auth_status()
            if not auth.get("logged_in"):
                if rt.name == "alpaca":
                    logger.warning("[%s] Alpaca not connected — set ALPACA_API_KEY", rt.name)
                elif rt.name == "tossctl":
                    logger.warning("[%s] tossctl not logged in — run: tossctl auth login", rt.name)
                else:
                    logger.warning(
                        "[%s] Toss Open API not connected — set TOSS_CLIENT_ID / TOSS_CLIENT_SECRET",
                        rt.name,
                    )
            else:
                dry = rt.safety.is_dry_run()
                logger.info(
                    "%s session OK (dry_run=%s kill=%s)",
                    rt.name,
                    dry,
                    rt.safety.kill_switch_active(),
                )

        if self.config.x_source == "browser" and not Path(self.config.x_session_file).is_file():
            logger.error(
                "X browser session missing (%s). Run: bash scripts/x_auth_login.sh",
                self.config.x_session_file,
            )
            return

        dry = any(rt.safety.is_dry_run() for rt in self.runtimes)
        brokers = ",".join(rt.name for rt in self.runtimes)
        logger.info(
            "Starting daemon dry_run=%s brokers=%s pba=@%s x_source=%s poll=%ss",
            dry,
            brokers,
            self.config.pba_username,
            self.config.x_source,
            self.config.poll_interval_sec,
        )
        await self.notifier.send(
            f"PBA-Toss sync started (brokers={brokers}, dry_run={dry}, "
            f"2x={self.config.use_2x_leverage}, user=@{self.config.pba_username})"
        )

        stop_tasks = [asyncio.create_task(mon.run_loop()) for mon in self.stop_monitors]
        try:
            async for tweet in self.x_monitor.poll_loop():
                try:
                    await self.process_tweet(tweet)
                except Exception as exc:
                    logger.exception("Failed processing tweet %s: %s", tweet.id, exc)
                    await self.notifier.send(f"[ERROR] tweet {tweet.id}: {exc}")
        finally:
            for task in stop_tasks:
                task.cancel()
            for task in stop_tasks:
                try:
                    await task
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
        return signal

    async def run_leverage_check(self, symbol: str) -> None:
        import json

        report: dict[str, object] = {"symbol": symbol.upper(), "brokers": {}}
        for rt in self.runtimes:
            report["brokers"][rt.name] = self.leverage.check_symbol(rt.bridge, symbol)
        print(json.dumps(report, ensure_ascii=False, indent=2))

    async def run_simulate_leverage(self, text: str) -> None:
        """Parse tweet text and show 2x trade resolution without ordering."""
        signal = self.parser.parse(text, self.pba_state.get_weights())
        for rt in self.runtimes:
            trade_symbol, adjusted_stop, meta = self._resolve_trade_context(signal, rt.bridge)
            print(f"[{rt.name}] trade_symbol: {trade_symbol} adjusted_stop: {adjusted_stop}")
            print(f"[{rt.name}] leverage_meta: {meta}")
        print(f"action: {signal.action} underlying: {signal.symbol}")
        print(f"entry: {signal.entry_price} stop: {signal.stop_price}")


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
    lev = sub.add_parser("leverage-check", help="Check 2x ETF availability for a symbol")
    lev.add_argument("symbol", help="Underlying ticker e.g. SNDK")
    sim = sub.add_parser("simulate-leverage", help="Parse tweet and show 2x mapping (no order)")
    sim.add_argument("text", nargs="+")
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
        x_session = Path(config.x_session_file).is_file()
        print(f"x_source: {config.x_source}")
        print(f"x_session: {config.x_session_file} (exists={x_session})")
        if config.x_source == "browser" and not x_session:
            print("  → run: bash scripts/x_auth_login.sh")
        print(f"brokers: {config.active_brokers}")
        for rt in app.runtimes:
            auth = rt.bridge.auth_status()
            print(
                f"  [{rt.name}] logged_in={auth.get('logged_in')} "
                f"dry_run={rt.safety.is_dry_run()} kill={rt.safety.kill_switch_active()}"
            )
            if rt.name == "alpaca" and auth.get("equity"):
                print(f"    alpaca paper={config.alpaca_paper} equity=${auth.get('equity')}")
            if rt.name == "toss" and auth.get("account_seq"):
                print(f"    toss account_seq={auth.get('account_seq')}")
        if "tossctl" in config.active_brokers:
            toss_cfg = Path(config.tossctl_config_dir) / "config.json"
            print(f"tossctl config: {toss_cfg} (exists={toss_cfg.is_file()})")
        print(f"use_2x_leverage: {config.use_2x_leverage}")
        print(f"leverage_auto_discover: {config.leverage_auto_discover}")
        print(f"symbol_aliases: {app.leverage.aliases.all_aliases()}")
        print(f"PBA weights: {app.pba_state.get_weights()}")
        print(f"active stops: {app.db.list_active_stops()}")
        return 0

    if cmd == "leverage-check":
        asyncio.run(app.run_leverage_check(args.symbol))
        return 0

    if cmd == "simulate-leverage":
        text = " ".join(args.text)
        asyncio.run(app.run_simulate_leverage(text))
        return 0

    if cmd == "parse":
        text = " ".join(args.text)
        signal = asyncio.run(app.run_parse_only(text))
        print(asdict(signal))
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
