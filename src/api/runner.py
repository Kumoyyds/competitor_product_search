from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from src.orchestrator import PreparedBatch, execute_batch
from src.orchestrator.database import OrchestratorDB


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Job:
    prepared: PreparedBatch
    task: asyncio.Task[None]
    started: bool = False
    queued_interrupted: bool = False


class BatchRunner:
    def __init__(self, max_concurrent_batches: int):
        self._semaphore = asyncio.Semaphore(max_concurrent_batches)
        self._jobs: dict[str, _Job] = {}

    def submit(self, prepared: PreparedBatch) -> None:
        task = asyncio.create_task(self._run(prepared), name=f"batch:{prepared.batch_id}")
        self._jobs[prepared.batch_id] = _Job(prepared, task)

    def phase(self, batch_id: str) -> Literal["queued", "running"] | None:
        job = self._jobs.get(batch_id)
        if job is None:
            return None
        return "running" if job.started else "queued"

    async def _run(self, prepared: PreparedBatch) -> None:
        job = self._jobs[prepared.batch_id]
        try:
            async with self._semaphore:
                job.started = True
                await execute_batch(prepared)
        except asyncio.CancelledError:
            if not job.started:
                await asyncio.to_thread(self._finish_interrupted, prepared)
                job.queued_interrupted = True
            raise
        except Exception:
            logger.exception("batch %s failed", prepared.batch_id)
        finally:
            self._jobs.pop(prepared.batch_id, None)

    @staticmethod
    def _finish_interrupted(prepared: PreparedBatch) -> None:
        db = OrchestratorDB(prepared.db_path)
        try:
            db.finish_batch(
                prepared.batch_id,
                error_message="API server stopped before batch execution",
                interrupted=True,
            )
        finally:
            db.close()

    async def shutdown(self) -> None:
        jobs = list(self._jobs.values())
        for job in jobs:
            job.task.cancel()
        if jobs:
            await asyncio.gather(*(job.task for job in jobs), return_exceptions=True)
        # A task may have been cancelled before its coroutine had a chance to start.
        for job in jobs:
            if not job.started and not job.queued_interrupted:
                await asyncio.to_thread(self._finish_interrupted, job.prepared)
        self._jobs.clear()
