"""PBA-specific phrasing: stops, portfolio snapshots, hypotheticals."""

from src.llm_parser import (
    TradeSignal,
    extract_portfolio_weights,
    refine_trade_signal,
)
from src.pba_state import PBAStateManager


PORTFOLIO_TWEET = """Updated-
Longs by Cost/Size-
$NBIS    $107.55 16%
$ARM    $148.96 13%
$SNDK  $1721.16 12%
$DOCN $150.25 12%
$CGNX  $66.01   11%
$AKAM $152.22  11%
$BB        $6.15        8%
$DELL   $167.27    2.5%"""

PORTFOLIO_TWEET_X_LAYOUT = """Updated-

Longs by Cost/Size-


$NBIS
    $107.55 16%

$ARM
    $148.96 13%

$SNDK
  $1721.16 12%

$CGNX
  $66.01   11%

Cash 14%"""

HYPOTHETICAL = (
    "I have plans here soon, if $65.25 hits on $CGNX I was stopped, "
    "if $SNDK stops me flat in last hr up nearly 100pts we are in trouble lol."
)

FULL_EOD_TWEET = HYPOTHETICAL + "\n\n" + PORTFOLIO_TWEET_X_LAYOUT

STOP_FLAT = "Up 5%, stop flat and ready to potentially ride this puppy. $SNDK #climaxtop"


def test_extract_portfolio_weights():
    w = extract_portfolio_weights(PORTFOLIO_TWEET)
    assert w is not None
    assert w["SNDK"] == 12.0
    assert w["CGNX"] == 11.0


def test_extract_portfolio_weights_x_multiline_layout():
    w = extract_portfolio_weights(PORTFOLIO_TWEET_X_LAYOUT)
    assert w is not None
    assert w["SNDK"] == 12.0
    assert w["CGNX"] == 11.0
    assert "CASH" not in w


def test_refine_hypothetical_not_sell():
    signal = TradeSignal(
        action="sell",
        symbol="SNDK",
        target_weight_pct=0.0,
        confidence=0.95,
        reasoning="misread",
    )
    out = refine_trade_signal(HYPOTHETICAL, signal)
    assert out.action == "hold"
    assert out.symbol == "SNDK"


def test_refine_stop_flat_not_sell():
    signal = TradeSignal(action="sell", symbol="SNDK", confidence=0.95, reasoning="misread")
    out = refine_trade_signal(STOP_FLAT, signal)
    assert out.action == "stop_update"
    assert out.symbol == "SNDK"


def test_combined_hypothetical_and_portfolio_prefers_sync():
    from src.config import AppConfig
    from src.llm_parser import LLMParser

    parser = LLMParser(AppConfig(llm_cache_only=True))
    signal = parser.parse(FULL_EOD_TWEET, {})
    assert signal.action == "portfolio_sync"
    assert signal.raw["portfolio_weights"]["SNDK"] == 12.0
    assert signal.raw["portfolio_weights"]["CGNX"] == 11.0


BB_NEVER_STOPPED = (
    "Was never stopped on $BB. This remains where I'm at after cashing out "
    "$DOCN (it happens) & trimming $NBIS."
)

SNDK_STOPPED_FLAT_CASH = (
    "Stopped flat $SNDK, this is ugly mkt reversal. Back to 53% cash"
)

TWLO_OBSERVATION = "$TWLO back into pivot/20ema"


def test_refine_never_stopped_not_sell():
    signal = TradeSignal(action="hold", symbol="BB", confidence=0.8, reasoning="agy")
    out = refine_trade_signal(BB_NEVER_STOPPED, signal)
    assert out.action == "hold"
    assert out.symbol == "BB"


def test_refine_stopped_flat_with_cash_is_sell():
    signal = TradeSignal(
        action="stop_update",
        symbol="SNDK",
        stop_price=1721.16,
        confidence=0.95,
        reasoning="agy misread",
    )
    out = refine_trade_signal(SNDK_STOPPED_FLAT_CASH, signal)
    assert out.action == "sell"
    assert out.symbol == "SNDK"
    assert out.target_weight_pct == 0.0


def test_refine_back_into_pivot_not_buy():
    signal = TradeSignal(action="buy", symbol="TWLO", confidence=0.9, reasoning="agy")
    out = refine_trade_signal(TWLO_OBSERVATION, signal)
    assert out.action == "hold"
    assert out.symbol == "TWLO"


def test_refine_stopped_out_still_sells():
    signal = TradeSignal(action="hold", confidence=0.8, reasoning="agy")
    out = refine_trade_signal("Stopped $HOOD", signal)
    assert out.action == "sell"
    assert out.symbol == "HOOD"


def test_portfolio_sync_updates_state(tmp_path):
    mgr = PBAStateManager(tmp_path / "state.json")
    mgr.apply_signal(
        TradeSignal(
            action="sell",
            symbol="SNDK",
            target_weight_pct=0.0,
            confidence=0.95,
        )
    )
    assert mgr.get_weights().get("SNDK") == 0.0
    mgr.apply_signal(
        TradeSignal(
            action="portfolio_sync",
            confidence=0.99,
            raw={"portfolio_weights": extract_portfolio_weights(PORTFOLIO_TWEET)},
        )
    )
    assert mgr.get_weights()["SNDK"] == 12.0
    assert mgr.get_weights()["CGNX"] == 11.0
