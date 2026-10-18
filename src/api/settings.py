from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    db_path: Path = Field(Path("orchestrator.db"), validation_alias="ORCHESTRATOR_DB_PATH")
    upload_dir: Path = Field(Path("uploads"), validation_alias="API_UPLOAD_DIR")
    max_concurrent_batches: int = Field(
        1, ge=1, validation_alias="API_MAX_CONCURRENT_BATCHES"
    )
    default_concurrency: int = Field(8, ge=1, validation_alias="API_DEFAULT_CONCURRENCY")
