from binance_oi_momentum.models import Direction, MarketSnapshot
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

