from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from .binance import BinanceMarketClient
from .execution import PaperExecutionEngine
from .models import Direction, MarketSnapshot, PriceTick, SignalContext
from .risk import evaluate_probe_risk
from .storage import SQLiteStorage
from .strategy import infer_candidate_direction, passes_directional_flow, score_signal


@dataclass
class SymbolState:
    ticks: deque[PriceTick] = field(default_factory=lambda: deque(maxlen=600))
    last_signal_at_ms: int | None = None
    pending_candidate: PendingCandidate | None = None


@dataclass(frozen=True)
class PendingCandidate:
    tick: PriceTick
    direction: Direction
    price_change_pct: float
    window_seconds: int
    candle_close_time_ms: int
    evaluate_after_ms: int


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

    async def run(self) -> None:
        while not self.symbols:
            try:
                self.symbols = await self._load_universe()
            except Exception as exc:
                message = f"failed to load universe: {type(exc).__name__}: {exc}"
                print(message, flush=True)
                self.storage.record_heartbeat(self._now_ms(), "waiting_rest", message)
                await asyncio.sleep(30)

        self.storage.record_heartbeat(self._now_ms(), "running", f"universe={len(self.symbols)}")
        print(f"scanner running, universe={len(self.symbols)}", flush=True)

        async for ticks in self.client.mini_ticker_stream():
            for tick in ticks:
                if tick.symbol not in self.symbols:
                    continue
                await self._handle_tick(tick)

            now_ms = self._now_ms()
            if now_ms - self._last_heartbeat_ms > 10_000:
                self._last_heartbeat_ms = now_ms
                self.storage.record_heartbeat(now_ms, "running", f"universe={len(self.symbols)}")

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

    async def _handle_tick(self, tick: PriceTick) -> None:
        state = self.states[tick.symbol]
        state.ticks.append(tick)
        if self.execution.update_open_positions(tick.symbol, tick.price, tick.timestamp_ms):
            self.storage.record_latest_price(tick.symbol, tick.timestamp_ms, tick.price)

        if state.pending_candidate is not None:
            if tick.timestamp_ms >= state.pending_candidate.evaluate_after_ms:
                await self._evaluate_pending_candidate(state)
            return

        if not self._passes_liquidity_filter(tick):
            return

        snapshot = self._snapshot(tick.symbol, state, tick)
        if snapshot is None:
            return

        direction = infer_candidate_direction(snapshot, self.config["signal"])
        if direction is None:
            return

        if self._in_signal_cooldown(tick.symbol, tick.timestamp_ms, state):
            return

        pending = self._build_pending_candidate(tick, snapshot, direction)
        if pending is None:
            return
        state.pending_candidate = pending

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

        context = await self._build_signal_context(pending)
        if context is None:
            return

        if not passes_directional_flow(
            direction=context.direction,
            volume_ratio=context.volume_ratio,
            taker_buy_ratio=context.taker_buy_ratio,
            oi_delta_pct=context.oi_delta_pct,
            oi_value_to_volume_ratio=context.oi_value_to_volume_ratio,
            signal_config=self.config["signal"],
        ):
            return

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

        if risk.allowed and self.config["execution"]["mode"] in {"research", "paper"}:
            self.execution.open_probe_position(signal_id, context)
            self.storage.record_latest_price(context.symbol, context.timestamp_ms, context.trigger_price)

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

    async def _build_signal_context(
        self,
        pending: PendingCandidate,
    ) -> SignalContext | None:
        kline, oi = await asyncio.gather(
            self.client.kline_volume_context(
                pending.tick.symbol,
                interval=self.config["signal"]["kline_interval"],
                lookback=self.config["signal"]["kline_lookback"],
                end_time_ms=pending.candle_close_time_ms,
            ),
            self.client.open_interest_hist(pending.tick.symbol, period="5m", limit=2),
        )
        if kline is None or oi is None:
            return None
        if kline.quote_volume_usdt <= 0:
            return None

        close_position = self._close_position(kline.low, kline.high, kline.close)
        if close_position is None:
            return None
        if pending.direction == Direction.LONG:
            if close_position < self.config["signal"]["long_close_position_min"]:
                return None
        elif close_position > self.config["signal"]["short_close_position_max"]:
            return None

        oi_value_to_volume_ratio = max(oi.delta_value_usdt, 0.0) / kline.quote_volume_usdt
        score = score_signal(
            direction=pending.direction,
            volume_ratio=kline.volume_ratio,
            taker_buy_ratio=kline.taker_buy_ratio,
            oi_delta_pct=oi.delta_pct,
            oi_value_to_volume_ratio=oi_value_to_volume_ratio,
            spread_pct=None,
            estimated_slippage_pct=None,
        )
        if score < self.config["signal"]["score_probe_min"]:
            return None

        return SignalContext(
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

    @staticmethod
    def _close_position(low: float, high: float, close: float) -> float | None:
        candle_range = high - low
        if candle_range <= 0:
            return None
        return (close - low) / candle_range

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
