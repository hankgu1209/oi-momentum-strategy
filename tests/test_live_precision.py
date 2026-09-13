from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from binance_oi_momentum.execution import LiveExecutionEngine
from binance_oi_momentum.models import Direction


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


class ProtectionClient:
    def __init__(self) -> None:
        self.algo_calls = []
        self.order_calls = []

    async def new_algo_order(self, **params: dict) -> dict:
        self.algo_calls.append(params)
        return params

    async def new_order(self, **params: dict) -> dict:
        self.order_calls.append(params)
        return params


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


def test_scale_out_tp1_is_reduce_only_limit_order() -> None:
    client = ProtectionClient()
    planner = SimpleNamespace(
        exit_config={"scale_out_enabled": True, "first_take_profit_fraction": 0.5}
    )
    engine = LiveExecutionEngine(
        trading_client=client,
        planner=planner,
        execution_config={"hedge_mode": False},
    )

    asyncio.run(
        engine._place_protection_orders(
            symbol="FIDAUSDT",
            direction=Direction.LONG,
            quantity=Decimal("10"),
            stop_price=Decimal("0.10000"),
            take_profit_price=Decimal("0.12000"),
            symbol_rules={"LOT_SIZE": {"stepSize": "1"}},
        )
    )

    assert client.algo_calls[0]["type"] == "STOP_MARKET"
    assert client.order_calls == [
        {
            "symbol": "FIDAUSDT",
            "side": "SELL",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "price": "0.12",
            "quantity": "5",
            "newClientOrderId": "OIM-TP1-FIDAUSDT-LONG",
            "reduceOnly": "true",
        }
    ]
