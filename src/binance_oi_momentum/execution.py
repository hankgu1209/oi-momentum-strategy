from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from decimal import Decimal

from .binance import BinanceFuturesTradingClient
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
        plan = self.plan_probe_position(context)
        return self.record_probe_position(signal_id, context, plan)

    def record_probe_position(self, signal_id: int, context: SignalContext, plan: dict) -> int:
        position = PaperPosition(
            id=None,
            signal_id=signal_id,
            symbol=context.symbol,
            direction=context.direction,
            status=PositionStatus.OPEN,
            entry_time_ms=context.timestamp_ms,
            entry_price=context.trigger_price,
            quantity=plan["initial_quantity"],
            notional_usdt=plan["initial_notional"],
            stop_loss_price=plan["stop_loss_price"],
            take_profit_price=plan["take_profit_price"],
            initial_quantity=plan["initial_quantity"],
            remaining_quantity=plan["initial_quantity"],
            remaining_notional_usdt=plan["initial_notional"],
            scale_out_enabled=plan["scale_out_enabled"],
            trailing_active=False,
            take_profit_1_price=plan["take_profit_price"],
            take_profit_2_price=None if plan["scale_out_enabled"] else plan["take_profit_price"],
            take_profit_1_quantity=plan["initial_quantity"] * plan["first_take_profit_fraction"]
            if plan["scale_out_enabled"]
            else None,
            trailing_pivot_window=plan["trailing_pivot_window"],
            scale_in_pending=plan["scale_in_pending"],
            scale_in_entry_price=plan["scale_in_entry_price"] if plan["scale_in_pending"] else None,
            scale_in_fraction=plan["scale_in_fraction"] if plan["scale_in_pending"] else None,
            entry_price_1=context.trigger_price,
            notional_1_usdt=plan["initial_notional"],
            entry_price_2=plan["scale_in_entry_price"] if plan["scale_in_pending"] else None,
            notional_2_usdt=plan["scale_in_notional"] if plan["scale_in_pending"] else None,
            max_hold_seconds=self.exit_config["max_hold_seconds"],
        )
        return self.storage.open_position(position)

    def plan_probe_position(self, context: SignalContext) -> dict:
        has_breakout_bar = (
            context.breakout_bar_high is not None and context.breakout_bar_low is not None
        )
        default_initial_fraction = 0.3 if has_breakout_bar else 1.0
        initial_entry_fraction = float(
            self.execution_config.get("initial_entry_fraction", default_initial_fraction)
        )
        initial_entry_fraction = min(max(initial_entry_fraction, 0.01), 1.0)
        scale_in_fraction = 1.0 - initial_entry_fraction
        scale_out_enabled = bool(self.exit_config.get("scale_out_enabled", False))
        first_take_profit_fraction = float(self.exit_config.get("first_take_profit_fraction", 0.5))
        trailing_pivot_window = int(self.exit_config.get("trailing_pivot_window", 5))

        if context.direction == Direction.LONG:
            stop_loss_price = context.breakout_bar_low or context.trigger_price * (
                1 - self.exit_config["stop_loss_pct"]
            )
            scale_in_entry_price = context.trigger_price - (
                context.trigger_price - stop_loss_price
            ) * float(self.execution_config.get("scale_in_retrace_fraction", 0.4))
        else:
            stop_loss_price = context.breakout_bar_high or context.trigger_price * (
                1 + self.exit_config["stop_loss_pct"]
            )
            scale_in_entry_price = context.trigger_price + (
                stop_loss_price - context.trigger_price
            ) * float(self.execution_config.get("scale_in_retrace_fraction", 0.4))

        scale_in_pending = scale_in_fraction > 0 and scale_in_entry_price > 0
        total_notional = self._risk_capped_notional(
            entry_price=context.trigger_price,
            stop_loss_price=stop_loss_price,
            initial_entry_fraction=initial_entry_fraction,
            scale_in_entry_price=scale_in_entry_price if scale_in_pending else None,
            scale_in_fraction=scale_in_fraction if scale_in_pending else 0.0,
        )
        initial_notional = total_notional * initial_entry_fraction
        scale_in_notional = total_notional * scale_in_fraction
        quantity = initial_notional / context.trigger_price if context.trigger_price > 0 else 0.0
        take_profit_price = self._take_profit_price(
            entry_price=context.trigger_price,
            stop_loss_price=stop_loss_price,
            direction=context.direction,
        )
        return {
            "initial_entry_fraction": initial_entry_fraction,
            "scale_in_fraction": scale_in_fraction,
            "scale_out_enabled": scale_out_enabled,
            "first_take_profit_fraction": first_take_profit_fraction,
            "trailing_pivot_window": trailing_pivot_window,
            "stop_loss_price": stop_loss_price,
            "scale_in_entry_price": scale_in_entry_price,
            "scale_in_pending": scale_in_pending,
            "total_notional": total_notional,
            "initial_notional": initial_notional,
            "scale_in_notional": scale_in_notional,
            "initial_quantity": quantity,
            "take_profit_price": take_profit_price,
        }

    def _risk_capped_notional(
        self,
        *,
        entry_price: float,
        stop_loss_price: float,
        initial_entry_fraction: float,
        scale_in_entry_price: float | None,
        scale_in_fraction: float,
    ) -> float:
        initial_equity = float(self.risk_config["initial_equity_usdt"])
        max_notional = initial_equity * float(self.execution_config["probe_position_fraction"])
        risk_fraction = float(self.risk_config.get("account_risk_per_trade", 0.0))
        if initial_equity <= 0 or risk_fraction <= 0:
            return max_notional

        weighted_stop_loss_pct = (
            initial_entry_fraction * self._price_distance_pct(entry_price, stop_loss_price)
        )
        if scale_in_entry_price is not None and scale_in_fraction > 0:
            weighted_stop_loss_pct += scale_in_fraction * self._price_distance_pct(
                scale_in_entry_price,
                stop_loss_price,
            )
        if weighted_stop_loss_pct <= 0:
            return max_notional

        risk_budget = initial_equity * risk_fraction
        return min(max_notional, risk_budget / weighted_stop_loss_pct)

    @staticmethod
    def _price_distance_pct(entry_price: float, stop_loss_price: float) -> float:
        if entry_price <= 0:
            return 0.0
        return abs(entry_price - stop_loss_price) / entry_price

    def _take_profit_price(
        self,
        *,
        entry_price: float,
        stop_loss_price: float,
        direction: Direction,
    ) -> float:
        take_profit_r = self.exit_config.get("take_profit_r")
        if take_profit_r is None:
            take_profit_r = self.exit_config.get("first_take_profit_r")
        if take_profit_r is not None:
            risk_distance = abs(entry_price - stop_loss_price)
            if risk_distance > 0:
                r_multiple = max(float(take_profit_r), 0.0)
                if direction == Direction.LONG:
                    return entry_price + risk_distance * r_multiple
                return entry_price - risk_distance * r_multiple

        take_profit_pct = self.exit_config["take_profit_pct"]
        if direction == Direction.LONG:
            return entry_price * (1 + take_profit_pct)
        return entry_price * (1 - take_profit_pct)

    def has_open_position(self, symbol: str) -> bool:
        return any(position.symbol == symbol for position in self.storage.get_open_positions())

    def update_position_kline(self, kline: KlineClosed) -> None:
        for position in self.storage.get_open_positions():
            if position.symbol != kline.symbol or position.id is None:
                continue

            exit_decision = self._kline_exit_decision(position, kline)
            if exit_decision is not None:
                exit_reason, exit_price = exit_decision
                self._close_position(position, kline.close_time_ms, exit_price, exit_reason)
                continue

            if self._should_scale_in_on_kline(position, kline):
                self._mark_scale_in(position, kline.close_time_ms)
                position = self.storage.get_position(position.id) or position

            if self._should_take_profit_1_on_kline(position, kline):
                target = position.take_profit_1_price or position.take_profit_price
                self._mark_first_take_profit(position, target, kline.close_time_ms)
                continue

            if not position.trailing_active:
                continue

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
            trailing_stop = self._effective_trailing_stop(position, pivot)
            self.storage.update_trailing_stop(position.id, trailing_stop)
            logger.info(
                "paper position trailing stop updated position_id=%s symbol=%s "
                "direction=%s trailing_stop=%.8g raw_pivot=%.8g",
                position.id,
                position.symbol,
                position.direction.value,
                trailing_stop,
                pivot,
            )

    def update_closed_kline(self, kline: KlineClosed) -> None:
        self.update_position_kline(kline)

    def _kline_exit_decision(
        self,
        position: PaperPosition,
        kline: KlineClosed,
    ) -> tuple[str, float] | None:
        if position.trailing_active and position.trailing_stop_price is not None:
            trailing_is_more_protective = (
                position.trailing_stop_price >= position.stop_loss_price
                if position.direction == Direction.LONG
                else position.trailing_stop_price <= position.stop_loss_price
            )
            if trailing_is_more_protective and self._trailing_stop_hit_on_kline(
                position,
                kline,
                position.trailing_stop_price,
            ):
                return "trailing_pivot", position.trailing_stop_price

        if position.direction == Direction.LONG:
            if kline.low <= position.stop_loss_price:
                return "stop_loss", position.stop_loss_price
            if not position.scale_out_enabled and kline.high >= position.take_profit_price:
                return "take_profit", position.take_profit_price
        else:
            if kline.high >= position.stop_loss_price:
                return "stop_loss", position.stop_loss_price
            if not position.scale_out_enabled and kline.low <= position.take_profit_price:
                return "take_profit", position.take_profit_price

        hold_seconds = (kline.close_time_ms - position.entry_time_ms) / 1000
        if hold_seconds >= position.max_hold_seconds:
            return "time_exit", kline.close

        return None

    @staticmethod
    def _should_scale_in_on_kline(position: PaperPosition, kline: KlineClosed) -> bool:
        if not position.scale_in_pending or position.scale_in_entry_price is None:
            return False
        if position.trailing_active:
            return False
        if position.direction == Direction.LONG:
            return kline.low <= position.scale_in_entry_price
        return kline.high >= position.scale_in_entry_price

    def _mark_scale_in(self, position: PaperPosition, timestamp_ms: int) -> None:
        if position.id is None or position.scale_in_entry_price is None:
            return
        filled_fraction = 1.0 - float(position.scale_in_fraction or 0.0)
        if filled_fraction <= 0:
            return
        add_notional = (
            position.notional_2_usdt
            if position.notional_2_usdt is not None
            else position.notional_usdt * float(position.scale_in_fraction or 0.0) / filled_fraction
        )
        add_quantity = add_notional / position.scale_in_entry_price
        new_notional = position.notional_usdt + add_notional
        new_quantity = position.quantity + add_quantity
        new_entry_price = new_notional / new_quantity if new_quantity > 0 else position.entry_price
        take_profit_price = self._take_profit_price(
            entry_price=new_entry_price,
            stop_loss_price=position.stop_loss_price,
            direction=position.direction,
        )
        first_take_profit_quantity = (
            new_quantity * float(self.exit_config.get("first_take_profit_fraction", 0.5))
            if position.scale_out_enabled
            else None
        )
        self.storage.mark_scale_in_filled(
            position.id,
            timestamp_ms=timestamp_ms,
            entry_price=new_entry_price,
            quantity=new_quantity,
            notional_usdt=new_notional,
            remaining_quantity=new_quantity,
            remaining_notional_usdt=new_notional,
            take_profit_price=take_profit_price,
            take_profit_1_price=take_profit_price,
            take_profit_2_price=None if position.scale_out_enabled else take_profit_price,
            take_profit_1_quantity=first_take_profit_quantity,
        )
        logger.info(
            "paper position scale-in filled position_id=%s symbol=%s direction=%s "
            "price=%.8g entry=%.8g quantity=%.8g notional=%.4f",
            position.id,
            position.symbol,
            position.direction.value,
            position.scale_in_entry_price,
            new_entry_price,
            new_quantity,
            new_notional,
        )

    def _should_take_profit_1_on_kline(self, position: PaperPosition, kline: KlineClosed) -> bool:
        if not position.scale_out_enabled or position.trailing_active:
            return False
        target = position.take_profit_1_price or position.take_profit_price
        if position.direction == Direction.LONG:
            return kline.high >= target
        return kline.low <= target

    def _close_position(
        self,
        position: PaperPosition,
        timestamp_ms: int,
        price: float,
        reason: str,
    ) -> None:
        if position.id is None:
            return
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
            exit_reason=reason,
            pnl_usdt=total_pnl_usdt,
            pnl_pct=total_pnl_pct,
        )
        logger.info(
            "paper position closed position_id=%s symbol=%s direction=%s "
            "reason=%s exit=%.8g pnl_usdt=%.4f pnl_pct=%.4f",
            position.id,
            position.symbol,
            position.direction.value,
            reason,
            price,
            total_pnl_usdt,
            total_pnl_pct,
        )

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
        trailing_stop = None if pivot is None else self._effective_trailing_stop(position, pivot)
        self.storage.mark_first_take_profit(
            position.id,
            timestamp_ms=timestamp_ms,
            exit_price=price,
            exit_quantity=exit_quantity,
            remaining_quantity=remaining_quantity,
            remaining_notional_usdt=remaining_notional,
            pnl_usdt=pnl_usdt,
            pnl_pct=pnl_pct,
            trailing_stop_price=trailing_stop,
        )
        logger.info(
            "paper position first take profit position_id=%s symbol=%s direction=%s "
            "price=%.8g quantity=%.8g remaining_quantity=%.8g trailing_stop=%s raw_pivot=%s pnl_usdt=%.4f",
            position.id,
            position.symbol,
            position.direction.value,
            price,
            exit_quantity,
            remaining_quantity,
            "none" if trailing_stop is None else f"{trailing_stop:.8g}",
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
    def _effective_trailing_stop(position: PaperPosition, raw_pivot: float) -> float:
        current_stop = position.trailing_stop_price
        if current_stop is None:
            return raw_pivot
        if position.direction == Direction.LONG:
            return max(current_stop, raw_pivot)
        return min(current_stop, raw_pivot)

    @staticmethod
    def _trailing_stop_hit_on_kline(
        position: PaperPosition,
        kline: KlineClosed,
        trailing_stop: float,
    ) -> bool:
        if position.direction == Direction.LONG:
            return kline.low <= trailing_stop
        return kline.high >= trailing_stop

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


class LiveExecutionEngine:
    """Places real Binance futures orders after the scanner and risk gate approve a signal."""

    def __init__(
        self,
        *,
        trading_client: BinanceFuturesTradingClient,
        planner: PaperExecutionEngine,
        execution_config: dict,
    ) -> None:
        self.trading_client = trading_client
        self.planner = planner
        self.execution_config = execution_config
        self.closed_klines: defaultdict[str, deque[KlineClosed]] = defaultdict(
            lambda: deque(maxlen=200)
        )
        if hasattr(self.planner, "closed_klines"):
            self.planner.closed_klines = self.closed_klines

    async def refresh_margin_equity(self) -> float:
        account = await self.trading_client.account()
        margin_balance = self._account_margin_balance(account)
        self.planner.risk_config["initial_equity_usdt"] = margin_balance
        return margin_balance

    async def open_probe_position(self, signal_id: int, context: SignalContext) -> dict:
        if not bool(self.execution_config.get("live_trading_enabled", False)):
            raise RuntimeError("execution.live_trading_enabled must be true before live orders are sent")

        margin_balance = await self.refresh_margin_equity()
        plan = self.planner.plan_probe_position(context)
        min_notional = float(self.execution_config.get("live_min_order_notional_usdt", 5.0))
        max_notional = float(self.execution_config.get("live_max_order_notional_usdt", 0.0))
        notional = float(plan["initial_notional"])
        if notional < min_notional:
            raise RuntimeError(f"planned notional {notional:.4f} is below live minimum {min_notional:.4f}")
        if max_notional > 0 and notional > max_notional:
            raise RuntimeError(f"planned notional {notional:.4f} exceeds live cap {max_notional:.4f}")

        symbol_rules = await self._symbol_rules(context.symbol)
        lot_size = symbol_rules["LOT_SIZE"]
        price_filter = symbol_rules["PRICE_FILTER"]

        leverage = int(self.execution_config.get("leverage", 1))
        if leverage > 1:
            await self.trading_client.change_leverage(context.symbol, leverage)

        quantity = self._round_down(
            plan["initial_quantity"],
            lot_size.get(
                "stepSize",
                str(self.execution_config.get("quantity_step_size", "0.001")),
            ),
        )
        if quantity <= 0:
            raise RuntimeError("planned quantity rounded to zero")
        min_qty = Decimal(str(lot_size.get("minQty", "0")))
        max_qty = Decimal(str(lot_size.get("maxQty", "0")))
        if quantity < min_qty:
            raise RuntimeError(
                f"planned quantity {self._decimal_str(quantity)} is below Binance minQty "
                f"{self._decimal_str(min_qty)} for {context.symbol}"
            )
        if max_qty > 0 and quantity > max_qty:
            raise RuntimeError(
                f"planned quantity {self._decimal_str(quantity)} exceeds Binance maxQty "
                f"{self._decimal_str(max_qty)} for {context.symbol}"
            )

        price_tick = price_filter.get("tickSize")
        stop_price = self._round_down(plan["stop_loss_price"], price_tick)
        take_profit_price = self._round_down(plan["take_profit_price"], price_tick)
        if stop_price <= 0 or take_profit_price <= 0:
            raise RuntimeError(f"invalid rounded exit price for {context.symbol}")

        side = "BUY" if context.direction == Direction.LONG else "SELL"
        position_side = self._position_side(context.direction)

        entry_order = await self.trading_client.new_order(
            symbol=context.symbol,
            side=side,
            type=str(self.execution_config.get("order_type", "MARKET")).upper(),
            quantity=self._decimal_str(quantity),
            **position_side,
        )
        protection_error = None
        stop_order = None
        take_profit_order = None
        try:
            stop_order, take_profit_order = await self._place_protection_orders(
                symbol=context.symbol,
                direction=context.direction,
                quantity=quantity,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                symbol_rules=symbol_rules,
            )
        except Exception as exc:
            protection_error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "live protection order placement failed signal_id=%s symbol=%s error=%s",
                signal_id,
                context.symbol,
                protection_error,
            )
        logger.info(
            "live orders placed signal_id=%s symbol=%s direction=%s quantity=%s notional=%.4f",
            signal_id,
            context.symbol,
            context.direction.value,
            self._decimal_str(quantity),
            notional,
        )
        return {
            "entry_order": entry_order,
            "stop_order": stop_order,
            "take_profit_order": take_profit_order,
            "plan": plan,
            "margin_balance_usdt": margin_balance,
            "symbol_rules": symbol_rules,
            "protection_error": protection_error,
        }

    async def update_position_kline(self, kline: KlineClosed) -> None:
        """Apply the paper state machine using real orders for every action."""
        if kline.is_closed:
            self.closed_klines[kline.symbol].append(kline)

        for position in self.planner.storage.get_open_positions():
            if position.symbol != kline.symbol or position.id is None:
                continue

            exit_decision = self.planner._kline_exit_decision(position, kline)
            if exit_decision is not None:
                reason, price = exit_decision
                if reason in {"trailing_pivot", "time_exit"}:
                    await self._market_close(position, reason)
                else:
                    # The pre-placed Binance protection order owns SL/TP execution.
                    # Reconcile the local row only after the exchange position is gone.
                    await self._reconcile_position(position)
                continue

            if self.planner._should_scale_in_on_kline(position, kline):
                await self._scale_in(position, kline.close_time_ms)
                position = self.planner.storage.get_position(position.id) or position

            if self.planner._should_take_profit_1_on_kline(position, kline):
                # Binance owns the conditional TP1 fill. Once it is hit, mark the
                # local state and leave only the remaining quantity for trailing.
                if not await self._exchange_position_is_reduced(position):
                    continue
                target = position.take_profit_1_price or position.take_profit_price
                self.planner._mark_first_take_profit(position, target, kline.close_time_ms)
                position = self.planner.storage.get_position(position.id) or position
                await self._replace_protection_orders(position)
                continue

            if not position.trailing_active or not kline.is_closed:
                continue
            pivot = self._pivot_stop(position, exclude_latest=True)
            if pivot is None:
                continue
            trailing_stop = self._effective_trailing_stop(position, pivot)
            self.planner.storage.update_trailing_stop(position.id, trailing_stop)
            logger.info(
                "live position trailing stop updated position_id=%s symbol=%s "
                "trailing_stop=%.8g",
                position.id,
                position.symbol,
                trailing_stop,
            )

    async def _scale_in(self, position: PaperPosition, timestamp_ms: int) -> None:
        if position.id is None or position.scale_in_entry_price is None:
            return
        filled_fraction = 1.0 - float(position.scale_in_fraction or 0.0)
        if filled_fraction <= 0:
            return
        add_notional = position.notional_2_usdt or (
            position.notional_usdt * float(position.scale_in_fraction or 0.0) / filled_fraction
        )
        rules = await self._symbol_rules(position.symbol)
        step = rules["LOT_SIZE"].get("stepSize")
        quantity = self._round_down(add_notional / position.scale_in_entry_price, step)
        if quantity <= 0:
            raise RuntimeError(f"scale-in quantity rounded to zero for {position.symbol}")
        side = "BUY" if position.direction == Direction.LONG else "SELL"
        order = await self.trading_client.new_order(
            symbol=position.symbol,
            side=side,
            type="MARKET",
            quantity=self._decimal_str(quantity),
            **self._position_side(position.direction),
        )
        fill_price = float(order.get("avgPrice") or position.scale_in_entry_price)
        new_notional = position.notional_usdt + float(quantity) * fill_price
        new_quantity = position.quantity + float(quantity)
        new_entry = new_notional / new_quantity
        stop = position.stop_loss_price
        tp = self.planner._take_profit_price(
            entry_price=new_entry,
            stop_loss_price=stop,
            direction=position.direction,
        )
        first_qty = (
            new_quantity * float(self.planner.exit_config.get("first_take_profit_fraction", 0.5))
            if position.scale_out_enabled else None
        )
        self.planner.storage.mark_scale_in_filled(
            position.id,
            timestamp_ms=timestamp_ms,
            entry_price=new_entry,
            quantity=new_quantity,
            notional_usdt=new_notional,
            remaining_quantity=new_quantity,
            remaining_notional_usdt=new_notional,
            take_profit_price=tp,
            take_profit_1_price=tp,
            take_profit_2_price=None if position.scale_out_enabled else tp,
            take_profit_1_quantity=first_qty,
        )
        updated = self.planner.storage.get_position(position.id)
        if updated is not None:
            await self._replace_protection_orders(updated)
        logger.info(
            "live position scale-in filled position_id=%s symbol=%s quantity=%s price=%.8g",
            position.id,
            position.symbol,
            self._decimal_str(quantity),
            fill_price,
        )

    async def _market_close(self, position: PaperPosition, reason: str) -> None:
        if position.id is None:
            return
        await self._cancel_algo_orders(position.symbol, position.direction)
        rules = await self._symbol_rules(position.symbol)
        quantity = self._round_down(
            position.remaining_quantity or position.quantity,
            rules["LOT_SIZE"].get("stepSize"),
        )
        if quantity <= 0:
            await self._reconcile_position(position)
            return
        side = "SELL" if position.direction == Direction.LONG else "BUY"
        result = await self.trading_client.new_order(
            symbol=position.symbol,
            side=side,
            type="MARKET",
            quantity=self._decimal_str(quantity),
            reduceOnly="true",
            **self._position_side(position.direction),
        )
        exit_price = float(result.get("avgPrice") or result.get("price") or 0)
        if exit_price <= 0:
            exit_price = position.entry_price
        pnl, pct = self.planner._pnl(
            position,
            exit_price,
            notional=position.remaining_notional_usdt or position.notional_usdt,
        )
        total_pnl = pnl + (position.take_profit_1_pnl_usdt or 0.0)
        self.planner.storage.close_position(
            position.id,
            exit_time_ms=int(result.get("updateTime") or 0) or int(time.time() * 1000),
            exit_price=exit_price,
            exit_reason=reason,
            pnl_usdt=total_pnl,
            pnl_pct=total_pnl / position.notional_usdt if position.notional_usdt else pct,
        )
        logger.info(
            "live position closed position_id=%s symbol=%s reason=%s exit=%.8g pnl_usdt=%.4f",
            position.id,
            position.symbol,
            reason,
            exit_price,
            total_pnl,
        )

    async def _reconcile_position(self, position: PaperPosition) -> None:
        account = await self.trading_client.account()
        raw = next(
            (
                row for row in account.get("positions", [])
                if row.get("symbol") == position.symbol
                and row.get("positionSide") == self._position_side_value(position.direction)
            ),
            None,
        )
        if raw is not None and abs(float(raw.get("positionAmt") or 0)) > 0:
            return
        trades = await self.trading_client.user_trades(symbol=position.symbol, limit=1000)
        exit_side = "SELL" if position.direction == Direction.LONG else "BUY"
        tp1_time = int(position.take_profit_1_time_ms or 0)
        exits = [
            trade for trade in trades
            if trade.get("positionSide") == self._position_side_value(position.direction)
            and trade.get("side") == exit_side
            and int(trade.get("time") or 0)
            > (tp1_time if tp1_time else position.entry_time_ms)
        ]
        if not exits:
            return
        exit_quantity = sum(float(trade.get("qty") or 0) for trade in exits)
        if exit_quantity <= 0:
            return
        exit_price = sum(
            float(trade.get("price") or 0) * float(trade.get("qty") or 0)
            for trade in exits
        ) / exit_quantity
        realized_pnl = sum(float(trade.get("realizedPnl") or 0) for trade in exits)
        exit_time = max(int(trade.get("time") or 0) for trade in exits)
        total_pnl = realized_pnl + (position.take_profit_1_pnl_usdt or 0.0)
        self.planner.storage.close_position(
            position.id,
            exit_time_ms=exit_time,
            exit_price=exit_price,
            exit_reason="exchange_protection",
            pnl_usdt=total_pnl,
            pnl_pct=total_pnl / position.notional_usdt if position.notional_usdt else 0.0,
        )
        logger.info(
            "live position reconciled from exchange position_id=%s symbol=%s "
            "exit_price=%.8g realized_pnl=%.4f",
            position.id,
            position.symbol,
            exit_price,
            total_pnl,
        )

    async def _exchange_position_is_reduced(self, position: PaperPosition) -> bool:
        account = await self.trading_client.account()
        expected_side = self._position_side_value(position.direction)
        raw = next(
            (
                row for row in account.get("positions", [])
                if row.get("symbol") == position.symbol
                and row.get("positionSide") == expected_side
            ),
            None,
        )
        if raw is None:
            return False
        exchange_quantity = abs(float(raw.get("positionAmt") or 0))
        local_quantity = abs(float(position.remaining_quantity or position.quantity))
        return exchange_quantity < local_quantity - 1e-9

    async def _replace_protection_orders(self, position: PaperPosition) -> None:
        await self._cancel_algo_orders(position.symbol, position.direction)
        rules = await self._symbol_rules(position.symbol)
        stop = self._round_down(position.stop_loss_price, rules["PRICE_FILTER"].get("tickSize"))
        tp = self._round_down(
            position.take_profit_1_price or position.take_profit_price,
            rules["PRICE_FILTER"].get("tickSize"),
        )
        await self._place_protection_orders(
            symbol=position.symbol,
            direction=position.direction,
            quantity=self._round_down(
                position.remaining_quantity or position.quantity,
                rules["LOT_SIZE"].get("stepSize"),
            ),
            stop_price=stop,
            take_profit_price=tp,
            symbol_rules=rules,
            include_take_profit=not position.trailing_active,
        )

    async def _place_protection_orders(
        self, *, symbol: str, direction: Direction, quantity: Decimal,
        stop_price: Decimal, take_profit_price: Decimal,
        symbol_rules: dict[str, dict[str, str]],
        include_take_profit: bool = True,
    ) -> tuple[dict, dict | None]:
        exit_side = "SELL" if direction == Direction.LONG else "BUY"
        position_side = self._position_side(direction)
        stop = await self.trading_client.new_algo_order(
            algoType="CONDITIONAL", symbol=symbol, side=exit_side, type="STOP_MARKET",
            triggerPrice=self._decimal_str(stop_price), closePosition="true",
            workingType=str(self.execution_config.get("working_type", "MARK_PRICE")),
            **position_side,
        )
        tp = None
        if include_take_profit and bool(self.planner.exit_config.get("scale_out_enabled", False)):
            tp_quantity = self._round_down(
                quantity * Decimal(str(self.planner.exit_config.get("first_take_profit_fraction", 0.5))),
                symbol_rules["LOT_SIZE"].get("stepSize"),
            )
            if tp_quantity > 0:
                tp = await self.trading_client.new_algo_order(
                    algoType="CONDITIONAL", symbol=symbol, side=exit_side,
                    type="TAKE_PROFIT_MARKET", triggerPrice=self._decimal_str(take_profit_price),
                    quantity=self._decimal_str(tp_quantity), reduceOnly="true",
                    workingType=str(self.execution_config.get("working_type", "MARK_PRICE")),
                    **position_side,
                )
        elif include_take_profit:
            tp = await self.trading_client.new_algo_order(
                algoType="CONDITIONAL", symbol=symbol, side=exit_side,
                type="TAKE_PROFIT_MARKET", triggerPrice=self._decimal_str(take_profit_price),
                closePosition="true",
                workingType=str(self.execution_config.get("working_type", "MARK_PRICE")),
                **position_side,
            )
        return stop, tp

    async def _cancel_algo_orders(self, symbol: str, direction: Direction) -> None:
        orders = await self.trading_client.open_algo_orders(symbol=symbol)
        expected_side = "LONG" if direction == Direction.LONG else "SHORT"
        for order in orders:
            if order.get("positionSide", expected_side) != expected_side:
                continue
            algo_id = order.get("algoId") or order.get("orderId")
            if algo_id is not None:
                await self.trading_client.cancel_algo_order(symbol=symbol, algoId=algo_id)

    def _pivot_stop(self, position: PaperPosition, *, exclude_latest: bool = False) -> float | None:
        window = int(position.trailing_pivot_window or self.planner.exit_config.get("trailing_pivot_window", 5))
        history = list(self.closed_klines[position.symbol])
        if exclude_latest:
            history = history[:-1]
        if len(history) < window:
            return None
        selected = history[-window:]
        return (
            min(k.low for k in selected)
            if position.direction == Direction.LONG
            else max(k.high for k in selected)
        )

    @staticmethod
    def _effective_trailing_stop(position: PaperPosition, raw_pivot: float) -> float:
        current = position.trailing_stop_price
        if current is None:
            return raw_pivot
        return max(current, raw_pivot) if position.direction == Direction.LONG else min(current, raw_pivot)

    @staticmethod
    def _position_side_value(direction: Direction) -> str:
        return "LONG" if direction == Direction.LONG else "SHORT"

    async def _symbol_rules(self, symbol: str) -> dict[str, dict[str, str]]:
        exchange_info = await self.trading_client.exchange_info()
        for item in exchange_info.get("symbols", []):
            if item.get("symbol") != symbol:
                continue
            filters = {
                str(rule.get("filterType")): rule
                for rule in item.get("filters", [])
                if rule.get("filterType") in {"LOT_SIZE", "PRICE_FILTER"}
            }
            missing = {"LOT_SIZE", "PRICE_FILTER"} - filters.keys()
            if missing:
                raise RuntimeError(
                    f"Binance exchange info missing {', '.join(sorted(missing))} for {symbol}"
                )
            return filters
        raise RuntimeError(f"Binance exchange info did not contain symbol {symbol}")

    def _position_side(self, direction: Direction) -> dict[str, str]:
        if not bool(self.execution_config.get("hedge_mode", False)):
            return {}
        return {"positionSide": "LONG" if direction == Direction.LONG else "SHORT"}

    @staticmethod
    def _account_margin_balance(account: dict) -> float:
        total_margin_balance = account.get("totalMarginBalance")
        if total_margin_balance is not None:
            return float(total_margin_balance)
        for asset in account.get("assets", []):
            if asset.get("asset") == "USDT" and asset.get("marginBalance") is not None:
                return float(asset["marginBalance"])
        raise RuntimeError("Binance account response did not include USDT margin balance")

    @staticmethod
    def _round_down(value: float | Decimal, step_size: str | None) -> Decimal:
        if not step_size:
            return Decimal(str(value))
        step = Decimal(step_size)
        if step <= 0:
            return Decimal(str(value))
        value_decimal = Decimal(str(value))
        return (value_decimal // step) * step

    @staticmethod
    def _decimal_str(value: Decimal) -> str:
        return format(value.normalize(), "f")
