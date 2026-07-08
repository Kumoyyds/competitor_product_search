from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class ScrapingConfig(BaseSettings):
    # --- Bright Data ---
    bright_data_key: str = ""
    bright_data_zone: str = "web_unlocker1"

    # --- DeepSeek (repair LLM) ---
    deepseek_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"

    # --- concurrency ---
    per_site_concurrency: int = 16

    # --- extraction ---
    extraction_retry_count: int = 2
    extraction_retry_interval: float = 2.0

    # --- repair (HTML route) ---
    repair_budget: int = 3
    repair_model_ladder: list[str] = Field(
        default=["deepseek-chat", "deepseek-chat", "deepseek-reasoner"]
    )

    # --- JSON self-healing (API route) ---
    json_heal_budget: int = 1

    # --- parser lifecycle ---
    prune_sliding_window: int = 50
    per_site_parser_limit: int = 4

    # --- sandbox ---
    sandbox_timeout: int = 10
    sandbox_import_whitelist: list[str] = Field(
        default=["bs4", "lxml", "re", "json"]
    )

    # --- promote ---
    promote_min_samples_per_page_type: int = 1

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
    }


_config: Optional[ScrapingConfig] = None


def get_config() -> ScrapingConfig:
    global _config
    if _config is None:
        _config = ScrapingConfig()
    return _config


def set_config(config: ScrapingConfig) -> None:
    global _config
    _config = config
