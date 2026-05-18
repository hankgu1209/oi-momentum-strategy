from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import shutil

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
    runtime: dict[str, Any] = field(default_factory=lambda: {"scanner_enabled": True})

    def model_dump(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "exchange": self.exchange,
            "universe": self.universe,
            "signal": self.signal,
            "execution": self.execution,
            "risk": self.risk,
            "exit": self.exit,
            "storage": self.storage,
        }


def load_config(path: str | Path) -> StrategyConfig:
    target = Path(path)
    if not target.exists() and target.name == "strategy.local.yaml":
        template = target.with_name("strategy.example.yaml")
        if template.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(template, target)

    with target.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file)
    return StrategyConfig(**raw)


def save_config(path: str | Path, config: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_suffix(f"{target.suffix}.bak")
        shutil.copy2(target, backup)

    tmp_path = target.with_suffix(f"{target.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        yaml.safe_dump(config, file, sort_keys=False, allow_unicode=True)
    tmp_path.replace(target)
