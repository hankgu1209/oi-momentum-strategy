from __future__ import annotations

import argparse
import asyncio

from .config import load_config
from .scanner import run_market_scanner


def run_scanner() -> None:
    parser = argparse.ArgumentParser(description="Run the Binance OI momentum research scanner.")
    parser.add_argument(
        "--config",
        default="configs/strategy.example.yaml",
        help="Path to strategy YAML config.",
    )
    args = parser.parse_args()
    config = load_config(args.config)
    asyncio.run(run_market_scanner(config.model_dump()))


if __name__ == "__main__":
    run_scanner()
