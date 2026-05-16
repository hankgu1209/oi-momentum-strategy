from dataclasses import dataclass
from enum import StrEnum


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    timestamp_ms: int
    price: float
    return_10s: float | None = None
    return_30s: float | None = None
    return_60s: float | None = None


@dataclass(frozen=True)
class SignalContext:
    symbol: str
    direction: Direction
    timestamp_ms: int
    trigger_price: float
    price_change_pct: float
    window_seconds: int
    quote_volume_usdt: float
    average_quote_volume_usdt: float
    volume_ratio: float
    taker_buy_ratio: float
    taker_sell_ratio: float
    open_interest: float
    open_interest_value_usdt: float
    oi_delta_pct: float
    oi_delta_value_usdt: float
    oi_value_to_volume_ratio: float
    spread_pct: float | None
    estimated_slippage_pct: float | None
    score: float


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    planned_position_fraction: float = 0.0


@dataclass(frozen=True)
class PriceTick:
    symbol: str
    timestamp_ms: int
    price: float
    open_24h: float
    high_24h: float
    low_24h: float
    base_volume_24h: float
    quote_volume_24h: float


@dataclass(frozen=True)
class OIContext:
    symbol: str
    timestamp_ms: int
    open_interest: float
    open_interest_value_usdt: float
    previous_open_interest: float | None
    previous_open_interest_value_usdt: float | None

    @property
    def delta_pct(self) -> float:
        if not self.previous_open_interest or self.previous_open_interest <= 0:
            return 0.0
        return (self.open_interest - self.previous_open_interest) / self.previous_open_interest

    @property
    def delta_value_usdt(self) -> float:
        if self.previous_open_interest_value_usdt is None:
            return 0.0
        return self.open_interest_value_usdt - self.previous_open_interest_value_usdt


@dataclass(frozen=True)
class KlineVolumeContext:
    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    quote_volume_usdt: float
    average_quote_volume_usdt: float
    volume_ratio: float
    taker_buy_quote_volume_usdt: float
    taker_sell_quote_volume_usdt: float
    taker_buy_ratio: float
    taker_sell_ratio: float


@dataclass(frozen=True)
class PaperPosition:
    id: int | None
    signal_id: int
    symbol: str
    direction: Direction
    status: PositionStatus
    entry_time_ms: int
    entry_price: float
    quantity: float
    notional_usdt: float
    stop_loss_price: float
    take_profit_price: float
    max_hold_seconds: int
    exit_time_ms: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_usdt: float | None = None
    pnl_pct: float | None = None
