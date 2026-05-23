from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from .binance import BinanceMarketClient
from .config import load_config
from .execution import PaperExecutionEngine
from .models import CurrentOpenInterest, Direction, MarketSnapshot, OIContext, PriceTick, SignalContext
from .risk import evaluate_probe_risk
from .storage import SQLiteStorage
from .strategy import infer_candidate_direction, score_signal


logger = logging.getLogger(__name__)


@dataclass
class SymbolState:
    ticks: deque[PriceTick] = field(default_factory=lambda: deque(maxlen=600))
    last_signal_at_ms: int | None = None
    last_precheck_reject_at_ms: int | None = None
    pending_candidate: PendingCandidate | None = None


@dataclass(frozen=True)
class PendingCandidate:
    tick: PriceTick
    direction: Direction
    price_change_pct: float
    window_seconds: int
    candle_close_time_ms: int
    evaluate_after_ms: int


@dataclass(frozen=True)
class SignalEvaluation:
    context: SignalContext | None
    log: dict


def _safe_float(value: float | None) -> str:
    if value is None:
        return "none"
    return f"{value:.4f}"


class MarketScanner:
    """Realtime research scanner for Binance USDT-M low-liquidity momentum."""

    def __init__(
        self,
        *,
        client: BinanceMarketClient,
        storage: SQLiteStorage,
        config: dict,
    ) -> None:
        self.client = client
        self.storage = storage
        self.config = config
        self.states: defaultdict[str, SymbolState] = defaultdict(SymbolState)
        self.execution = PaperExecutionEngine(
            storage,
            config["risk"],
            config["execution"],
            config["exit"],
        )
        self.symbols: set[str] = set()
        self._last_heartbeat_ms = 0
        self._last_config_check_ms = 0
        self._last_carry_forward_ms = 0
        self.config_path = Path(config["_config_path"]) if config.get("_config_path") else None
        self.config_mtime: float | None = (
            self.config_path.stat().st_mtime if self.config_path and self.config_path.exists() else None
        )

    async def run(self) -> None:
        while not self.symbols:
            try:
                self.symbols = await self._load_universe()
            except Exception as exc:
                message = f"failed to load universe: {type(exc).__name__}: {exc}"
                logger.warning(message)
                self.storage.record_heartbeat(self._now_ms(), "waiting_rest", message)
                await asyncio.sleep(30)

        self.storage.record_heartbeat(self._now_ms(), "running", f"universe={len(self.symbols)}")
        await self._seed_price_windows()
        logger.info(
            "scanner running universe=%s enabled=%s",
            len(self.symbols),
            self._scanner_enabled(),
        )

        kline_manager = asyncio.create_task(self._run_position_kline_manager())
        try:
            async for ticks in self.client.mini_ticker_stream():
                await self._reload_config_if_changed()
                if not self._scanner_enabled():
                    self.states.clear()
                    now_ms = self._now_ms()
                    if now_ms - self._last_heartbeat_ms > 10_000:
                        self._last_heartbeat_ms = now_ms
                        self.storage.record_heartbeat(now_ms, "paused", "scanner_enabled=false")
                        logger.info("scanner paused scanner_enabled=false")
                    continue

                for tick in ticks:
                    if tick.symbol not in self.symbols:
                        continue
                    await self._handle_tick(tick)

                now_ms = self._now_ms()
                self._carry_forward_prices(now_ms)
                if now_ms - self._last_heartbeat_ms > 10_000:
                    self._last_heartbeat_ms = now_ms
                    self.storage.record_heartbeat(now_ms, "running", f"universe={len(self.symbols)}")
                    logger.info("heartbeat running universe=%s ticks=%s", len(self.symbols), len(ticks))
        finally:
            kline_manager.cancel()

    async def _load_universe(self) -> set[str]:
        universe_config = self.config["universe"]
        include_symbols = set(universe_config.get("include_symbols") or [])
        if include_symbols:
            return include_symbols

        exclude_symbols = set(universe_config.get("exclude_symbols") or [])
        exchange_info = await self.client.exchange_info()
        symbols = {
            item["symbol"]
            for item in exchange_info["symbols"]
            if item.get("contractType") == "PERPETUAL"
            and item.get("status") == "TRADING"
            and item.get("quoteAsset") == universe_config["quote_asset"]
        }
        return symbols - exclude_symbols

    async def _reload_config_if_changed(self) -> None:
        if self.config_path is None:
            return

        now_ms = self._now_ms()
        if now_ms - self._last_config_check_ms < 5_000:
            return
        self._last_config_check_ms = now_ms

        try:
            current_mtime = self.config_path.stat().st_mtime
        except FileNotFoundError:
            return

        if self.config_mtime is not None and current_mtime <= self.config_mtime:
            return

        old_universe = self.config.get("universe", {}).copy()
        new_config = load_config(self.config_path).model_dump()
        new_config["_config_path"] = str(self.config_path)
        self.config.clear()
        self.config.update(new_config)
        self.execution.risk_config = self.config["risk"]
        self.execution.execution_config = self.config["execution"]
        self.execution.exit_config = self.config["exit"]
        self.config_mtime = current_mtime

        if old_universe != self.config.get("universe", {}):
            self.symbols = await self._load_universe()
            logger.info("universe reloaded universe=%s", len(self.symbols))

        message = f"config reloaded from {self.config_path}"
        status = "running" if self._scanner_enabled() else "paused"
        logger.info("%s status=%s", message, status)
        self.storage.record_heartbeat(now_ms, status, message)

    def _scanner_enabled(self) -> bool:
        return bool(self.config.get("runtime", {}).get("scanner_enabled", True))

    async def _run_position_kline_manager(self) -> None:
        active_key: tuple[tuple[str, ...], str] | None = None
        stream_task: asyncio.Task | None = None
        while True:
            if not self._scanner_enabled():
                if stream_task is not None:
                    stream_task.cancel()
                    stream_task = None
                    active_key = None
                await asyncio.sleep(5)
                continue

            interval = str(self.config["exit"].get("trailing_kline_interval", "1m"))
            symbols = tuple(
                sorted(
                    {
                        position.symbol
                        for position in self.storage.get_open_positions()
                    }
                )
            )
            next_key = (symbols, interval) if symbols else None
            if next_key != active_key:
                if stream_task is not None:
                    stream_task.cancel()
                    stream_task = None
                active_key = next_key
                if next_key is not None:
                    logger.info(
                        "starting position kline stream symbols=%s interval=%s",
                        ",".join(symbols),
                        interval,
                    )
                    stream_task = asyncio.create_task(
                        self._consume_position_klines(set(symbols), interval)
                    )
            elif stream_task is not None and stream_task.done():
                logger.warning("position kline stream stopped unexpectedly, restarting")
                stream_task = asyncio.create_task(
                    self._consume_position_klines(set(symbols), interval)
                )
            await asyncio.sleep(5)

    async def _consume_position_klines(self, symbols: set[str], interval: str) -> None:
        async for kline in self.client.kline_stream(symbols, interval=interval):
            self.execution.update_position_kline(kline)

    async def _handle_tick(self, tick: PriceTick) -> None:
        state = self.states[tick.symbol]
        self._append_tick(state, tick)
        if self.execution.has_open_position(tick.symbol):
            self.storage.record_latest_price(tick.symbol, tick.timestamp_ms, tick.price)

        if state.pending_candidate is not None:
            if tick.timestamp_ms >= state.pending_candidate.evaluate_after_ms:
                await self._evaluate_pending_candidate(state)
            return

        snapshot = self._snapshot(tick.symbol, state, tick)
        if snapshot is None:
            return

        direction = infer_candidate_direction(snapshot, self.config["signal"])
        if direction is None:
            return

        pending = self._build_pending_candidate(tick, snapshot, direction)
        if pending is None:
            return

        if not self._passes_liquidity_filter(tick):
            self._record_precheck_reject(state, pending, "liquidity_filter_failed")
            return

        if self._in_signal_cooldown(tick.symbol, tick.timestamp_ms, state):
            self._record_precheck_reject(state, pending, "signal_cooldown_active")
            return

        state.pending_candidate = pending
        logger.info(
            "candidate queued symbol=%s direction=%s price=%.8g price_change_pct=%.4f "
            "window_seconds=%s evaluate_after_ms=%s",
            pending.tick.symbol,
            pending.direction.value,
            pending.tick.price,
            pending.price_change_pct,
            pending.window_seconds,
            pending.evaluate_after_ms,
        )

    def _record_precheck_reject(
        self,
        state: SymbolState,
        pending: PendingCandidate,
        reason: str,
    ) -> None:
        cooldown_ms = int(self.config["signal"].get("precheck_log_cooldown_seconds", 60)) * 1000
        if (
            state.last_precheck_reject_at_ms is not None
            and pending.tick.timestamp_ms - state.last_precheck_reject_at_ms < cooldown_ms
        ):
            return

        state.last_precheck_reject_at_ms = pending.tick.timestamp_ms
        log = self._base_signal_check_log(pending)
        log["reject_reason"] = reason
        log["raw"].update(
            {
                "precheck_reject": True,
                "min_24h_quote_volume": self.config["universe"].get("min_24h_quote_volume"),
                "max_24h_quote_volume": self.config["universe"].get("max_24h_quote_volume"),
            }
        )
        self.storage.record_signal_check(log)
        logger.info(
            "candidate precheck rejected symbol=%s direction=%s reason=%s price_change_pct=%.4f "
            "quote_volume_24h=%.2f",
            pending.tick.symbol,
            pending.direction.value,
            reason,
            pending.price_change_pct,
            pending.tick.quote_volume_24h,
        )

    async def _seed_price_windows(self) -> None:
        try:
            ticks = await self.client.ticker_24hr()
        except Exception as exc:
            logger.warning("failed to seed price windows: %s: %s", type(exc).__name__, exc)
            return

        max_window_seconds = max(self.config["signal"].get("windows_seconds") or [60])
        seed_timestamp_ms = self._now_ms() - max_window_seconds * 1000
        seeded = 0
        for tick in ticks:
            if tick.symbol not in self.symbols:
                continue
            seeded_tick = PriceTick(
                symbol=tick.symbol,
                timestamp_ms=seed_timestamp_ms,
                price=tick.price,
                open_24h=tick.open_24h,
                high_24h=tick.high_24h,
                low_24h=tick.low_24h,
                base_volume_24h=tick.base_volume_24h,
                quote_volume_24h=tick.quote_volume_24h,
            )
            self._append_tick(self.states[tick.symbol], seeded_tick)
            seeded += 1

        logger.info(
            "price windows seeded symbols=%s seed_timestamp_ms=%s",
            seeded,
            seed_timestamp_ms,
        )

    def _carry_forward_prices(self, now_ms: int) -> None:
        interval_ms = int(self.config["signal"].get("price_carry_forward_interval_seconds", 5)) * 1000
        if interval_ms <= 0 or now_ms - self._last_carry_forward_ms < interval_ms:
            return

        self._last_carry_forward_ms = now_ms
        carried = 0
        for state in self.states.values():
            if not state.ticks:
                continue
            latest = state.ticks[-1]
            if latest.timestamp_ms >= now_ms:
                continue
            self._append_tick(
                state,
                PriceTick(
                    symbol=latest.symbol,
                    timestamp_ms=now_ms,
                    price=latest.price,
                    open_24h=latest.open_24h,
                    high_24h=latest.high_24h,
                    low_24h=latest.low_24h,
                    base_volume_24h=latest.base_volume_24h,
                    quote_volume_24h=latest.quote_volume_24h,
                ),
            )
            carried += 1

        if carried:
            logger.debug("price windows carried forward symbols=%s", carried)

    @staticmethod
    def _append_tick(state: SymbolState, tick: PriceTick) -> None:
        if state.ticks and state.ticks[-1].timestamp_ms == tick.timestamp_ms:
            state.ticks[-1] = tick
            return
        state.ticks.append(tick)

    def _build_pending_candidate(
        self,
        tick: PriceTick,
        snapshot: MarketSnapshot,
        direction: Direction,
    ) -> PendingCandidate | None:
        window_seconds = self.config["signal"]["primary_window_seconds"]
        price_return = self._return_for_window(snapshot, window_seconds)
        if price_return is None:
            return None

        candle_close_time_ms = ((tick.timestamp_ms // 60_000) + 1) * 60_000 - 1
        return PendingCandidate(
            tick=tick,
            direction=direction,
            price_change_pct=price_return * 100,
            window_seconds=window_seconds,
            candle_close_time_ms=candle_close_time_ms,
            evaluate_after_ms=candle_close_time_ms + self.config["signal"].get(
                "kline_close_delay_ms",
                1500,
            ),
        )

    async def _evaluate_pending_candidate(self, state: SymbolState) -> None:
        pending = state.pending_candidate
        state.pending_candidate = None
        if pending is None:
            return

        evaluation = await self._build_signal_evaluation(pending)
        self.storage.record_signal_check(evaluation.log)
        context = evaluation.context
        if context is None:
            logger.info(
                "candidate rejected symbol=%s direction=%s reason=%s price_change_pct=%.4f "
                "volume_ratio=%s oi_delta_pct=%s taker_buy_ratio=%s taker_sell_ratio=%s score=%s",
                pending.tick.symbol,
                pending.direction.value,
                evaluation.log.get("reject_reason"),
                pending.price_change_pct,
                _safe_float(evaluation.log.get("volume_ratio")),
                _safe_float(evaluation.log.get("oi_delta_pct")),
                _safe_float(evaluation.log.get("taker_buy_ratio")),
                _safe_float(evaluation.log.get("taker_sell_ratio")),
                _safe_float(evaluation.log.get("score")),
            )
            return

        logger.info(
            "signal accepted symbol=%s direction=%s entry=%.8g price_change_pct=%.4f "
            "volume_ratio=%.4f oi_delta_pct=%.4f taker_buy_ratio=%.4f "
            "taker_sell_ratio=%.4f score=%.2f",
            context.symbol,
            context.direction.value,
            context.trigger_price,
            context.price_change_pct,
            context.volume_ratio,
            context.oi_delta_pct,
            context.taker_buy_ratio,
            context.taker_sell_ratio,
            context.score,
        )
        risk = evaluate_probe_risk(
            context,
            self.config["risk"],
            self.config["execution"],
            open_positions=self.storage.count_open_positions(),
            same_direction_positions=self.storage.count_open_positions(context.direction),
            daily_realized_pnl=self.storage.daily_realized_pnl(self._today_start_ms()),
        )
        signal_id = self.storage.record_signal(
            context,
            risk_allowed=risk.allowed,
            risk_reason=risk.reason,
            raw={
                "candidate_detected_at_ms": pending.tick.timestamp_ms,
                "candidate_trigger_price": pending.tick.price,
                "candidate_candle_close_time_ms": pending.candle_close_time_ms,
                "candidate_evaluate_after_ms": pending.evaluate_after_ms,
                "open_24h": pending.tick.open_24h,
                "high_24h": pending.tick.high_24h,
                "low_24h": pending.tick.low_24h,
                "quote_volume_24h": pending.tick.quote_volume_24h,
                "risk_position_fraction": risk.planned_position_fraction,
            },
        )
        state.last_signal_at_ms = context.timestamp_ms
        logger.info(
            "risk decision signal_id=%s symbol=%s allowed=%s reason=%s planned_fraction=%.4f",
            signal_id,
            context.symbol,
            risk.allowed,
            risk.reason,
            risk.planned_position_fraction,
        )

        if risk.allowed and self.config["execution"]["mode"] in {"research", "paper"}:
            position_id = self.execution.open_probe_position(signal_id, context)
            self.storage.record_latest_price(context.symbol, context.timestamp_ms, context.trigger_price)
            logger.info(
                "paper position opened position_id=%s signal_id=%s symbol=%s direction=%s entry=%.8g",
                position_id,
                signal_id,
                context.symbol,
                context.direction.value,
                context.trigger_price,
            )

    def _snapshot(
        self,
        symbol: str,
        state: SymbolState,
        tick: PriceTick,
    ) -> MarketSnapshot | None:
        returns: dict[int, float | None] = {}
        for window in self.config["signal"]["windows_seconds"]:
            base_tick = self._oldest_tick_at_or_before(state, tick.timestamp_ms - window * 1000)
            returns[window] = None if base_tick is None else (tick.price - base_tick.price) / base_tick.price

        if all(value is None for value in returns.values()):
            return None

        return MarketSnapshot(
            symbol=symbol,
            timestamp_ms=tick.timestamp_ms,
            price=tick.price,
            return_10s=returns.get(10),
            return_30s=returns.get(30),
            return_60s=returns.get(60),
        )

    async def _build_signal_evaluation(
        self,
        pending: PendingCandidate,
    ) -> SignalEvaluation:
        log = self._base_signal_check_log(pending)
        kline, current_oi, oi_snapshot = await asyncio.gather(
            self.client.kline_volume_context(
                pending.tick.symbol,
                interval=self.config["signal"]["kline_interval"],
                lookback=self.config["signal"]["kline_lookback"],
                end_time_ms=pending.candle_close_time_ms,
            ),
            self.client.open_interest(pending.tick.symbol),
            self.client.open_interest_hist(pending.tick.symbol, period="5m", limit=1),
            return_exceptions=True,
        )
        if isinstance(kline, Exception):
            log["raw"]["kline_error"] = f"{type(kline).__name__}: {kline}"
            logger.warning(
                "kline request failed symbol=%s error=%s",
                pending.tick.symbol,
                log["raw"]["kline_error"],
            )
            return self._rejected_signal_evaluation(log, "kline_request_failed")
        if isinstance(current_oi, Exception):
            log["raw"]["open_interest_error"] = f"{type(current_oi).__name__}: {current_oi}"
            logger.warning(
                "current open interest request failed symbol=%s error=%s",
                pending.tick.symbol,
                log["raw"]["open_interest_error"],
            )
            return self._rejected_signal_evaluation(log, "open_interest_request_failed")
        if isinstance(oi_snapshot, Exception):
            log["raw"]["open_interest_hist_error"] = f"{type(oi_snapshot).__name__}: {oi_snapshot}"
            logger.warning(
                "open interest snapshot request failed symbol=%s error=%s",
                pending.tick.symbol,
                log["raw"]["open_interest_hist_error"],
            )
            return self._rejected_signal_evaluation(log, "open_interest_request_failed")
        if kline is None:
            return self._rejected_signal_evaluation(log, "missing_kline_data")
        if current_oi is None or oi_snapshot is None:
            return self._rejected_signal_evaluation(log, "missing_open_interest_data")

        oi = self._realtime_oi_context(
            current=current_oi,
            snapshot=oi_snapshot,
            reference_price=kline.close,
        )

        log.update(
            {
                "trigger_price": kline.close,
                "quote_volume_usdt": kline.quote_volume_usdt,
                "average_quote_volume_usdt": kline.average_quote_volume_usdt,
                "volume_ratio": kline.volume_ratio,
                "taker_buy_ratio": kline.taker_buy_ratio,
                "taker_sell_ratio": kline.taker_sell_ratio,
                "open_interest": oi.open_interest,
                "previous_open_interest": oi.previous_open_interest,
                "open_interest_value_usdt": oi.open_interest_value_usdt,
                "oi_delta_pct": oi.delta_pct,
                "oi_delta_value_usdt": oi.delta_value_usdt,
            }
        )
        if kline.quote_volume_usdt <= 0:
            return self._rejected_signal_evaluation(log, "zero_kline_quote_volume")

        close_position = self._close_position(
            low=kline.low,
            high=kline.high,
            close=kline.close,
            direction=pending.direction,
        )
        log["close_position"] = close_position
        if close_position is None:
            return self._rejected_signal_evaluation(log, "invalid_candle_range")
        if pending.direction == Direction.LONG:
            if close_position > self._long_close_distance_max():
                return self._rejected_signal_evaluation(log, "long_close_too_far_from_high")
        elif close_position > self._short_close_distance_max():
            return self._rejected_signal_evaluation(log, "short_close_too_far_from_low")

        oi_value_to_volume_ratio = max(oi.delta_value_usdt, 0.0) / kline.quote_volume_usdt
        log["oi_value_to_volume_ratio"] = oi_value_to_volume_ratio
        score = score_signal(
            direction=pending.direction,
            volume_ratio=kline.volume_ratio,
            taker_buy_ratio=kline.taker_buy_ratio,
            oi_delta_pct=oi.delta_pct,
            oi_value_to_volume_ratio=oi_value_to_volume_ratio,
            spread_pct=None,
            estimated_slippage_pct=None,
        )
        log["score"] = score
        if score < self.config["signal"]["score_probe_min"]:
            return self._rejected_signal_evaluation(log, "score_below_min")

        flow_reject_reason = self._directional_flow_reject_reason(
            pending.direction,
            volume_ratio=kline.volume_ratio,
            taker_buy_ratio=kline.taker_buy_ratio,
            oi_delta_pct=oi.delta_pct,
            oi_value_to_volume_ratio=oi_value_to_volume_ratio,
        )
        if flow_reject_reason is not None:
            return self._rejected_signal_evaluation(log, flow_reject_reason)

        context = SignalContext(
            symbol=pending.tick.symbol,
            direction=pending.direction,
            timestamp_ms=kline.close_time_ms,
            trigger_price=kline.close,
            price_change_pct=pending.price_change_pct,
            window_seconds=pending.window_seconds,
            quote_volume_usdt=kline.quote_volume_usdt,
            average_quote_volume_usdt=kline.average_quote_volume_usdt,
            volume_ratio=kline.volume_ratio,
            taker_buy_ratio=kline.taker_buy_ratio,
            taker_sell_ratio=kline.taker_sell_ratio,
            open_interest=oi.open_interest,
            open_interest_value_usdt=oi.open_interest_value_usdt,
            oi_delta_pct=oi.delta_pct,
            oi_delta_value_usdt=oi.delta_value_usdt,
            oi_value_to_volume_ratio=oi_value_to_volume_ratio,
            spread_pct=None,
            estimated_slippage_pct=None,
            score=score,
            breakout_bar_high=kline.high,
            breakout_bar_low=kline.low,
        )
        log["passed"] = True
        log["reject_reason"] = ""
        return SignalEvaluation(context=context, log=log)

    def _base_signal_check_log(self, pending: PendingCandidate) -> dict:
        return {
            "checked_at_ms": self._now_ms(),
            "candidate_detected_at_ms": pending.tick.timestamp_ms,
            "symbol": pending.tick.symbol,
            "direction": pending.direction.value,
            "window_seconds": pending.window_seconds,
            "candidate_trigger_price": pending.tick.price,
            "candle_close_time_ms": pending.candle_close_time_ms,
            "price_change_pct": pending.price_change_pct,
            "passed": False,
            "reject_reason": "",
            "raw": {
                "candidate_evaluate_after_ms": pending.evaluate_after_ms,
                "open_24h": pending.tick.open_24h,
                "high_24h": pending.tick.high_24h,
                "low_24h": pending.tick.low_24h,
                "quote_volume_24h": pending.tick.quote_volume_24h,
                "open_interest_source": "current_open_interest_vs_latest_5m_snapshot",
            },
        }

    @staticmethod
    def _rejected_signal_evaluation(log: dict, reason: str) -> SignalEvaluation:
        log["passed"] = False
        log["reject_reason"] = reason
        return SignalEvaluation(context=None, log=log)

    def _directional_flow_reject_reason(
        self,
        direction: Direction,
        *,
        volume_ratio: float,
        taker_buy_ratio: float,
        oi_delta_pct: float,
        oi_value_to_volume_ratio: float,
    ) -> str | None:
        signal_config = self.config["signal"]
        if volume_ratio < signal_config["volume_ratio_min"]:
            return "volume_ratio_below_min"

        if oi_delta_pct < signal_config["oi_delta_pct_min"]:
            return "oi_delta_pct_below_min"

        if oi_value_to_volume_ratio < signal_config["oi_value_to_volume_ratio_min"]:
            return "oi_value_to_volume_below_min"

        if direction == Direction.LONG:
            if taker_buy_ratio < signal_config["taker_buy_ratio_min_for_long"]:
                return "taker_buy_ratio_below_min"
            return None

        taker_sell_ratio = 1.0 - taker_buy_ratio
        if taker_sell_ratio < signal_config["taker_sell_ratio_min_for_short"]:
            return "taker_sell_ratio_below_min"
        return None

    @staticmethod
    def _realtime_oi_context(
        *,
        current: CurrentOpenInterest,
        snapshot: OIContext,
        reference_price: float,
    ) -> OIContext:
        return OIContext(
            symbol=current.symbol,
            timestamp_ms=current.timestamp_ms,
            open_interest=current.open_interest,
            open_interest_value_usdt=current.open_interest * reference_price,
            previous_open_interest=snapshot.open_interest,
            previous_open_interest_value_usdt=snapshot.open_interest * reference_price,
        )

    @staticmethod
    def _return_for_window(snapshot: MarketSnapshot, window_seconds: int) -> float | None:
        if window_seconds == 10:
            return snapshot.return_10s
        if window_seconds == 30:
            return snapshot.return_30s
        if window_seconds == 60:
            return snapshot.return_60s
        return None

    def _long_close_distance_max(self) -> float:
        signal_config = self.config["signal"]
        if "long_close_distance_max" in signal_config:
            return float(signal_config["long_close_distance_max"])
        return max(1.0 - float(signal_config.get("long_close_position_min", 0.999)), 0.0)

    def _short_close_distance_max(self) -> float:
        signal_config = self.config["signal"]
        if "short_close_distance_max" in signal_config:
            return float(signal_config["short_close_distance_max"])
        return float(signal_config.get("short_close_position_max", 0.001))

    @staticmethod
    def _close_position(
        *,
        low: float,
        high: float,
        close: float,
        direction: Direction,
    ) -> float | None:
        if low <= 0 or high <= 0:
            return None
        if direction == Direction.LONG:
            return max((high - close) / high, 0.0)
        return max((close - low) / low, 0.0)

    @staticmethod
    def _oldest_tick_at_or_before(state: SymbolState, timestamp_ms: int) -> PriceTick | None:
        candidates = [tick for tick in state.ticks if tick.timestamp_ms <= timestamp_ms]
        if not candidates:
            return None
        return candidates[-1]

    def _passes_liquidity_filter(self, tick: PriceTick) -> bool:
        universe_config = self.config["universe"]
        return (
            tick.quote_volume_24h >= universe_config["min_24h_quote_volume"]
            and tick.quote_volume_24h <= universe_config["max_24h_quote_volume"]
        )

    def _in_signal_cooldown(self, symbol: str, timestamp_ms: int, state: SymbolState) -> bool:
        cooldown_ms = self.config["signal"]["signal_cooldown_seconds"] * 1000
        last_signal_at_ms = state.last_signal_at_ms or self.storage.last_signal_time_ms(symbol)
        return last_signal_at_ms is not None and timestamp_ms - last_signal_at_ms < cooldown_ms

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _today_start_ms() -> int:
        return int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)) * 1000)


async def run_market_scanner(config: dict) -> None:
    client = BinanceMarketClient(
        rest_base_url=config["exchange"]["rest_base_url"],
        websocket_url=config["exchange"]["websocket_url"],
        request_timeout_seconds=config["exchange"]["request_timeout_seconds"],
        rest_retries=config["exchange"].get("rest_retries", 5),
    )
    storage = SQLiteStorage(config["storage"]["database_url"])
    scanner = MarketScanner(client=client, storage=storage, config=config)
    await scanner.run()
