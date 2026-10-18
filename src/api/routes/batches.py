from __future__ import annotations

import asyncio
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status

from src.orchestrator import prepare_new_input, prepare_rerun

from .. import queries
from ..deps import get_db, get_runner, get_settings
from ..runner import BatchRunner
from ..schemas.batches import AcceptedBatch, NewItemsRequest, RerunRequest
from ..settings import ApiSettings


router = APIRouter(tags=["batches"])
Settings = Annotated[ApiSettings, Depends(get_settings)]
Runner = Annotated[BatchRunner, Depends(get_runner)]
Connection = Annotated[sqlite3.Connection, Depends(get_db)]


def _copy_upload(source: UploadFile, destination: Path) -> None:
    with destination.open("wb") as target:
        shutil.copyfileobj(source.file, target)


async def _prepare_new(source: object, settings: ApiSettings, vision_enabled: bool, concurrency: int):
    try:
        return await asyncio.to_thread(
            prepare_new_input,
            source,
            vision_enabled=vision_enabled,
            concurrency=concurrency,
            db_path=settings.db_path,
        )
    except (KeyError, ValueError) as exc:
        detail = exc.args[0] if isinstance(exc, KeyError) else str(exc)
        raise HTTPException(status_code=400, detail=detail) from exc


@router.post("/batches/new-input", response_model=AcceptedBatch, status_code=status.HTTP_202_ACCEPTED)
async def create_file_batch(
    file: Annotated[UploadFile, File()],
    settings: Settings,
    runner: Runner,
    vision_enabled: Annotated[bool, Form()] = False,
    concurrency: Annotated[int | None, Form(ge=1)] = None,
) -> AcceptedBatch:
    filename = Path(file.filename or "").name
    if Path(filename).suffix.lower() not in {".xlsx", ".csv", ".json"}:
        raise HTTPException(status_code=400, detail="input file must be .xlsx, .csv, or .json")
    saved = settings.upload_dir / f"{uuid.uuid4().hex}_{filename}"
    try:
        await asyncio.to_thread(_copy_upload, file, saved)
    finally:
        await file.close()
    prepared = await _prepare_new(
        saved, settings, vision_enabled, concurrency or settings.default_concurrency
    )
    runner.submit(prepared)
    return AcceptedBatch(batch_id=prepared.batch_id, status_url=f"/batches/{prepared.batch_id}")


@router.post("/batches/new-input/items", response_model=AcceptedBatch, status_code=status.HTTP_202_ACCEPTED)
async def create_items_batch(body: NewItemsRequest, settings: Settings, runner: Runner) -> AcceptedBatch:
    prepared = await _prepare_new(
        body.items,
        settings,
        body.vision_enabled,
        body.concurrency or settings.default_concurrency,
    )
    runner.submit(prepared)
    return AcceptedBatch(batch_id=prepared.batch_id, status_url=f"/batches/{prepared.batch_id}")


@router.post("/batches/{batch_id}/rerun", response_model=AcceptedBatch, status_code=status.HTTP_202_ACCEPTED)
async def create_rerun_batch(
    batch_id: str, body: RerunRequest, settings: Settings, runner: Runner
) -> AcceptedBatch:
    try:
        prepared = await asyncio.to_thread(
            prepare_rerun,
            batch_id,
            search_titles=body.search_titles,
            vision_enabled=body.vision_enabled,
            concurrency=body.concurrency or settings.default_concurrency,
            db_path=settings.db_path,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=exc.args[0]) from exc
    except ValueError as exc:
        code = 409 if "still running" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    runner.submit(prepared)
    return AcceptedBatch(batch_id=prepared.batch_id, status_url=f"/batches/{prepared.batch_id}")


@router.get("/batches")
def list_batches(
    conn: Connection,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    operation: Literal["new_input", "rerun"] | None = None,
    status: Literal["running", "completed", "completed_with_failures", "failed", "interrupted"] | None = None,
    root_batch_id: str | None = None,
):
    return queries.list_batches(
        conn,
        limit=limit,
        offset=offset,
        operation=operation,
        status=status,
        root_batch_id=root_batch_id,
    )


@router.get("/batches/{batch_id}", name="get_batch")
def get_batch(batch_id: str, conn: Connection, runner: Runner):
    result = queries.get_batch(conn, batch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="batch not found")
    result["phase"] = runner.phase(batch_id) if result["status"] == "running" else None
    return result


@router.get("/batches/{batch_id}/items")
def list_items(
    batch_id: str,
    conn: Connection,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Literal["pending", "running", "valid", "failed"] | None = None,
):
    if queries.get_batch(conn, batch_id) is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return queries.list_items(conn, batch_id, limit=limit, offset=offset, status=status)
