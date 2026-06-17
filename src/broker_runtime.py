"""Per-broker trading runtime (bridge, sizer, executor, safety)."""

from __future__ import annotations

from dataclasses import dataclass

from src.broker import create_broker
from src.config import AppConfig
from src.db import StateDB
from src.notifier import Notifier
from src.order_executor import OrderExecutor
from src.position_sizer import PositionSizer
from src.safety import SafetyGuard


@dataclass
class BrokerRuntime:
    name: str
    bridge: object
    sizer: PositionSizer
    executor: OrderExecutor
    safety: SafetyGuard


def create_broker_runtime(
    config: AppConfig,
    broker_name: str,
    db: StateDB,
    notifier: Notifier,
) -> BrokerRuntime:
    broker_config = config.with_broker(broker_name)
    bridge = create_broker(broker_config)
    safety = SafetyGuard(broker_config, db, broker_name=broker_name)
    return BrokerRuntime(
        name=broker_name,
        bridge=bridge,
        sizer=PositionSizer(broker_config, bridge),
        executor=OrderExecutor(broker_config, bridge, db, safety, notifier),
        safety=safety,
    )
