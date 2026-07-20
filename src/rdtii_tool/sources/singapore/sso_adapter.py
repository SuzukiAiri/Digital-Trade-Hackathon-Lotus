"""Singapore Statutes Online Zone 1 adapter."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from pypdf import PdfReader

from rdtii_tool.document_models import CandidateURL, IngestionInput
from rdtii_tool.downloaders.sso_html import DIRECT_HTML_HEADERS, SSOHTMLDownloader
from rdtii_tool.ingestion.parser_router import ParserRouter
from rdtii_tool.zone1.models import DocumentRef, DownloadCandidate, DownloadResult, HostPolicy
from rdtii_tool.zone1.storage import write_json, write_jsonl


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]


class _RawStore:
    def __init__(self, raw_dir: Path) -> None:
        self.raw_dir = raw_dir

    def path_for(self, candidate: CandidateURL) -> Path:
        return self.raw_dir / f"{candidate.metadata['document_id']}.html"

    def find_cached(self, candidate: CandidateURL) -> Path | None:
        path = self.path_for(candidate)
        return path if path.exists() and path.stat().st_size > 0 else None

    def save_html(self, candidate: CandidateURL, content: bytes) -> Path:
        path = self.path_for(candidate)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


class SingaporeSSOAdapter:
    economy = "singapore"

    def __init__(self, project_root: Path, *, force: bool = False) -> None:
        self.project_root = project_root
        self.force = force
        self.output_root = project_root / "outputs" / "corpus" / "singapore"
        self.source_root = project_root / "outputs" / "corpus" / "sources"
        self.router = ParserRouter()
        self._html_downloader = SSOHTMLDownloader(_RawStore(self.output_root / "raw" / "_adapter_fragments"), timeout=60.0, fragment_delay=0.1)
        self._manifest_by_id = {
            row.get("document_id"): row
            for row in [
                *_read_jsonl(self.output_root / "manifests" / "acts_manifest.jsonl"),
                *_read_jsonl(self.output_root / "manifests" / "subsidiary_manifest.jsonl"),
            ]
            if row.get("document_id")
        }

    def discover(self) -> Iterable[DocumentRef]:
        for record in _read_jsonl(self.source_root / "singapore_sso_current_acts.jsonl"):
            yield self._document(record, "act")
        for record in _read_jsonl(self.source_root / "singapore_sso_current_subsidiary_legislation.jsonl"):
            yield self._document(record, "subsidiary_legislation")

    def _document(self, record: dict[str, Any], kind: str) -> DocumentRef:
        document_id = self._document_id(record, kind)
        collection = "Act" if kind == "act" else "SubsidiaryLegislation"
        metadata = {
            **record,
            "storage_collection": "acts" if kind == "act" else "subsidiary_legislation",
            "instrument_type": kind,
            "document_type": kind,
            "official_id": str(record.get("law_id") or "").casefold(),
            "legacy_jsonl_paths": [str(path) for path in self._jsonl_paths(document_id, kind)],
            "legacy_raw_paths": [str(path) for path in self._raw_paths(document_id, kind)],
        }
        return DocumentRef(
            economy="Singapore",
            document_id=document_id,
            collection=collection,
            title=str(record.get("official_title") or document_id),
            canonical_url=str(record.get("canonical_url") or ""),
            version_id="current",
            status=str(record.get("status") or "current"),
            metadata=metadata,
        )

    def resolve_current_version(self, document: DocumentRef) -> DocumentRef:
        return document

    def get_download_candidates(self, document: DocumentRef) -> list[DownloadCandidate]:
        candidates = [
            DownloadCandidate(
                url=document.canonical_url,
                format="html",
                source_type="sso_html",
                priority=10,
                headers={**DIRECT_HTML_HEADERS, "Referer": "https://sso.agc.gov.sg/"},
                required=True,
                metadata={"extension": ".html"},
            )
        ]
        for key in ("pdf_url", "download_pdf_url"):
            value = str(document.metadata.get(key) or "").strip()
            if value:
                candidates.append(DownloadCandidate(
                    url=urljoin(document.canonical_url, value),
                    format="pdf",
                    source_type="sso_catalog_pdf",
                    priority=50,
                    headers={**DIRECT_HTML_HEADERS, "Accept": "application/pdf,*/*", "Referer": document.canonical_url},
                    required=False,
                    metadata={"extension": ".pdf"},
                ))
        return candidates

    def validate_response(self, document: DocumentRef, candidate: DownloadCandidate, response: Any) -> bool:
        content = bytes(response.content)
        if candidate.format == "pdf":
            return content.startswith(b"%PDF")
        html = content.decode(getattr(response, "encoding", None) or "utf-8", errors="replace")
        valid, _, reason = SSOHTMLDownloader.validate_legal_html(html, expected_title=document.title)
        return valid or "lazy fragments" in reason.casefold()

    def normalize_response(self, document: DocumentRef, candidate: DownloadCandidate, content: bytes) -> str:
        if candidate.format == "pdf":
            return self._extract_pdf_text(content)
        html = content.decode("utf-8", errors="replace")
        try:
            html, _ = self._html_downloader._hydrate_lazy_fragments(html, page_url=candidate.url)
        except Exception:
            pass
        text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def normalize_file(self, document: DocumentRef, path: Path) -> str:
        if path.suffix.casefold() == ".pdf":
            return self._extract_pdf_text(path.read_bytes())
        return BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser").get_text("\n", strip=True)

    def existing_output_result(self, document: DocumentRef) -> DownloadResult | None:
        for output in self._jsonl_paths(document.document_id, str(document.metadata.get("instrument_type") or "")):
            if output.exists() and output.stat().st_size > 0:
                manifest_row = self._manifest_by_id.get(document.document_id) or {}
                count = int(manifest_row.get("provision_count") or 1)
                raw = None
                metadata = self._manifest(document, raw or Path(""), output, count, "cache_hit", "success", "")
                return DownloadResult(
                    document_id=document.document_id,
                    success=True,
                    selected_candidate=None,
                    raw_path=str(raw) if raw else None,
                    normalized_path=str(output),
                    attempts=[],
                    final_error=None,
                    status="existing_normalized",
                    document=document,
                    metadata=metadata,
                )
        return None

    def materialize_output(self, document: DocumentRef, metadata: dict[str, Any], text: str) -> None:
        kind = str(document.metadata.get("instrument_type") or "")
        output = self._canonical_jsonl_path(document.document_id, kind)
        raw = Path(str(metadata.get("raw_file_path") or ""))
        rows: list[dict[str, Any]] = []
        if raw.exists() and raw.suffix.casefold() == ".html":
            outcome = self.router.route(IngestionInput(input_path=str(raw), source_url=document.canonical_url, source_name="Singapore Statutes Online", source_type="legislation", document_type=kind, content_type="text/html", title=document.title))
            if outcome.document is not None and outcome.document.sections:
                rows = [self._provision_row(document, kind, section) for section in outcome.document.sections]
        elif raw.exists() and raw.suffix.casefold() == ".pdf":
            rows = [self._pdf_document_row(document, kind, raw, document.canonical_url)]
        if not rows:
            rows = self._plain_text_rows(document, kind, text, document.canonical_url)
        write_jsonl(output, rows)

    def host_policy(self) -> HostPolicy:
        return HostPolicy(max_concurrency=2, retryable_statuses={429, 467, 500, 502, 503, 504}, shared_backoff=True, backoff_seconds=30)

    def write_compat_outputs(self, results: list[DownloadResult], summary: dict[str, Any]) -> None:
        acts = [self._compat_manifest(r) for r in results if r.document.collection == "Act"]
        subsidiary = [self._compat_manifest(r) for r in results if r.document.collection == "SubsidiaryLegislation"]
        write_jsonl(self.output_root / "manifests" / "acts_manifest.jsonl", sorted(acts, key=lambda x: x["document_id"]))
        write_jsonl(self.output_root / "manifests" / "subsidiary_manifest.jsonl", sorted(subsidiary, key=lambda x: x["document_id"]))
        act_complete = [r for r in acts if r["parse_status"] == "success"]
        sl_complete = [r for r in subsidiary if r["parse_status"] == "success"]
        build_summary = {
            "acts_catalogued": len(acts),
            "acts_downloaded": len(act_complete),
            "acts_parsed": len(act_complete),
            "act_provisions": sum(int(r.get("provision_count") or 0) for r in act_complete),
            "subsidiary_legislation_catalogued": len(subsidiary),
            "subsidiary_legislation_downloaded": len(sl_complete),
            "subsidiary_legislation_parsed": len(sl_complete),
            "subsidiary_legislation_provisions": sum(int(r.get("provision_count") or 0) for r in sl_complete),
            "documents_failed": summary["documents_failed"],
            "failure_events": summary["failure_events"],
            "failures": summary["documents_failed"],
            "failure_details": [
                {
                    "document_id": r.document_id,
                    "stage": "download_or_parse",
                    "error": r.final_error,
                    "attempts": r.attempts,
                }
                for r in results
                if not r.success
            ],
            "collections": summary["collections"],
        }
        write_json(self.output_root / "manifests" / "build_summary.json", build_summary)

    def _compat_manifest(self, result: DownloadResult) -> dict[str, Any]:
        if result.metadata:
            row = dict(result.metadata)
        else:
            row = self._manifest(result.document, Path(""), self._canonical_jsonl_path(result.document_id, str(result.document.metadata.get("instrument_type") or "")), 0, "failed", "failed", result.final_error or "")
        row.setdefault("document_completeness", "complete" if result.success else "empty")
        row.setdefault("error_type", "" if result.success else "download_failed")
        row.setdefault("final_response_url", row.get("download_url") or result.document.canonical_url)
        return row

    def _manifest(self, document: DocumentRef, raw: Path, output: Path, count: int, download: str, parse: str, error: str) -> dict[str, Any]:
        kind = str(document.metadata.get("instrument_type") or "")
        return {
            "country": "Singapore",
            "collection": "Act" if kind == "act" else "Subsidiary Legislation",
            "instrument_type": kind,
            "title": document.title,
            "official_title": document.title,
            "official_id": str(document.metadata.get("law_id") or "").casefold(),
            "document_id": document.document_id,
            "status": document.status,
            "version_id": "current",
            "effective_date": "",
            "source_url": document.canonical_url,
            "canonical_url": document.canonical_url,
            "authorising_act": document.metadata.get("parent_act", ""),
            "local_path": str(output),
            "raw_html_path": str(raw),
            "jsonl_path": str(output),
            "provision_count": count,
            "download_status": download,
            "parse_status": parse,
            "error": error,
        }

    @staticmethod
    def _document_id(record: dict[str, Any], kind: str) -> str:
        return f"{'sg-act' if kind == 'act' else 'sg-sl'}-{str(record['law_id']).casefold()}"

    def _jsonl_paths(self, document_id: str, kind: str) -> list[Path]:
        if kind == "act":
            return [self.output_root / "acts" / f"{document_id}.jsonl"]
        return [
            self.output_root / "subsidiary_legislation" / f"{document_id}.jsonl",
            self.output_root / "subsidiary" / f"{document_id}.jsonl",
        ]

    def _raw_paths(self, document_id: str, kind: str) -> list[Path]:
        if kind == "act":
            return [
                self.output_root / "raw" / "acts" / f"{document_id}.html",
                self.output_root / "raw" / "acts" / f"{document_id}.pdf",
            ]
        return [
            self.output_root / "raw" / "subsidiary_legislation" / f"{document_id}.html",
            self.output_root / "raw" / "subsidiary_legislation" / f"{document_id}.pdf",
            self.output_root / "raw" / "subsidiary" / f"{document_id}.html",
            self.output_root / "raw" / "subsidiary" / f"{document_id}.pdf",
        ]

    def _canonical_jsonl_path(self, document_id: str, kind: str) -> Path:
        return self.output_root / ("acts" if kind == "act" else "subsidiary_legislation") / f"{document_id}.jsonl"

    @staticmethod
    def _extract_pdf_text(content: bytes) -> str:
        try:
            reader = PdfReader(io.BytesIO(content))
            return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        except Exception:
            return ""

    def _provision_row(self, document: DocumentRef, kind: str, section: Any) -> dict[str, Any]:
        hierarchy = [value for value in (section.part, section.division, section.schedule) if value]
        return {"economy":"Singapore", "document_id":document.document_id, "law_id":str(document.metadata["law_id"]).casefold(), "official_title":document.title, "instrument_type":kind, "status":"current", "canonical_url":document.canonical_url, "provision_id":section.section_id, "hierarchy":hierarchy, "article":section.heading, "text":section.text, "anchor_url":section.url, "provision_type":section.provision_type, "provision_number":section.provision_number, "editorial_annotations":section.editorial_annotations}

    def _plain_text_rows(self, document: DocumentRef, kind: str, text: str, source_url: str) -> list[dict[str, Any]]:
        clean = re.sub(r"\s+", " ", text).strip()
        return [{"economy":"Singapore", "document_id":document.document_id, "law_id":str(document.metadata["law_id"]).casefold(), "official_title":document.title, "instrument_type":kind, "status":"current", "canonical_url":document.canonical_url, "provision_id":"text", "hierarchy":[], "article":"Text", "text":clean, "anchor_url":source_url, "provision_type":"section", "provision_number":"text"}] if clean else []

    def _pdf_document_row(self, document: DocumentRef, kind: str, raw_pdf: Path, source_url: str) -> dict[str, Any]:
        return {
            "record_type": "pdf_document",
            "document_id": document.document_id,
            "economy": "singapore",
            "collection": "Act" if kind == "act" else "SubsidiaryLegislation",
            "title": document.title,
            "official_number": str(document.metadata.get("law_id") or "").casefold(),
            "year": "",
            "language": "en",
            "source_format": "pdf",
            "source_url": source_url,
            "raw_path": str(raw_pdf),
            "prefilter_status": "uncertain",
        }
