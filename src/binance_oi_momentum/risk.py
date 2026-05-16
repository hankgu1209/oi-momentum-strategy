from .models import RiskDecision, SignalContext


def evaluate_probe_risk(
    context: SignalContext,
    risk_config: dict,
    execution_config: dict,
    *,
    open_positions: int = 0,
    same_direction_positions: int = 0,
    daily_realized_pnl: float = 0.0,
) -> RiskDecision:
    max_spread = risk_config["max_spread_pct"]
    max_slippage = risk_config["max_estimated_slippage_pct"]
    initial_equity = risk_config.get("initial_equity_usdt", 0)

    if context.spread_pct is not None and context.spread_pct > max_spread:
        return RiskDecision(False, "spread_too_wide")

    if context.estimated_slippage_pct is not None and context.estimated_slippage_pct > max_slippage:
        return RiskDecision(False, "slippage_too_high")

    if open_positions >= risk_config["max_simultaneous_positions"]:
        return RiskDecision(False, "too_many_open_positions")

    if same_direction_positions >= risk_config["max_same_direction_positions"]:
        return RiskDecision(False, "too_many_same_direction_positions")

    if initial_equity > 0 and daily_realized_pnl <= -initial_equity * risk_config["max_daily_loss"]:
        return RiskDecision(False, "daily_loss_limit")

    return RiskDecision(
        allowed=True,
        reason="allowed",
        planned_position_fraction=execution_config["probe_position_fraction"],
    )
