from binance_oi_momentum.execution import PaperExecutionEngine
from binance_oi_momentum.models import Direction, KlineClosed, SignalContext
from binance_oi_momentum.storage import SQLiteStorage


def test_records_signal_and_open_position_limits(tmp_path) -> None:
    storage = SQLiteStorage(f"sqlite:///{tmp_path / 'events.sqlite3'}")
    context = SignalContext(
        symbol="TESTUSDT",
        direction=Direction.LONG,
        timestamp_ms=1_700_000_000_000,
        trigger_price=1.0,
        price_change_pct=2.0,
        window_seconds=30,
        quote_volume_usdt=100_000,
        average_quote_volume_usdt=30_000,
        volume_ratio=3.0,
        taker_buy_ratio=0.65,
        taker_sell_ratio=0.35,
        open_interest=1_000_000,
        open_interest_value_usdt=2_000_000,
        oi_delta_pct=0.05,
        oi_delta_value_usdt=25_000,
        oi_value_to_volume_ratio=0.25,
        spread_pct=None,
        estimated_slippage_pct=None,
        score=90.0,
    )

    signal_id = storage.record_signal(
        context,
        risk_allowed=True,
        risk_reason="allowed",
        raw={"source": "test"},
    )

    assert signal_id == 1
    assert storage.last_signal_time_ms("TESTUSDT") == context.timestamp_ms
    assert storage.count_open_positions() == 0


def test_records_signal_check_log(tmp_path) -> None:
    storage = SQLiteStorage(f"sqlite:///{tmp_path / 'events.sqlite3'}")

    check_id = storage.record_signal_check(
        {
            "checked_at_ms": 1_700_000_060_000,
            "candidate_detected_at_ms": 1_700_000_000_000,
            "symbol": "TESTUSDT",
            "direction": "long",
            "window_seconds": 60,
            "candidate_trigger_price": 1.02,
            "candle_close_time_ms": 1_700_000_059_999,
            "trigger_price": 1.03,
            "price_change_pct": 2.1,
            "quote_volume_usdt": 120_000,
            "average_quote_volume_usdt": 40_000,
            "volume_ratio": 3.0,
            "taker_buy_ratio": 0.66,
            "taker_sell_ratio": 0.34,
            "open_interest": 1_050_000,
            "previous_open_interest": 1_000_000,
            "open_interest_value_usdt": 1_081_500,
            "oi_delta_pct": 0.05,
            "oi_delta_value_usdt": 51_500,
            "oi_value_to_volume_ratio": 0.43,
            "close_position": 0.97,
            "score": 88,
            "passed": True,
            "reject_reason": "",
            "raw": {"source": "test"},
        }
    )

    assert check_id == 1
    with storage.connect() as conn:
        row = conn.execute("SELECT * FROM signal_checks WHERE id = ?", (check_id,)).fetchone()

    assert row["symbol"] == "TESTUSDT"
    assert row["passed"] == 1
    assert row["price_change_pct"] == 2.1


def test_scale_out_then_trailing_pivot_close(tmp_path) -> None:
    storage = SQLiteStorage(f"sqlite:///{tmp_path / 'events.sqlite3'}")
    context = SignalContext(
        symbol="TESTUSDT",
        direction=Direction.LONG,
        timestamp_ms=1_700_000_000_000,
        trigger_price=100.0,
        price_change_pct=2.0,
        window_seconds=60,
        quote_volume_usdt=100_000,
        average_quote_volume_usdt=30_000,
        volume_ratio=3.0,
        taker_buy_ratio=0.65,
        taker_sell_ratio=0.35,
        open_interest=1_000_000,
        open_interest_value_usdt=2_000_000,
        oi_delta_pct=0.05,
        oi_delta_value_usdt=25_000,
        oi_value_to_volume_ratio=0.25,
        spread_pct=None,
        estimated_slippage_pct=None,
        score=90.0,
    )
    signal_id = storage.record_signal(
        context,
        risk_allowed=True,
        risk_reason="allowed",
        raw={"source": "test"},
    )
    engine = PaperExecutionEngine(
        storage,
        risk_config={"initial_equity_usdt": 10_000},
        execution_config={"probe_position_fraction": 0.2},
        exit_config={
            "stop_loss_pct": 0.01,
            "take_profit_pct": 0.02,
            "max_hold_seconds": 900,
            "scale_out_enabled": True,
            "first_take_profit_fraction": 0.5,
            "trailing_pivot_window": 5,
        },
    )
    position_id = engine.open_probe_position(signal_id, context)

    engine.update_open_positions("TESTUSDT", 102.0, 1_700_000_010_000)
    position = storage.get_open_positions()[0]
    assert position.id == position_id
    assert position.trailing_active is True
    assert position.remaining_notional_usdt == 1_000

    for index in range(5):
        engine.update_closed_kline(
            KlineClosed(
                symbol="TESTUSDT",
                interval="1m",
                open_time_ms=1_700_000_020_000 + index * 60_000,
                close_time_ms=1_700_000_079_999 + index * 60_000,
                open=102.0,
                high=104.0,
                low=101.0,
                close=103.0,
                is_closed=True,
            )
        )

    engine.update_closed_kline(
        KlineClosed(
            symbol="TESTUSDT",
            interval="1m",
            open_time_ms=1_700_000_400_000,
            close_time_ms=1_700_000_459_999,
            open=102.0,
            high=102.5,
            low=99.0,
            close=100.5,
            is_closed=True,
        )
    )

    with storage.connect() as conn:
        row = conn.execute("SELECT * FROM paper_positions WHERE id = ?", (position_id,)).fetchone()

    assert row["status"] == "closed"
    assert row["exit_reason"] == "trailing_pivot"
    assert row["take_profit_1_pnl_usdt"] > 0
