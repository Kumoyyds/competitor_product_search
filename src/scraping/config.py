from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, PrivateAttr, field_validator
from pydantic_settings import BaseSettings

from .models.enums import PAGE_TYPES


class ScrapingConfig(BaseSettings):
    _provider_env_file: Any = PrivateAttr(default=".env")
    _provider_dotenv_values: Optional[dict[str, Optional[str]]] = PrivateAttr(
        default=None
    )

    # --- Bright Data ---
    bright_data_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "SCRAPING_BRIGHT_DATA_KEY", "BRIGHT_DATA_KEY", "BRIGHT_UNLOCKER_KEY"
        ),
    )
    bright_data_zone: str = "web_unlocker1"

    # --- Qwen (repair LLM) ---
    qwen_key: str = Field(
        default="",
        validation_alias=AliasChoices("SCRAPING_QWEN_KEY", "QWEN_KEY"),
    )
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # --- concurrency ---
    per_site_concurrency: int = 16

    # --- extraction ---
    extraction_retry_count: int = 2
    extraction_retry_interval: float = 2.0

    # --- Bright Data async polling (Datasets + DCA) ---
    bd_async_poll_max_seconds: int = 300  # wall-clock budget to poll one snapshot
    bd_async_poll_interval_seconds: float = 4.0  # sleep between poll GETs

    # --- repair (HTML route) ---
    # Choose model ids from providers.py. Changing provider only requires the
    # model name here plus that provider's key in .env. Edit both ladder lists
    # together to change attempt count (their lengths are checked at runtime).
    repair_model_ladder: list[str] = Field(
        default=["deepseek-v4-flash", "deepseek-v4-flash"]
    )
    repair_temperature_ladder: list[float] = Field(
        default=[0.1, 0.4]
    )

    # --- cold start (independent from the runtime repair ladder) ---
    # Node 0 generates the initial parser; later rounds repair it. The last
    # model/temperature rung repeats until review succeeds or the human stops.
    cold_start_model_ladder: list[str] = Field(
        default=["deepseek-v4-flash", "deepseek-v4-flash"]
    )
    cold_start_temperature_ladder: list[float] = Field(
        default=[0.1, 0.4]
    )
    # The repair loop is human-terminated; this is only a runaway guard.
    cold_start_max_repair_rounds: int = Field(default=10)

    # --- JSON self-healing (API route) ---
    json_heal_budget: int = 1

    # --- parser lifecycle ---
    prune_sliding_window: int = 50
    per_site_parser_limit: int = 4

    # --- sandbox ---
    sandbox_timeout: int = 10
    sandbox_max_concurrency: int = 8
    sandbox_spawn_retries: int = 2
    sandbox_spawn_retry_interval: float = 1.0
    sandbox_import_whitelist: list[str] = Field(
        default=["bs4", "lxml", "re", "json"]
    )

    # --- cold start / golden set page_type policy ---
    # Missing keys are deliberately treated as mandatory by the accessors below.
    cold_start_page_require_mandatory: dict[str, bool] = Field(
        default_factory=lambda: {
            "standard": True,
            "discounted": True,
            "out_of_stock": True,
            "membership": True,
            "multipack": False,
        }
    )
    golden_max_samples_per_page_type: int = 3

    # --- dedup ---
    scrape_runs_dedup_window_seconds: int = 3600

    # --- invalid target detection ---
    invalid_target_absence_threshold: int = 2
    mass_invalid_target_ratio: float = 0.3
    mass_invalid_target_absolute: int = 20

    # --- storage ---
    db_path: Path = Path("scraping.db")

    # --- per-site scraper lists (code-registered, not configured here) ---

    model_config = {
        "env_prefix": "SCRAPING_",
        "env_file": ".env",
        "extra": "ignore",
        "populate_by_name": True,
    }

    def __init__(self, **values: Any) -> None:
        provider_env_file = values.get(
            "_env_file", self.model_config.get("env_file")
        )
        super().__init__(**values)
        self._provider_env_file = provider_env_file

    def api_key_for(self, key_name: str) -> str:
        """Resolve a provider API key from environment or configured env file.

        Precedence is ``SCRAPING_<KEY>`` then ``<KEY>`` in ``os.environ``,
        followed by those names in the configured dotenv file. A matching
        settings field is the final compatibility fallback (for example,
        programmatic ``ScrapingConfig(qwen_key=...)`` in existing callers).
        Dotenv files are parsed at most once per config instance.
        """
        prefixed_name = (
            key_name if key_name.startswith("SCRAPING_") else f"SCRAPING_{key_name}"
        )
        for name in (prefixed_name, key_name):
            if name in os.environ:
                return os.environ[name]

        if self._provider_dotenv_values is None:
            self._provider_dotenv_values = self._read_provider_dotenv()
        for name in (prefixed_name, key_name):
            if name in self._provider_dotenv_values:
                return self._provider_dotenv_values[name] or ""

        configured = getattr(self, key_name.lower(), "")
        return configured if isinstance(configured, str) else ""

    def _read_provider_dotenv(self) -> dict[str, Optional[str]]:
        env_file = self._provider_env_file
        if env_file is None:
            return {}
        paths = env_file if isinstance(env_file, (list, tuple)) else (env_file,)
        merged: dict[str, Optional[str]] = {}
        for path in paths:
            merged.update(dotenv_values(path))
        return merged

    @field_validator("cold_start_page_require_mandatory")
    @classmethod
    def _validate_page_type_policy(cls, value: dict[str, bool]) -> dict[str, bool]:
        unknown = sorted(set(value) - set(PAGE_TYPES))
        if unknown:
            raise ValueError(
                "unknown page_type key(s): "
                f"{', '.join(unknown)}; legal values: {', '.join(PAGE_TYPES)}"
            )
        return value

    @field_validator("golden_max_samples_per_page_type")
    @classmethod
    def _validate_golden_max(cls, value: int) -> int:
        if value < 1:
            raise ValueError("golden_max_samples_per_page_type must be >= 1")
        return value

    @field_validator(
        "per_site_concurrency", "sandbox_timeout", "sandbox_max_concurrency"
    )
    @classmethod
    def _validate_positive_sandbox_int(cls, value: int) -> int:
        if value < 1:
            raise ValueError("timeout/concurrency values must be >= 1")
        return value

    @field_validator("sandbox_spawn_retries")
    @classmethod
    def _validate_sandbox_retries(cls, value: int) -> int:
        if value < 0:
            raise ValueError("sandbox_spawn_retries must be >= 0")
        return value

    @field_validator("sandbox_spawn_retry_interval")
    @classmethod
    def _validate_sandbox_retry_interval(cls, value: float) -> float:
        if value < 0:
            raise ValueError("sandbox_spawn_retry_interval must be >= 0")
        return value

    def is_mandatory_page_type(self, page_type: str) -> bool:
        return self.cold_start_page_require_mandatory.get(page_type, True)

    def golden_min_for(self, page_type: str) -> int:
        return 1 if self.is_mandatory_page_type(page_type) else 0

    def golden_max_for(self, page_type: str) -> int:
        return self.golden_max_samples_per_page_type

    def mandatory_page_types(self) -> tuple[str, ...]:
        return tuple(pt for pt in PAGE_TYPES if self.is_mandatory_page_type(pt))


_config: Optional[ScrapingConfig] = None


def get_config() -> ScrapingConfig:
    global _config
    if _config is None:
        _config = ScrapingConfig()
    return _config


def set_config(config: ScrapingConfig) -> None:
    global _config
    _config = config
