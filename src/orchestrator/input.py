from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
from pydantic import ValidationError

from src.models import InputItem


@dataclass(frozen=True, slots=True)
class ParsedRow:
    row_index: int
    raw: dict[str, Any]
    item: InputItem | None
    error: str | None = None


def _parse_image_urls(value: Any) -> Any:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("image_urls must be a JSON array or one URL") from exc
        if not isinstance(parsed, list):
            raise ValueError("image_urls JSON value must be an array")
        return parsed
    return [text]


def _validate_structure(rows: list[dict[str, Any]], columns: set[str]) -> None:
    if not rows:
        raise ValueError("input contains no items")
    required = {"title", "site_name"}
    missing = sorted(required - columns)
    if "country" not in columns and "region" not in columns:
        missing.append("country (or region)")
    if missing:
        raise KeyError(f"columns not found: {', '.join(missing)}")


def load_input(
    source: str | Path | Sequence[InputItem],
) -> tuple[list[ParsedRow], str | None]:
    if not isinstance(source, (str, Path)):
        rows = [item.model_dump() for item in source]
        _validate_structure(rows, set().union(*(row.keys() for row in rows)) if rows else set())
        return [ParsedRow(index, row, InputItem.model_validate(row)) for index, row in enumerate(rows)], None

    path = Path(source)
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        frame = pd.read_excel(path, dtype=str, keep_default_na=False)
        rows = frame.to_dict(orient="records")
        columns = set(map(str, frame.columns))
    elif suffix == ".csv":
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        rows = frame.to_dict(orient="records")
        columns = set(map(str, frame.columns))
    elif suffix == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            raise ValueError("JSON input must be an array of objects")
        rows = value
        columns = set().union(*(row.keys() for row in rows)) if rows else set()
    else:
        raise ValueError("input file must be .xlsx, .csv, or .json")
    _validate_structure(rows, columns)

    parsed: list[ParsedRow] = []
    for index, raw in enumerate(rows):
        candidate = dict(raw)
        try:
            candidate["image_urls"] = _parse_image_urls(candidate.get("image_urls"))
            item = InputItem.model_validate(candidate)
            parsed.append(ParsedRow(index, raw, item))
        except (ValidationError, ValueError) as exc:
            parsed.append(ParsedRow(index, raw, None, str(exc)))
    return parsed, str(path)
