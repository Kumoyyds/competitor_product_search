from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, Request

from .runner import BatchRunner
from .settings import ApiSettings


def get_settings(request: Request) -> ApiSettings:
    return request.app.state.settings


def get_runner(request: Request) -> BatchRunner:
    return request.app.state.runner


def get_db(settings: ApiSettings = Depends(get_settings)) -> Iterator[sqlite3.Connection]:
    path = Path(settings.db_path).resolve()
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
