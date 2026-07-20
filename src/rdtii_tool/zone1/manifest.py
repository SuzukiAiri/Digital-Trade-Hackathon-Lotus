"""Shared Zone 1 manifest and summary helpers."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from rdtii_tool.zone1.models import DownloadResult
from rdtii_tool.zone1.storage import write_json, write_jsonl


def build_summary(economy: str, results: list[DownloadResult], *, discovered: int) -> dict[str, Any]:
    success = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    failure_events = sum(len(r.attempts) for r in results)
    collections: dict[str, dict[str, int]] = defaultdict(lambda: {"catalogued": 0, "available": 0, "failed": 0})
    for result in results:
        collection = result.document.collection
        collections[collection]["catalogued"] += 1
        if result.success:
            collections[collection]["available"] += 1
        else:
            collections[collection]["failed"] += 1
    status_counts = Counter(r.status for r in results)
    format_counts = Counter(
        (r.selected_candidate.format if r.selected_candidate else "cache")
        for r in success
    )
    return {
        "economy": economy,
        "documents_catalogued": discovered,
        "documents_available": len(success),
        "documents_failed": len(failed),
        "failure_events": failure_events,
        "documents_existing_normalized": status_counts.get("existing_normalized", 0),
        "documents_existing_raw": status_counts.get("existing_raw", 0),
        "documents_downloaded_this_run": status_counts.get("downloaded", 0),
        "documents_pending": 0,
        "collections": dict(collections),
        "format_success": dict(format_counts),
    }


def write_result_files(output_root: Path, data_root: Path, results: list[DownloadResult], summary: dict[str, Any]) -> None:
    rows = [r.metadata for r in results if r.metadata]
    failures = [
        {
            "document_id": r.document_id,
            "collection": r.document.collection,
            "title": r.document.title,
            "canonical_url": r.document.canonical_url,
            "final_status": "failed",
            "final_error": r.final_error,
            "attempts": r.attempts,
        }
        for r in results
        if not r.success
    ]
    write_jsonl(data_root / "manifests" / "zone1_manifest.jsonl", rows)
    write_jsonl(output_root / "source_manifest.jsonl", rows)
    write_jsonl(output_root / "failed_downloads.jsonl", failures)
    write_json(output_root / "download_report.json", summary)
    write_json(output_root / "source_coverage_report.json", summary)

