"""Canonical user-input model shared by orchestrator and matching."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class InputItem(BaseModel):
    title: str
    country: str
    site_name: str
    gtin: str | None = None
    image_urls: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _country_alias(cls, value: Any) -> Any:
        if isinstance(value, dict) and not value.get("country") and value.get("region"):
            return {**value, "country": value["region"]}
        return value

    @field_validator("title")
    @classmethod
    def _non_empty_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("country", "site_name")
    @classmethod
    def _normalized_key(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("gtin", mode="before")
    @classmethod
    def _optional_gtin(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("image_urls", mode="before")
    @classmethod
    def _coerce_images(cls, value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise ValueError("image_urls must be a list of URLs")
        urls: list[str] = []
        for item in value:
            url = str(item).strip()
            if not url:
                continue
            if not url.lower().startswith(("http://", "https://")):
                raise ValueError(f"image URL must use http/https: {url}")
            if url not in urls:
                urls.append(url)
        return urls
