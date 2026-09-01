from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).with_name("matching_config.yaml")


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"{_CONFIG_PATH}: top level must be a mapping")
    return data


def get(*keys: str, default: Any = None) -> Any:
    current: Any = load_config()
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current
