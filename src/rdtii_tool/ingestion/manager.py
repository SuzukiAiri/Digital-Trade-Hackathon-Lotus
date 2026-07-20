"""Ingest downloaded or local Singapore SSO HTML documents."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from rdtii_tool.document_models import (
    CandidateURL,
    DownloadResult,
    IngestionInput,
)
from rdtii_tool.ingestion.corpus_writer import SectionCorpusWriter
from rdtii_tool.ingestion.parser_router import ParseOutcome, ParserRouter
from rdtii_tool.storage.document_store import SUCCESS_STATUSES


SUPPORTED_SUFFIXES = {".html", ".htm"}


class DocumentIngestionManager:
    """Coordinate SSO parsing and per-source corpus output."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        country: str = "SG",
        parser_router: ParserRouter | None = None,
        corpus_writer: SectionCorpusWriter | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.country = country.upper()
        self.parser_router = parser_router or ParserRouter()
        self.corpus_writer = corpus_writer or SectionCorpusWriter(
            self.output_dir,
            country=self.country,
        )

    def ingest_downloaded(
        self,
        *,
        download_log_path: str | Path | None = None,
        candidate_store_path: str | Path | None = None,
    ) -> list[ParseOutcome]:
        started_at = self._now()
        download_log = Path(
            download_log_path or self.output_dir / "download_log.jsonl"
        )
        candidate_path = Path(
            candidate_store_path
            or self.output_dir / "discovered_urls.jsonl"
        )
        candidates = self._load_candidates(candidate_path)
        records = self._records_from_download_log(download_log, candidates)
        outcomes = [self.parser_router.route(record) for record in records]
        self.corpus_writer.write(outcomes, started_at=started_at)
        return outcomes

    def ingest_local(self, path: str | Path) -> list[ParseOutcome]:
        started_at = self._now()
        target = Path(path)
        if not target.exists():
            records = [
                IngestionInput(
                    input_path=str(target),
                    notes="Local ingestion target does not exist.",
                )
            ]
        elif target.is_dir():
            records = [
                self._local_record(file_path)
                for file_path in sorted(target.rglob("*"))
                if file_path.is_file()
                and file_path.suffix.casefold() in SUPPORTED_SUFFIXES
            ]
        else:
            records = [self._local_record(target)]

        outcomes = [self.parser_router.route(record) for record in records]
        self.corpus_writer.write(outcomes, started_at=started_at)
        return outcomes

    def _records_from_download_log(
        self,
        path: Path,
        candidates: dict[str, CandidateURL],
    ) -> list[IngestionInput]:
        records = []
        if not path.exists():
            return records
        for payload in self._read_jsonl(path):
            try:
                result = DownloadResult.from_json_dict(payload)
            except (TypeError, ValueError):
                continue
            saved_path = Path(result.saved_path) if result.saved_path else None
            if (
                result.status not in SUCCESS_STATUSES
                or saved_path is None
                or not saved_path.is_file()
            ):
                continue
            candidate = candidates.get(result.normalized_url)
            records.append(self._record_from_download(result, candidate))
        return records

    def _record_from_download(
        self,
        result: DownloadResult,
        candidate: CandidateURL | None,
    ) -> IngestionInput:
        metadata = dict(candidate.metadata) if candidate else {}
        source_url = result.source_url or (
            candidate.url if candidate is not None else result.candidate_url
        )
        saved_path = Path(result.saved_path)
        return IngestionInput(
            input_path=result.saved_path,
            source_url=source_url,
            source_name=(
                candidate.source_name if candidate else result.source_name
            ),
            source_type=candidate.source_type if candidate else "legislation",
            legal_rank=str(metadata.get("legal_rank", "")),
            document_type=(
                candidate.document_type
                if candidate
                else self._document_type(source_url)
            ),
            content_type=result.content_type or "text/html",
            title=candidate.title if candidate else saved_path.stem,
            notes=str(metadata.get("notes", "")),
            metadata=metadata,
        )

    def _local_record(self, path: Path) -> IngestionInput:
        metadata = self._sidecar_metadata(path)
        inferred = self._infer_local_source(path)
        combined = {**inferred, **metadata}
        source_url = str(combined.get("source_url", "")).strip()
        notes = str(combined.get("notes", "")).strip()
        if not source_url:
            notes = " ".join(
                part
                for part in (
                    notes,
                    "Local SSO HTML ingested without a confirmed original URL.",
                )
                if part
            )
        return IngestionInput(
            input_path=str(path),
            source_url=source_url,
            source_name=str(
                combined.get(
                    "source_name",
                    "Singapore Statutes Online",
                )
            ),
            source_type=str(combined.get("source_type", "legislation")),
            legal_rank=str(combined.get("legal_rank", "")),
            document_type=str(
                combined.get(
                    "document_type",
                    self._document_type(source_url),
                )
            ),
            content_type="text/html",
            title=str(combined.get("title", path.stem)),
            notes=notes,
            metadata=combined,
        )

    @staticmethod
    def _infer_local_source(path: Path) -> dict[str, Any]:
        sample = path.read_text(
            encoding="utf-8",
            errors="replace",
        )[:200000]
        match = re.search(
            r"https://sso\.agc\.gov\.sg/(act|sl)/[^\"'\\s<]+",
            sample,
            flags=re.IGNORECASE,
        )
        source_url = match.group(0) if match else ""
        return {
            "source_url": source_url,
            "source_name": "Singapore Statutes Online",
            "source_type": "legislation",
            "document_type": DocumentIngestionManager._document_type(
                source_url
            ),
        }

    @staticmethod
    def _sidecar_metadata(path: Path) -> dict[str, Any]:
        candidates = (
            path.with_suffix(f"{path.suffix}.json"),
            path.with_suffix(".metadata.json"),
            path.parent / f"{path.name}.metadata.json",
        )
        for sidecar in candidates:
            if not sidecar.exists():
                continue
            try:
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                return payload
        return {}

    @staticmethod
    def _load_candidates(path: Path) -> dict[str, CandidateURL]:
        candidates = {}
        if not path.exists():
            return candidates
        for payload in DocumentIngestionManager._read_jsonl(path):
            try:
                candidate = CandidateURL.from_json_dict(payload)
            except (TypeError, ValueError):
                continue
            candidates[candidate.normalized_url] = candidate
        return candidates

    @staticmethod
    def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    yield payload

    @staticmethod
    def _document_type(source_url: str) -> str:
        lowered = source_url.casefold()
        if "/sl/" in lowered:
            return "subsidiary_legislation"
        if "/act/" in lowered:
            return "act"
        return ""

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
