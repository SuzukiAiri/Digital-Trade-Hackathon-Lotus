"""Australia Federal Register of Legislation Zone 1 adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from rdtii_tool.australia_corpus import AustraliaFRLCorpusBuilder, FRL_API, FRL_BASE, USER_AGENT
from rdtii_tool.zone1.models import DocumentRef, DownloadCandidate, DownloadResult, HostPolicy
from rdtii_tool.zone1.storage import write_json, write_jsonl


class AustraliaFRLAdapter:
    economy = "australia"

    def __init__(self, project_root: Path, *, force: bool = False, download_all: bool = True) -> None:
        self.project_root = project_root
        self.force = force
        self.download_all = download_all
        self.builder = AustraliaFRLCorpusBuilder(project_root, force=force, download_all=download_all)
        self.full_catalogue = {}

    def discover(self) -> Iterable[DocumentRef]:
        self.builder._ensure_dirs()
        self.builder._write_source_registry()
        capability = self.builder.capability_check()
        write_json(self.builder.output_root / "australia_frl_capability_report.json", capability)
        self.full_catalogue = self.builder.discover_catalogue()
        self.builder.full_catalogue = self.full_catalogue
        self.builder._write_catalogue_manifests(self.full_catalogue.values())
        targets = dict(self.full_catalogue) if self.download_all else self.builder._target_documents(self.full_catalogue)
        for title in targets.values():
            metadata = title.to_manifest()
            metadata.update({
                "storage_collection": "frl",
                "instrument_type": title.document_type,
                "document_type": title.document_type,
                "official_id": title.register_id,
                "register_id": title.register_id,
                "source_url": title.source_url,
            })
            yield DocumentRef(
                economy="Australia",
                document_id=title.register_id,
                collection=title.collection,
                title=title.title,
                canonical_url=title.source_url,
                version_id="current",
                status=title.status,
                metadata=metadata,
            )

    def resolve_current_version(self, document: DocumentRef) -> DocumentRef:
        attempts: list[dict[str, Any]] = []
        title = self.builder._title_from_id(str(document.metadata.get("register_id") or document.document_id), fallback_title=document.title)
        if title is None:
            return document
        version = self.builder._api_current_version(title, attempts)
        docs = self.builder._api_primary_documents(title, version, attempts)
        metadata = dict(document.metadata)
        metadata["frl_version"] = version
        metadata["frl_documents"] = docs
        if version.get("registerId"):
            metadata["current_version_register_id"] = version.get("registerId")
        return DocumentRef(
            economy=document.economy,
            document_id=document.document_id,
            collection=document.collection,
            title=document.title,
            canonical_url=document.canonical_url,
            version_id=str(version.get("compilationNumber") or version.get("registerId") or document.version_id or "current"),
            status=document.status,
            metadata=metadata,
        )

    def get_download_candidates(self, document: DocumentRef) -> list[DownloadCandidate]:
        docs = list(document.metadata.get("frl_documents") or [])
        priority = {"Epub": 0, "Word": 1, "Pdf": 2}
        candidates: list[DownloadCandidate] = []
        for doc in docs:
            fmt = self.builder._doc_format(doc)
            if fmt == "NameOnly":
                continue
            extension = str(doc.get("extension") or self.builder._extension_for_format(fmt)).lower()
            register_id = str(doc.get("registerId") or document.document_id)
            url = self.builder._api_document_url(doc)
            candidates.append(DownloadCandidate(
                url=url,
                format={"Epub": "epub", "Word": "word", "Pdf": "pdf"}.get(fmt, fmt.casefold()),
                source_type="frl_api",
                priority=priority.get(fmt, 99),
                headers={"Accept": "application/octet-stream", "User-Agent": USER_AGENT},
                required=True,
                metadata={
                    "frl_document": doc,
                    "extension": extension,
                    "filename": f"{document.document_id}_{register_id}_{fmt.casefold()}_{doc.get('volumeNumber', 0)}{extension}",
                },
            ))
        candidates.append(DownloadCandidate(
            url=f"{FRL_BASE}/{document.document_id}/latest",
            format="html",
            source_type="frl_page_fallback",
            priority=99,
            headers={"Accept": "text/html,*/*", "User-Agent": USER_AGENT},
            required=False,
            metadata={"extension": ".html"},
        ))
        return candidates

    def existing_output_result(self, document: DocumentRef) -> DownloadResult | None:
        register_id = str(document.metadata.get("register_id") or document.document_id)
        norm_path = self.builder.data_root / "normalized" / "frl" / f"{register_id}.txt"
        if not norm_path.exists() or norm_path.stat().st_size <= 0:
            return None
        raw_path = self.builder._existing_raw_for_register_id(register_id)
        raw_html = self.builder.data_root / "raw" / "frl" / "html" / f"{register_id}.html"
        self.builder._ensure_synthetic_html_from_normalized(title=document.title, norm_path=norm_path, raw_html=raw_html)
        metadata_path = self.builder.data_root / "metadata" / "frl" / f"{register_id}.json"
        metadata: dict[str, Any] = {}
        if metadata_path.exists():
            try:
                import json
                metadata = json.loads(metadata_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                metadata = {}
        metadata.update({
            "economy": "Australia",
            "country": "Australia",
            "document_id": document.document_id,
            "title": document.title,
            "official_title": document.title,
            "register_id": register_id,
            "official_id": register_id,
            "collection": document.collection,
            "status": document.status,
            "source_url": document.canonical_url,
            "canonical_url": document.canonical_url,
            "download_status": "cache_hit",
            "parse_status": "success",
            "raw_file_path": str(raw_path or raw_html),
            "raw_html_path": str(raw_html),
            "normalized_file_path": str(norm_path),
            "local_path": str(norm_path),
            "metadata_path": str(metadata_path),
            "normalized_char_count": norm_path.stat().st_size,
            "api_download_status": metadata.get("api_download_status") or "cache_hit",
        })
        return DownloadResult(
            document_id=document.document_id,
            success=True,
            selected_candidate=None,
            raw_path=str(raw_path or raw_html),
            normalized_path=str(norm_path),
            attempts=[],
            final_error=None,
            status="existing_normalized",
            document=document,
            metadata=metadata,
        )

    def validate_response(self, document: DocumentRef, candidate: DownloadCandidate, response: Any) -> bool:
        content = bytes(response.content)
        if candidate.format == "pdf":
            return content.startswith(b"%PDF")
        if candidate.format in {"epub", "word"}:
            return len(content) > 0
        if candidate.format == "html":
            text = content.decode("utf-8", errors="replace")
            return document.document_id in text or document.title.casefold() in text.casefold()
        return bool(content)

    def normalize_response(self, document: DocumentRef, candidate: DownloadCandidate, content: bytes) -> str:
        if candidate.source_type == "frl_api":
            doc = candidate.metadata.get("frl_document") or {}
            fmt = self.builder._doc_format(doc)
            extension = str(doc.get("extension") or candidate.metadata.get("extension") or "")
            return self.builder._normalise_api_content(content, fmt=fmt, extension=extension, title=document.title)
        return self.builder._normalise_html_document(content.decode("utf-8", errors="replace"))

    def normalize_file(self, document: DocumentRef, path: Path) -> str:
        content = path.read_bytes()
        suffix = path.suffix.casefold()
        if suffix == ".pdf":
            return self.builder._normalise_pdf_bytes(content)
        if suffix == ".epub":
            return self.builder._normalise_epub(content)
        if suffix == ".rtf":
            return self.builder._normalise_rtf(content.decode("utf-8", errors="replace"))
        if suffix in {".docx", ".doc"}:
            return self.builder._normalise_api_content(content, fmt="Word", extension=suffix, title=document.title)
        return self.builder._normalise_html_document(content.decode("utf-8", errors="replace"))

    def host_policy(self) -> HostPolicy:
        return HostPolicy(max_concurrency=8, retryable_statuses={429, 500, 502, 503, 504}, shared_backoff=True, backoff_seconds=5)

    def write_compat_outputs(self, results: list[DownloadResult], summary: dict[str, Any]) -> None:
        rows = [self._compat_row(result) for result in results if result.success]
        rows = sorted(rows, key=lambda r: (str(r.get("collection")), str(r.get("title")).casefold()))
        write_jsonl(self.builder.data_root / "manifests" / "australia_downloaded_manifest.jsonl", rows)
        write_jsonl(self.builder.output_root / "australia_source_manifest.jsonl", rows)
        final_failures = [
            {
                "title_id": r.document_id,
                "register_id": r.document_id,
                "collection": r.document.collection,
                "title": r.document.title,
                "final_status": "failed",
                "attempts": r.attempts,
                "final_error": r.final_error,
            }
            for r in results
            if not r.success
        ]
        write_jsonl(self.builder.output_root / "australia_failed_downloads.jsonl", final_failures)
        coverage = self._coverage(results, summary)
        write_json(self.builder.output_root / "australia_download_report.json", coverage)
        write_json(self.builder.output_root / "australia_source_coverage_report.json", coverage["coverage"])
        write_json(self.builder.output_root / "australia_corpus_summary.json", coverage)

    def _compat_row(self, result: DownloadResult) -> dict[str, Any]:
        row = dict(result.metadata)
        row.setdefault("economy", "Australia")
        row.setdefault("country", "Australia")
        row.setdefault("portal_name", "Australian Federal Register of Legislation")
        row.setdefault("portal_code", "australia_frl")
        row.setdefault("jurisdiction", "Commonwealth of Australia")
        row.setdefault("authority", "Office of Parliamentary Counsel")
        row.setdefault("official", True)
        row.setdefault("register_id", result.document_id)
        row.setdefault("official_id", result.document_id)
        row.setdefault("series_id", result.document_id)
        row.setdefault("latest_version_url", result.document.canonical_url)
        row.setdefault("text_url", row.get("download_url") or result.document.canonical_url)
        row.setdefault("api_download_status", "success" if result.status == "downloaded" else "cache_hit")
        row.setdefault("raw_html_path", row.get("raw_file_path") if str(row.get("raw_file_path", "")).endswith(".html") else "")
        row.setdefault("pdf_download_status", "success" if row.get("source_format") == "pdf" else "")
        row.setdefault("html_download_status", "success" if row.get("source_format") in {"html", "epub"} else "")
        return row

    def _coverage(self, results: list[DownloadResult], summary: dict[str, Any]) -> dict[str, Any]:
        def count(collection: str, success: bool | None = None) -> int:
            items = [r for r in results if r.document.collection == collection]
            if success is None:
                return len(items)
            return sum(r.success == success for r in items)

        acts = count("Act", True)
        li = count("LegislativeInstrument", True)
        ni = count("NotifiableInstrument", True)
        coverage = {
            "acts_discovered": count("Act"),
            "acts_downloaded": acts,
            "legislative_instruments_discovered": count("LegislativeInstrument"),
            "legislative_instruments_downloaded": li,
            "notifiable_instruments_discovered": count("NotifiableInstrument"),
            "notifiable_instruments_downloaded": ni,
            "current_in_force_documents": len(results),
            "documents_discovered": len(results),
            "documents_downloaded": summary["documents_available"],
            "documents_skipped_existing": summary["documents_existing_normalized"] + summary["documents_existing_raw"],
            "documents_failed": summary["documents_failed"],
            "failed_documents": summary["documents_failed"],
            "failure_events": summary["failure_events"],
            "full_download_scope_enabled": self.download_all,
            "collection_coverage": summary["collections"],
            "format_success": summary["format_success"],
        }
        return {
            "economy": "Australia",
            "documents_downloaded": summary["documents_available"],
            "failed_downloads": summary["documents_failed"],
            "acts_discovered": coverage["acts_discovered"],
            "acts_downloaded": acts,
            "legislative_instruments_discovered": coverage["legislative_instruments_discovered"],
            "legislative_instruments_downloaded": li,
            "coverage": coverage,
            **coverage,
        }
