"""Offline P4 treaty-status resolver backed by an auditable local registry."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from .models import P4TreatyStatus


P4_TREATY_REGISTRY_VERSION = "rdtii-p4-treaty-registry-v1"
P4_TREATY_INDICATORS = ("P4-I4", "P4-I7", "P4-I8")


def load_p4_treaty_status(project_root: Path, economy: str) -> list[dict]:
    path = project_root / "data" / "external_status" / "p4_treaties.json"
    economy_name = {
        "singapore": "Singapore",
        "australia": "Australia",
        "malaysia": "Malaysia",
    }.get(economy.casefold(), economy)
    if not path.exists():
        return [_missing_row(economy_name, indicator, "registry_missing") for indicator in P4_TREATY_INDICATORS]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [
            _missing_row(economy_name, indicator, f"registry_unreadable:{type(exc).__name__}")
            for indicator in P4_TREATY_INDICATORS
        ]
    if payload.get("registry_version") != P4_TREATY_REGISTRY_VERSION:
        return [_missing_row(economy_name, indicator, "registry_version_mismatch") for indicator in P4_TREATY_INDICATORS]

    by_indicator = {
        str(row.get("indicator_id") or ""): row
        for row in payload.get("records", [])
        if str(row.get("economy") or "").casefold() == economy_name.casefold()
    }
    rows = []
    for indicator in P4_TREATY_INDICATORS:
        raw = by_indicator.get(indicator)
        if raw is None:
            rows.append(_missing_row(economy_name, indicator, "registry_record_missing"))
            continue
        try:
            status = P4TreatyStatus.model_validate(raw)
        except Exception as exc:
            rows.append(_missing_row(economy_name, indicator, f"registry_record_invalid:{type(exc).__name__}"))
            continue
        reason = validate_p4_treaty_status(status)
        if reason:
            rows.append(_missing_row(economy_name, indicator, reason, raw))
            continue
        rows.append(p4_treaty_status_row(status))
    return rows


def validate_p4_treaty_status(status: P4TreatyStatus) -> str:
    if status.status not in {"party", "not_party"}:
        return "status_uncertain"
    if not status.effective_date and not status.last_checked:
        return "effective_date_and_last_checked_missing"
    if not status.status_text.strip():
        return "status_text_missing"
    parsed = urlparse(status.official_source_url)
    host = parsed.netloc.casefold()
    if parsed.scheme != "https" or not (host == "wipo.int" or host.endswith(".wipo.int")):
        return "official_wipo_source_missing"
    return ""


def p4_treaty_status_row(status: P4TreatyStatus, *, decision_source: str = "local_registry") -> dict:
    return {
        **status.model_dump(),
        "rdtii_score": 0 if status.status == "party" else 1,
        "pillar_id": 4,
        "task_id": f"external-status:{status.economy.casefold()}:{status.indicator_id}",
        "task_type": "external_status",
        "route_topic": "P4_TREATY_STATUS",
        "queue_type": "none",
        "result_code": None,
        "review_required": False,
        "registry_version": P4_TREATY_REGISTRY_VERSION,
        "decision_source": decision_source,
    }


def _missing_row(economy: str, indicator: str, reason: str, raw: dict | None = None) -> dict:
    raw = raw or {}
    row = {
        "economy": economy,
        "pillar_id": 4,
        "indicator_id": indicator,
        "indicator": indicator,
        "task_id": f"external-status:{economy.casefold()}:{indicator}",
        "route_topic": "P4_TREATY_STATUS",
        "document_id": f"p4-status-{indicator.casefold()}-{economy.casefold()}",
        "law_title": str(raw.get("instrument") or "Treaty status"),
        "focal_provision_id": "Treaty status",
        "focal_quote": str(raw.get("status_text") or ""),
        "source_url": str(raw.get("official_source_url") or ""),
        "instrument": str(raw.get("instrument") or ""),
        "status": "uncertain",
        "accession_or_ratification_date": str(raw.get("accession_or_ratification_date") or ""),
        "effective_date": str(raw.get("effective_date") or ""),
        "official_source_url": str(raw.get("official_source_url") or ""),
        "last_checked": str(raw.get("last_checked") or ""),
        "source_note": str(raw.get("source_note") or ""),
        "status_text": str(raw.get("status_text") or ""),
        "task_type": "external_status",
        "queue_type": "external_source",
        "result_code": "EXTERNAL_SOURCE_UNAVAILABLE",
        "review_required": True,
        "rdtii_score": None,
        "external_source_reason": reason,
        "registry_version": P4_TREATY_REGISTRY_VERSION,
    }
    row["reviewer_attributes"] = {
        key: row[key]
        for key in (
            "instrument",
            "status",
            "accession_or_ratification_date",
            "effective_date",
            "official_source_url",
            "last_checked",
            "source_note",
            "status_text",
        )
    }
    return row
