from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import websockets

from .models import KlineVolumeContext, OIContext, PriceTick


class BinanceMarketClient:
    def __init__(
        self,
        *,
        rest_base_url: str,
        websocket_url: str,
        request_timeout_seconds: float,
        rest_retries: int = 5,
    ) -> None:
        self.rest_base_url = rest_base_url.rstrip("/")
        self.websocket_url = websocket_url
        self.timeout = aiohttp.ClientTimeout(total=request_timeout_seconds)
        self.rest_retries = rest_retries

    async def exchange_info(self) -> dict[str, Any]:
        return await self._get_json("/fapi/v1/exchangeInfo")

    async def open_interest_hist(self, symbol: str, period: str = "5m", limit: int = 2) -> OIContext | None:
        params = {"symbol": symbol, "period": period, "limit": limit}
        payload = await self._get_json("/futures/data/openInterestHist", params=params)

        if not payload:
            return None

        latest = payload[-1]
        previous = payload[-2] if len(payload) >= 2 else None
        return OIContext(
            symbol=symbol,
            timestamp_ms=int(latest["timestamp"]),
            open_interest=float(latest["sumOpenInterest"]),
            open_interest_value_usdt=float(latest["sumOpenInterestValue"]),
            previous_open_interest=None
            if previous is None
            else float(previous["sumOpenInterest"]),
            previous_open_interest_value_usdt=None
            if previous is None
            else float(previous["sumOpenInterestValue"]),
        )

    async def kline_volume_context(
        self,
        symbol: str,
        *,
        interval: str = "1m",
        lookback: int = 30,
        end_time_ms: int | None = None,
    ) -> KlineVolumeContext | None:
        params = {
            "pair": symbol,
            "contractType": "PERPETUAL",
            "interval": interval,
            "limit": lookback + 2,
        }
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        payload = await self._get_json("/fapi/v1/continuousKlines", params=params)
        if len(payload) < lookback + 1:
            return None

        if end_time_ms is None:
            now_ms = int(time.time() * 1000)
            closed_rows = [item for item in payload if int(item[6]) <= now_ms]
        else:
            closed_rows = [item for item in payload if int(item[6]) <= end_time_ms]
        if len(closed_rows) < lookback + 1:
            return None

        latest = closed_rows[-1]
        baseline = closed_rows[-(lookback + 1):-1]
        baseline_volumes = [float(item[7]) for item in baseline]
        average_quote_volume = sum(baseline_volumes) / len(baseline_volumes)
        latest_quote_volume = float(latest[7])
        taker_buy_quote_volume = float(latest[10])
        taker_sell_quote_volume = max(latest_quote_volume - taker_buy_quote_volume, 0.0)
        taker_buy_ratio = (
            taker_buy_quote_volume / latest_quote_volume if latest_quote_volume > 0 else 0.0
        )

        return KlineVolumeContext(
            symbol=symbol,
            interval=interval,
            open_time_ms=int(latest[0]),
            close_time_ms=int(latest[6]),
            open=float(latest[1]),
            high=float(latest[2]),
            low=float(latest[3]),
            close=float(latest[4]),
            quote_volume_usdt=latest_quote_volume,
            average_quote_volume_usdt=average_quote_volume,
            volume_ratio=latest_quote_volume / average_quote_volume
            if average_quote_volume > 0
            else 0.0,
            taker_buy_quote_volume_usdt=taker_buy_quote_volume,
            taker_sell_quote_volume_usdt=taker_sell_quote_volume,
            taker_buy_ratio=taker_buy_ratio,
            taker_sell_ratio=1.0 - taker_buy_ratio,
        )

    async def mini_ticker_stream(self) -> AsyncIterator[list[PriceTick]]:
        while True:
            try:
                async with websockets.connect(
                    self.websocket_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as websocket:
                    async for raw_message in websocket:
                        payload = json.loads(raw_message)
                        data = payload.get("data", payload)
                        if not isinstance(data, list):
                            continue
                        ticks = [self._parse_mini_ticker(item) for item in data]
                        yield [tick for tick in ticks if tick is not None]
            except Exception:
                await asyncio.sleep(5)

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.rest_retries + 1):
            try:
                async with aiohttp.ClientSession(timeout=self.timeout, trust_env=True) as session:
                    async with session.get(f"{self.rest_base_url}{path}", params=params) as response:
                        response.raise_for_status()
                        return await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                await asyncio.sleep(min(2**attempt, 30))

        assert last_error is not None
        raise last_error

    @staticmethod
    def _parse_mini_ticker(item: dict[str, Any]) -> PriceTick | None:
        try:
            return PriceTick(
                symbol=item["s"],
                timestamp_ms=int(item["E"]),
                price=float(item["c"]),
                open_24h=float(item["o"]),
                high_24h=float(item["h"]),
                low_24h=float(item["l"]),
                base_volume_24h=float(item["v"]),
                quote_volume_24h=float(item["q"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
