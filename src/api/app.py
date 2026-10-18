from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.orchestrator.database import OrchestratorDB

from .routes.batches import router
from .runner import BatchRunner
from .settings import ApiSettings


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    resolved = settings or ApiSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        resolved.upload_dir.mkdir(parents=True, exist_ok=True)
        db = OrchestratorDB(resolved.db_path)
        try:
            db.mark_running_batches_interrupted("API server restarted")
        finally:
            db.close()
        app.state.settings = resolved
        runner = BatchRunner(resolved.max_concurrent_batches)
        app.state.runner = runner
        try:
            yield
        finally:
            await runner.shutdown()

    app = FastAPI(title="PriceScope Operations API", lifespan=lifespan)
    app.include_router(router)
    return app
