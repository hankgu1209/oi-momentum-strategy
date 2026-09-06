from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from .config import load_config
from .logging_utils import configure_logging
from .scanner import run_market_scanner


logger = logging.getLogger(__name__)


def load_dotenv(path: str | Path = ".env") -> None:
    dotenv_path = Path(path)
    if not dotenv_path.exists():
        return
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip("'").strip('"')
        os.environ[key] = value


def run_scanner() -> None:
    configure_logging("scanner")
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run the Binance OI momentum research scanner.")
    parser.add_argument(
        "--config",
        default=os.getenv("OI_MOMENTUM_CONFIG", "configs/strategy.local.yaml"),
        help="Path to strategy YAML config.",
    )
    args = parser.parse_args()
    config = load_config(args.config)
    config_dict = config.model_dump()
    config_dict["_config_path"] = args.config
    logger.info("starting scanner config=%s", args.config)
    asyncio.run(run_market_scanner(config_dict))


if __name__ == "__main__":
    run_scanner()
