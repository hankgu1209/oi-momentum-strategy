from .models import Direction, MarketSnapshot, SignalContext


def infer_candidate_direction(snapshot: MarketSnapshot, thresholds: dict) -> Direction | None:
    long_thresholds = thresholds["long_return_thresholds"]
    short_thresholds = thresholds["short_return_thresholds"]

    returns = {
        10: snapshot.return_10s,
        30: snapshot.return_30s,
        60: snapshot.return_60s,
    }

    if any(value is not None and value >= long_thresholds[window] for window, value in returns.items()):
        return Direction.LONG

    if any(value is not None and value <= short_thresholds[window] for window, value in returns.items()):
        return Direction.SHORT

    return None


def should_probe(context: SignalContext, score_min: float) -> bool:
    return context.score >= score_min


def score_signal(
    *,
    direction: Direction,
    volume_ratio: float,
    taker_buy_ratio: float,
    oi_delta_pct: float,
    oi_value_to_volume_ratio: float = 0.0,
    spread_pct: float | None = None,
    estimated_slippage_pct: float | None = None,
) -> float:
    score = 0.0

    score += min(volume_ratio / 2.0, 3.0) * 15.0
    score += min(max(oi_delta_pct, 0.0) / 0.05, 3.0) * 15.0
    score += min(max(oi_value_to_volume_ratio, 0.0) / 0.25, 3.0) * 10.0

    if direction == Direction.LONG:
        score += max(taker_buy_ratio - 0.5, 0.0) * 120.0
    else:
        score += max((1.0 - taker_buy_ratio) - 0.5, 0.0) * 120.0

    if spread_pct is not None:
        score -= min(spread_pct / 0.003, 3.0) * 10.0

    if estimated_slippage_pct is not None:
        score -= min(estimated_slippage_pct / 0.005, 3.0) * 10.0

    return max(score, 0.0)


def passes_directional_flow(
    *,
    direction: Direction,
    volume_ratio: float,
    taker_buy_ratio: float,
    oi_delta_pct: float,
    oi_value_to_volume_ratio: float,
    signal_config: dict,
) -> bool:
    if volume_ratio < signal_config["volume_ratio_min"]:
        return False

    if oi_delta_pct < signal_config["oi_delta_pct_min"]:
        return False

    if oi_value_to_volume_ratio < signal_config["oi_value_to_volume_ratio_min"]:
        return False

    if direction == Direction.LONG:
        return taker_buy_ratio >= signal_config["taker_buy_ratio_min_for_long"]

    return (1.0 - taker_buy_ratio) >= signal_config["taker_sell_ratio_min_for_short"]
