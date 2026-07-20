"""Malaysia-only P4 source metadata overrides.

These overrides repair numeric LOM principal-act metadata so document-direct
P4 routing can see the source family. They do not encode provision-level
mapping conclusions.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


MALAYSIA_P4_SOURCE_OVERRIDE_VERSION = "rdtii-malaysia-p4-source-overrides-v1"


@lru_cache(maxsize=4)
def _load_override_payload(project_root: str) -> dict[str, Any]:
    path = Path(project_root) / "data" / "source_overrides" / "malaysia_p4.json"
    if not path.exists():
        return {"records": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("override_version") != MALAYSIA_P4_SOURCE_OVERRIDE_VERSION:
        raise RuntimeError(f"Unsupported Malaysia P4 source override version: {path}")
    return payload


def malaysia_p4_override_for_row(row: dict[str, Any], project_root: Path) -> dict[str, Any] | None:
    if str(row.get("economy") or "").strip().casefold() != "malaysia":
        return None
    if str(row.get("collection") or "").strip().casefold() != "principalactupdated":
        return None
    records = _load_override_payload(str(project_root)).get("records") or {}
    for act_number, override in records.items():
        if _row_matches_act_number(row, str(act_number)):
            return dict(override, act_number=str(act_number), override_version=MALAYSIA_P4_SOURCE_OVERRIDE_VERSION)
    return None


def apply_malaysia_p4_override(row: dict[str, Any], project_root: Path) -> dict[str, Any]:
    override = malaysia_p4_override_for_row(row, project_root)
    if not override:
        return row
    updated = dict(row)
    updated["title"] = override["canonical_title"]
    updated["official_number"] = override["law_number"]
    updated["source_family"] = override["source_family"]
    updated["malaysia_p4_override_version"] = override["override_version"]
    updated["malaysia_p4_override_act"] = override["act_number"]
    updated["malaysia_p4_routes"] = list(override.get("p4_routes") or [])
    updated["malaysia_p4_indicators"] = list(override.get("p4_indicators") or [])
    return updated


def malaysia_p4_override_fingerprint(row: dict[str, Any], project_root: Path) -> str:
    override = malaysia_p4_override_for_row(row, project_root)
    if not override:
        return ""
    return "|".join(
        [
            override["override_version"],
            override["act_number"],
            override["canonical_title"],
            override["law_number"],
            override["source_family"],
            ",".join(override.get("p4_indicators") or []),
        ]
    )


def _row_matches_act_number(row: dict[str, Any], act_number: str) -> bool:
    direct_fields = (
        str(row.get("official_number") or ""),
        str(row.get("title") or ""),
    )
    if any(_normalise_act_token(value) in {act_number, f"act {act_number}"} for value in direct_fields):
        return True
    haystack = " ".join(
        str(row.get(key) or "")
        for key in ("document_id", "raw_path", "normalized_path", "pdf_text_path", "source_url", "canonical_url")
    ).casefold()
    patterns = (
        rf"\bact[-_= ]{re.escape(act_number)}\b",
        rf"\bact={re.escape(act_number)}\b",
        rf"\bact%3d{re.escape(act_number)}\b",
    )
    return any(re.search(pattern, haystack) for pattern in patterns)


def _normalise_act_token(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().casefold())
    if re.fullmatch(r"\d+", text):
        return text
    match = re.fullmatch(r"act\s+(\d+)", text)
    if match:
        return f"act {match.group(1)}"
    return text
