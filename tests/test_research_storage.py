from binance_oi_momentum.models import Direction, SignalContext
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
