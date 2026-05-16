from __future__ import annotations

import logging

from .models import Direction, PaperPosition, PositionStatus, SignalContext
from .storage import SQLiteStorage


logger = logging.getLogger(__name__)


class PaperExecutionEngine:
    """Paper trading engine used for validating signal quality without live orders."""

    def __init__(self, storage: SQLiteStorage, risk_config: dict, execution_config: dict, exit_config: dict):
        self.storage = storage
        self.risk_config = risk_config
        self.execution_config = execution_config
        self.exit_config = exit_config

    def open_probe_position(self, signal_id: int, context: SignalContext) -> int:
        notional = (
            self.risk_config["initial_equity_usdt"]
            * self.execution_config["probe_position_fraction"]
        )
        quantity = notional / context.trigger_price
        stop_loss_pct = self.exit_config["stop_loss_pct"]
        take_profit_pct = self.exit_config["take_profit_pct"]

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
            max_hold_seconds=self.exit_config["max_hold_seconds"],
        )
        return self.storage.open_position(position)

    def update_open_positions(self, symbol: str, price: float, timestamp_ms: int) -> bool:
        touched_position = False
        for position in self.storage.get_open_positions():
            if position.symbol != symbol or position.id is None:
                continue
            touched_position = True

            exit_reason = self._exit_reason(position, price, timestamp_ms)
            if exit_reason is None:
                continue

            pnl_usdt, pnl_pct = self._pnl(position, price)
            self.storage.close_position(
                position.id,
                exit_time_ms=timestamp_ms,
                exit_price=price,
                exit_reason=exit_reason,
                pnl_usdt=pnl_usdt,
                pnl_pct=pnl_pct,
            )
            logger.info(
                "paper position closed position_id=%s symbol=%s direction=%s "
                "reason=%s exit=%.8g pnl_usdt=%.4f pnl_pct=%.4f",
                position.id,
                position.symbol,
                position.direction.value,
                exit_reason,
                price,
                pnl_usdt,
                pnl_pct,
            )
        return touched_position

    def _exit_reason(
        self,
        position: PaperPosition,
        price: float,
        timestamp_ms: int,
    ) -> str | None:
        if position.direction == Direction.LONG:
            if price <= position.stop_loss_price:
                return "stop_loss"
            if price >= position.take_profit_price:
                return "take_profit"
        else:
            if price >= position.stop_loss_price:
                return "stop_loss"
            if price <= position.take_profit_price:
                return "take_profit"

        hold_seconds = (timestamp_ms - position.entry_time_ms) / 1000
        if hold_seconds >= position.max_hold_seconds:
            return "time_exit"

        return None

    @staticmethod
    def _pnl(position: PaperPosition, exit_price: float) -> tuple[float, float]:
        if position.direction == Direction.LONG:
            pnl_pct = (exit_price - position.entry_price) / position.entry_price
        else:
            pnl_pct = (position.entry_price - exit_price) / position.entry_price

        return position.notional_usdt * pnl_pct, pnl_pct
