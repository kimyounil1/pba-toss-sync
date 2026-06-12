#!/usr/bin/env python3
"""Sync local PBA weights and rebalance broker to match."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.broker import create_broker
from src.config import load_config
from src.db import StateDB
from src.notifier import Notifier
from src.order_executor import OrderExecutor
from src.pba_state import PBAStateManager
from src.position_sizer import PositionSizer
from src.safety import SafetyGuard

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sync_portfolio")

# Latest official snapshot: Weekly Portfolio Update 6/5/26 (+ 6/8 no trades)
PBA_TARGETS: dict[str, float] = {
    "NBIS": 16.0,
    "DOCN": 12.0,
    "ARM": 9.0,
    "BB": 8.0,
    "DELL": 2.5,
}


async def main() -> int:
    config = load_config()
    bridge = create_broker(config)
    auth = bridge.auth_status()
    if not auth.get("logged_in"):
        logger.error("Broker not connected: %s", auth.get("error", "unknown"))
        return 1

    pba_state = PBAStateManager(config.data_dir / "pba_portfolio_state.json")
    pba_state.state.weights = dict(PBA_TARGETS)
    pba_state.state.stops = {}
    pba_state.save()
    logger.info("PBA state updated: %s", pba_state.get_weights())

    db = StateDB(config.data_dir / "state.db")
    safety = SafetyGuard(config, db)
    notifier = Notifier(config)
    sizer = PositionSizer(config, bridge)
    executor = OrderExecutor(config, bridge, db, safety, notifier)

    positions = bridge.portfolio_positions()
    held = {bridge.position_symbol(p) for p in positions if bridge.position_qty(p) > 0}
    symbols = sorted(set(PBA_TARGETS) | held)

    summary = bridge.account_summary()
    equity = bridge.extract_total_value_krw(summary)
    logger.info("Account equity: $%.2f — rebalancing %d symbols", equity, len(symbols))

    results: list[str] = []
    for symbol in symbols:
        target = PBA_TARGETS.get(symbol, 0.0)
        plan = sizer.build_plan(symbol, target)
        if not plan.should_execute:
            results.append(f"  {symbol}: skip ({plan.skip_reason})")
            continue
        result = await executor.execute_plan(plan, tweet_id=f"manual-sync-{symbol}")
        status = result.get("status", "unknown")
        results.append(f"  {symbol}: {plan.side} ${plan.delta_krw:.0f} → {status}")

    print("\n=== Portfolio sync ===")
    print(f"Targets: {PBA_TARGETS}")
    print(f"Equity: ${equity:,.2f}")
    for line in results:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
