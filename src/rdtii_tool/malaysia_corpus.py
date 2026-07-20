"""Malaysia Zone 1 corpus builder.

This module intentionally only runs official-source discovery/download and does
not invoke Zone 2 provision splitting or RDTII mapping.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rdtii_tool.sources.malaysia.catalog import write_json, write_jsonl
from rdtii_tool.sources.malaysia.lom_adapter import MalaysiaLOMAdapter
from rdtii_tool.sources.malaysia.pdp_adapter import MalaysiaPDPAdapter
from rdtii_tool.zone1.engine import Zone1CorpusEngine


class MalaysiaZone1Builder:
    def __init__(
        self,
        project_root: Path,
        *,
        source: str = "all",
        limit: int | None = None,
        include_repealed: bool = False,
        collection: str | None = None,
        document_id: str | None = None,
        force: bool = False,
    ) -> None:
        self.project_root = project_root
        self.source = source
        self.limit = limit
        self.include_repealed = include_repealed
        self.collection = collection
        self.document_id = document_id
        self.force = force
        self.output_root = project_root / "outputs" / "corpus" / "malaysia"
        self.data_root = project_root / "data" / "legal_sources" / "malaysia"

    def build_zone1(self) -> dict[str, Any]:
        adapters = self._adapters()
        all_results = []
        source_summaries: dict[str, Any] = {}
        for adapter in adapters:
            run = Zone1CorpusEngine(adapter=adapter, project_root=self.project_root, workers=8, force=self.force).run()
            all_results.extend(run["results"])
            source_summaries[adapter.__class__.__name__] = run["summary"]
        summary = self._combined_summary(all_results, source_summaries)
        self._write_combined_outputs(all_results, summary)
        return summary

    def discover_only(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for adapter in self._adapters():
            list(adapter.discover())
            rows.extend(getattr(adapter, "catalogue_rows", []))
            errors.extend(getattr(adapter, "discovery_errors", []))
        summary = {
            "economy": "malaysia",
            "source": self.source,
            "documents_catalogued": len(rows),
            "collections": self._count(rows, "collection"),
            "sources": self._count(rows, "source"),
            "discovery_errors": errors,
        }
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.data_root.mkdir(parents=True, exist_ok=True)
        write_jsonl(self.output_root / "malaysia_catalogue.jsonl", rows)
        write_jsonl(self.data_root / "manifests" / "malaysia_catalogue.jsonl", rows)
        write_json(self.output_root / "malaysia_catalogue_summary.json", summary)
        return summary

    def _adapters(self) -> list[Any]:
        adapters: list[Any] = []
        if self.source in {"all", "lom"}:
            adapters.append(
                MalaysiaLOMAdapter(
                    self.project_root,
                    source="lom",
                    limit=self.limit,
                    include_repealed=self.include_repealed,
                    collection_filter=self.collection,
                    document_id=self.document_id,
                )
            )
        if self.source in {"all", "pdp"}:
            adapters.append(
                MalaysiaPDPAdapter(
                    self.project_root,
                    limit=self.limit,
                    collection_filter=self.collection,
                    document_id=self.document_id,
                )
            )
        return adapters

    def _combined_summary(self, results: list[Any], source_summaries: dict[str, Any]) -> dict[str, Any]:
        success = [result for result in results if result.success]
        failed = [result for result in results if not result.success]
        rows = [dict(result.metadata) for result in success]
        return {
            "economy": "malaysia",
            "source": self.source,
            "documents_catalogued": len(results),
            "documents_available": len(success),
            "documents_failed": len(failed),
            "failure_events": sum(len(result.attempts) for result in results),
            "documents_downloaded_this_run": sum(result.status == "downloaded" for result in success),
            "documents_existing_normalized": sum(result.status == "existing_normalized" for result in success),
            "documents_existing_raw": sum(result.status == "existing_raw" for result in success),
            "collections": self._count(rows, "collection"),
            "sources": self._count(rows, "source"),
            "binding_status": self._count(rows, "binding_status"),
            "source_summaries": source_summaries,
            "zone2_invoked": False,
        }

    def _write_combined_outputs(self, results: list[Any], summary: dict[str, Any]) -> None:
        rows = [dict(result.metadata) for result in results if result.success]
        failures = [
            {
                "document_id": result.document_id,
                "collection": result.document.collection,
                "title": result.document.title,
                "source": result.document.metadata.get("source"),
                "final_error": result.final_error,
                "attempts": result.attempts,
            }
            for result in results
            if not result.success
        ]
        write_jsonl(self.data_root / "manifests" / "malaysia_zone1_manifest.jsonl", rows)
        write_jsonl(self.output_root / "malaysia_source_manifest.jsonl", rows)
        write_jsonl(self.output_root / "malaysia_failed_downloads.jsonl", failures)
        write_json(self.output_root / "malaysia_download_report.json", summary)
        write_json(self.output_root / "malaysia_corpus_summary.json", summary)

    @staticmethod
    def _count(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            value = str(row.get(key) or "")
            if not value:
                continue
            counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items()))

