from __future__ import annotations

import logging
from collections import defaultdict, deque

from .models import Direction, KlineClosed, PaperPosition, PositionStatus, SignalContext
from .storage import SQLiteStorage


logger = logging.getLogger(__name__)


class PaperExecutionEngine:
    """Paper trading engine used for validating signal quality without live orders."""

    def __init__(self, storage: SQLiteStorage, risk_config: dict, execution_config: dict, exit_config: dict):
        self.storage = storage
        self.risk_config = risk_config
        self.execution_config = execution_config
        self.exit_config = exit_config
        self.closed_klines: defaultdict[str, deque[KlineClosed]] = defaultdict(
            lambda: deque(maxlen=200)
        )

    def open_probe_position(self, signal_id: int, context: SignalContext) -> int:
        notional = (
            self.risk_config["initial_equity_usdt"]
            * self.execution_config["probe_position_fraction"]
        )
        quantity = notional / context.trigger_price
        stop_loss_pct = self.exit_config["stop_loss_pct"]
        take_profit_pct = self.exit_config["take_profit_pct"]
        scale_out_enabled = bool(self.exit_config.get("scale_out_enabled", False))
        first_take_profit_fraction = float(self.exit_config.get("first_take_profit_fraction", 0.5))
        trailing_pivot_window = int(self.exit_config.get("trailing_pivot_window", 5))

        if context.direction == Direction.LONG:
            stop_loss_price = context.trigger_price * (1 - stop_loss_pct)
            take_profit_price = context.trigger_price * (1 + take_profit_pct)
        else:
            stop_loss_price = context.trigger_price * (1 + stop_loss_pct)
            take_profit_price = context.trigger_price * (1 - take_profit_pct)

        position = PaperPosition(
            id=None,
            signal_id=signal_id,
            symbol=context.symbol,
            direction=context.direction,
            status=PositionStatus.OPEN,
            entry_time_ms=context.timestamp_ms,
            entry_price=context.trigger_price,
            quantity=quantity,
            notional_usdt=notional,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            initial_quantity=quantity,
            remaining_quantity=quantity,
            remaining_notional_usdt=notional,
            scale_out_enabled=scale_out_enabled,
            trailing_active=False,
            take_profit_1_price=take_profit_price,
            take_profit_2_price=None if scale_out_enabled else take_profit_price,
            take_profit_1_quantity=quantity * first_take_profit_fraction
            if scale_out_enabled
            else None,
            trailing_pivot_window=trailing_pivot_window,
            max_hold_seconds=self.exit_config["max_hold_seconds"],
        )
        return self.storage.open_position(position)

    def update_open_positions(self, symbol: str, price: float, timestamp_ms: int) -> bool:
        touched_position = False
        for position in self.storage.get_open_positions():
            if position.symbol != symbol or position.id is None:
                continue
            touched_position = True

            if self._should_take_profit_1(position, price):
                self._mark_first_take_profit(position, price, timestamp_ms)
                continue

            exit_reason = self._exit_reason(position, price, timestamp_ms)
            if exit_reason is None:
                continue

            pnl_usdt, pnl_pct = self._pnl(
                position,
                price,
                notional=position.remaining_notional_usdt or position.notional_usdt,
            )
            total_pnl_usdt = pnl_usdt + (position.take_profit_1_pnl_usdt or 0.0)
            total_pnl_pct = total_pnl_usdt / position.notional_usdt if position.notional_usdt else pnl_pct
            self.storage.close_position(
                position.id,
                exit_time_ms=timestamp_ms,
                exit_price=price,
                exit_reason=exit_reason,
                pnl_usdt=total_pnl_usdt,
                pnl_pct=total_pnl_pct,
            )
            logger.info(
                "paper position closed position_id=%s symbol=%s direction=%s "
                "reason=%s exit=%.8g pnl_usdt=%.4f pnl_pct=%.4f",
                position.id,
                position.symbol,
                position.direction.value,
                exit_reason,
                price,
                total_pnl_usdt,
                total_pnl_pct,
            )
        return touched_position

    def update_closed_kline(self, kline: KlineClosed) -> None:
        if not kline.is_closed:
            return
        self.closed_klines[kline.symbol].append(kline)
        for position in self.storage.get_open_positions():
            if position.symbol != kline.symbol or position.id is None:
                continue
            if not position.trailing_active:
                continue

            pivot = self._pivot_stop(position, exclude_latest=True)
            if pivot is None:
                continue
            self.storage.update_trailing_stop(position.id, pivot)

            if self._trailing_stop_hit(position, kline.close, pivot):
                pnl_usdt, pnl_pct = self._pnl(
                    position,
                    kline.close,
                    notional=position.remaining_notional_usdt or position.notional_usdt,
                )
                total_pnl_usdt = pnl_usdt + (position.take_profit_1_pnl_usdt or 0.0)
                total_pnl_pct = (
                    total_pnl_usdt / position.notional_usdt
                    if position.notional_usdt
                    else pnl_pct
                )
                self.storage.close_position(
                    position.id,
                    exit_time_ms=kline.close_time_ms,
                    exit_price=kline.close,
                    exit_reason="trailing_pivot",
                    pnl_usdt=total_pnl_usdt,
                    pnl_pct=total_pnl_pct,
                )
                logger.info(
                    "paper position closed by trailing pivot position_id=%s symbol=%s "
                    "direction=%s close=%.8g pivot=%.8g pnl_usdt=%.4f",
                    position.id,
                    position.symbol,
                    position.direction.value,
                    kline.close,
                    pivot,
                    total_pnl_usdt,
                )

    def _exit_reason(
        self,
        position: PaperPosition,
        price: float,
        timestamp_ms: int,
    ) -> str | None:
        if position.direction == Direction.LONG:
            if price <= position.stop_loss_price:
                return "stop_loss"
            if not position.scale_out_enabled and price >= position.take_profit_price:
                return "take_profit"
        else:
            if price >= position.stop_loss_price:
                return "stop_loss"
            if not position.scale_out_enabled and price <= position.take_profit_price:
                return "take_profit"

        hold_seconds = (timestamp_ms - position.entry_time_ms) / 1000
        if hold_seconds >= position.max_hold_seconds:
            return "time_exit"

        return None

    def _should_take_profit_1(self, position: PaperPosition, price: float) -> bool:
        if not position.scale_out_enabled or position.trailing_active:
            return False
        target = position.take_profit_1_price or position.take_profit_price
        if position.direction == Direction.LONG:
            return price >= target
        return price <= target

    def _mark_first_take_profit(
        self,
        position: PaperPosition,
        price: float,
        timestamp_ms: int,
    ) -> None:
        if position.id is None:
            return
        first_fraction = float(self.exit_config.get("first_take_profit_fraction", 0.5))
        first_fraction = min(max(first_fraction, 0.0), 1.0)
        initial_quantity = position.initial_quantity or position.quantity
        exit_quantity = initial_quantity * first_fraction
        remaining_quantity = max(initial_quantity - exit_quantity, 0.0)
        remaining_notional = position.notional_usdt * (remaining_quantity / initial_quantity)
        exit_notional = position.notional_usdt - remaining_notional
        pnl_usdt, pnl_pct = self._pnl(position, price, notional=exit_notional)
        pivot = self._pivot_stop(position)
        self.storage.mark_first_take_profit(
            position.id,
            timestamp_ms=timestamp_ms,
            exit_price=price,
            exit_quantity=exit_quantity,
            remaining_quantity=remaining_quantity,
            remaining_notional_usdt=remaining_notional,
            pnl_usdt=pnl_usdt,
            pnl_pct=pnl_pct,
            trailing_stop_price=pivot,
        )
        logger.info(
            "paper position first take profit position_id=%s symbol=%s direction=%s "
            "price=%.8g quantity=%.8g remaining_quantity=%.8g pivot=%s pnl_usdt=%.4f",
            position.id,
            position.symbol,
            position.direction.value,
            price,
            exit_quantity,
            remaining_quantity,
            "none" if pivot is None else f"{pivot:.8g}",
            pnl_usdt,
        )

    def _pivot_stop(self, position: PaperPosition, *, exclude_latest: bool = False) -> float | None:
        window = int(position.trailing_pivot_window or self.exit_config.get("trailing_pivot_window", 5))
        if window <= 0:
            return None
        history = list(self.closed_klines[position.symbol])
        if exclude_latest:
            history = history[:-1]
        klines = history[-window:]
        if len(klines) < window:
            return None
        if position.direction == Direction.LONG:
            return min(kline.low for kline in klines)
        return max(kline.high for kline in klines)

    @staticmethod
    def _trailing_stop_hit(position: PaperPosition, price: float, pivot: float) -> bool:
        if position.direction == Direction.LONG:
            return price < pivot
        return price > pivot

    @staticmethod
    def _pnl(
        position: PaperPosition,
        exit_price: float,
        *,
        notional: float,
    ) -> tuple[float, float]:
        if position.direction == Direction.LONG:
            pnl_pct = (exit_price - position.entry_price) / position.entry_price
        else:
            pnl_pct = (position.entry_price - exit_price) / position.entry_price

        return notional * pnl_pct, pnl_pct
