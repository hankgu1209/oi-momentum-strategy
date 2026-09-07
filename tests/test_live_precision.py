from __future__ import annotations

import asyncio
from decimal import Decimal

from binance_oi_momentum.execution import LiveExecutionEngine


class FakeTradingClient:
    async def exchange_info(self) -> dict:
        return {
            "symbols": [
                {
                    "symbol": "FIDAUSDT",
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "tickSize": "0.0000100",
                        },
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "1",
                            "maxQty": "30000000",
                            "stepSize": "1",
                        },
                    ],
                }
            ]
        }


def test_round_down_uses_step_multiple() -> None:
    assert LiveExecutionEngine._round_down(123.987, "1") == 123
    assert LiveExecutionEngine._round_down(0.123456, "0.0000100") == Decimal("0.12345")


def test_symbol_rules_extract_fida_filters() -> None:
    engine = LiveExecutionEngine(
        trading_client=FakeTradingClient(),
        planner=object(),
        execution_config={},
    )

    rules = asyncio.run(engine._symbol_rules("FIDAUSDT"))

    assert rules["LOT_SIZE"]["stepSize"] == "1"
    assert rules["PRICE_FILTER"]["tickSize"] == "0.0000100"
