import pytest

from binance_oi_momentum.execution import PaperExecutionEngine
from binance_oi_momentum.models import CurrentOpenInterest, Direction, MarketSnapshot, OIContext, PriceTick
from binance_oi_momentum.scanner import MarketScanner
from binance_oi_momentum.storage import SQLiteStorage
from binance_oi_momentum.strategy import infer_candidate_direction, score_signal


def test_infer_long_candidate_direction() -> None:
    snapshot = MarketSnapshot("TESTUSDT", 1, 1.0, return_10s=0.01)
    thresholds = {
        "long_return_thresholds": {10: 0.008, 30: 0.015, 60: 0.025},
        "short_return_thresholds": {10: -0.008, 30: -0.015, 60: -0.025},
    }

    assert infer_candidate_direction(snapshot, thresholds) == Direction.LONG


def test_score_penalizes_wide_spread() -> None:
    clean_score = score_signal(
        direction=Direction.LONG,
        volume_ratio=3.0,
        taker_buy_ratio=0.65,
        oi_delta_pct=0.003,
        spread_pct=0.001,
        estimated_slippage_pct=0.001,
    )
    wide_spread_score = score_signal(
        direction=Direction.LONG,
        volume_ratio=3.0,
        taker_buy_ratio=0.65,
        oi_delta_pct=0.003,
        spread_pct=0.01,
        estimated_slippage_pct=0.001,
    )

    assert wide_spread_score < clean_score


def test_close_position_uses_directional_extreme_distance() -> None:
    long_distance = MarketScanner._close_position(
        low=100,
        high=110,
        close=109.9,
        direction=Direction.LONG,
    )
    short_distance = MarketScanner._close_position(
        low=100,
        high=110,
        close=100.1,
        direction=Direction.SHORT,
    )

    assert long_distance == pytest.approx((110 - 109.9) / 110)
    assert short_distance == pytest.approx((100.1 - 100) / 100)


def test_realtime_oi_context_compares_current_qty_to_latest_snapshot() -> None:
    context = MarketScanner._realtime_oi_context(
        current=CurrentOpenInterest("TESTUSDT", 1_700_000_060_000, 1_050_000),
        snapshot=OIContext(
            symbol="TESTUSDT",
            timestamp_ms=1_700_000_000_000,
            open_interest=1_000_000,
            open_interest_value_usdt=2_000_000,
            previous_open_interest=None,
            previous_open_interest_value_usdt=None,
        ),
        reference_price=2.0,
    )

    assert context.open_interest == 1_050_000
    assert context.previous_open_interest == 1_000_000
    assert context.delta_pct == pytest.approx(0.05)
    assert context.delta_value_usdt == pytest.approx(100_000)


def test_precheck_reject_records_liquidity_filter_failure(tmp_path) -> None:
    storage = SQLiteStorage(f"sqlite:///{tmp_path / 'events.sqlite3'}")
    scanner = MarketScanner(
        client=None,  # type: ignore[arg-type]
        storage=storage,
        config={
            "universe": {
                "min_24h_quote_volume": 10_000_000,
                "max_24h_quote_volume": 500_000_000,
            },
            "signal": {
                "primary_window_seconds": 60,
                "precheck_log_cooldown_seconds": 60,
            },
            "risk": {"initial_equity_usdt": 10_000},
            "execution": {"probe_position_fraction": 0.2},
            "exit": {
                "stop_loss_pct": 0.01,
                "take_profit_pct": 0.02,
                "max_hold_seconds": 900,
            },
        },
    )
    scanner.execution = PaperExecutionEngine(
        storage,
        risk_config={"initial_equity_usdt": 10_000},
        execution_config={"probe_position_fraction": 0.2},
        exit_config={
            "stop_loss_pct": 0.01,
            "take_profit_pct": 0.02,
            "max_hold_seconds": 900,
        },
    )
    state = scanner.states["TESTUSDT"]
    tick = PriceTick(
        symbol="TESTUSDT",
        timestamp_ms=1_700_000_000_000,
        price=1.1,
        open_24h=1.0,
        high_24h=1.2,
        low_24h=0.9,
        base_volume_24h=1_000,
        quote_volume_24h=1_000_000,
    )
    pending = scanner._build_pending_candidate(
        tick,
        MarketSnapshot(
            symbol="TESTUSDT",
            timestamp_ms=tick.timestamp_ms,
            price=tick.price,
            return_60s=0.1,
        ),
        Direction.LONG,
    )
    assert pending is not None

    scanner._record_precheck_reject(state, pending, "liquidity_filter_failed")

    with storage.connect() as conn:
        row = conn.execute("SELECT symbol, reject_reason FROM signal_checks").fetchone()

    assert row["symbol"] == "TESTUSDT"
    assert row["reject_reason"] == "liquidity_filter_failed"
