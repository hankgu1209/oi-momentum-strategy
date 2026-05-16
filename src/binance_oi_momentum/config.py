from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class StrategyConfig:
    exchange: dict[str, Any]
    universe: dict[str, Any]
    signal: dict[str, Any]
    execution: dict[str, Any]
    risk: dict[str, Any]
    exit: dict[str, Any]
    storage: dict[str, Any]

    def model_dump(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "universe": self.universe,
            "signal": self.signal,
            "execution": self.execution,
            "risk": self.risk,
            "exit": self.exit,
            "storage": self.storage,
        }


def load_config(path: str | Path) -> StrategyConfig:
    with Path(path).open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file)
    return StrategyConfig(**raw)
