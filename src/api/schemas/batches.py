from __future__ import annotations

from pydantic import BaseModel, Field

from src.models import InputItem


class NewItemsRequest(BaseModel):
    items: list[InputItem] = Field(min_length=1)
    vision_enabled: bool = False
    concurrency: int | None = Field(default=None, ge=1)


class RerunRequest(BaseModel):
    search_titles: list[str] | None = None
    vision_enabled: bool | None = None
    concurrency: int | None = Field(default=None, ge=1)


class AcceptedBatch(BaseModel):
    batch_id: str
    status_url: str
