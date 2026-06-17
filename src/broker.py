"""Broker factory — toss Open API, tossctl (legacy), or Alpaca."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from src.config import AppConfig


class TradingBridge(Protocol):
    def auth_status(self) -> dict: ...

    def account_summary(self) -> dict: ...

    def portfolio_positions(self) -> list: ...

    def quote_get(self, symbol: str) -> dict: ...

    def quote_batch_live(self, symbols: list[str]) -> dict: ...

    def order_preview(self, **kwargs) -> object: ...

    def order_place(self, **kwargs) -> dict: ...

    def extract_total_value_krw(self, summary: dict) -> float: ...

    def extract_cash_krw(self, summary: dict) -> float: ...

    def position_value_krw(self, position: dict) -> float: ...

    def position_symbol(self, position: dict) -> str: ...

    def position_qty(self, position: dict) -> float: ...

    def quote_price_krw(self, quote: dict) -> float: ...


def create_broker(config: AppConfig) -> TradingBridge:
    from src.alpaca_bridge import AlpacaBridge
    from src.toss_bridge import TossBridge
    from src.toss_openapi_bridge import TossOpenApiBridge

    broker = (config.broker or "toss").lower()
    if broker == "alpaca":
        return AlpacaBridge(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=config.alpaca_paper,
            base_url=config.alpaca_base_url,
            data_url=config.alpaca_data_url,
            extended_hours=config.alpaca_extended_hours,
            limit_orders_only=config.alpaca_limit_orders_only,
        )
    if broker == "tossctl":
        return TossBridge(config.tossctl_bin, config.tossctl_config_dir)
    return TossOpenApiBridge(
        client_id=config.toss_client_id,
        client_secret=config.toss_client_secret,
        account_seq=config.toss_account_seq,
        base_url=config.toss_base_url,
    )
