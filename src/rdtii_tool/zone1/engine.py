"""Shared Zone 1 corpus acquisition engine."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from rdtii_tool.zone1.manifest import build_summary, write_result_files
from rdtii_tool.zone1.models import DocumentRef, DownloadCandidate, DownloadResult, HostPolicy, PortalAdapter
from rdtii_tool.zone1.progress import Zone1Progress
from rdtii_tool.zone1.storage import Zone1Storage, sha256_file


class _HostLimiter:
    def __init__(self, policy: HostPolicy) -> None:
        self.policy = policy
        self.semaphore = threading.Semaphore(max(policy.max_concurrency, 1))
        self.lock = threading.Lock()
        self.backoff_until = 0.0

    def wait(self) -> None:
        if not self.policy.shared_backoff:
            return
        while True:
            with self.lock:
                wait = self.backoff_until - time.monotonic()
            if wait <= 0:
                return
            time.sleep(min(wait, 5.0))

    def backoff(self, seconds: float | None = None) -> None:
        if not self.policy.shared_backoff:
            return
        with self.lock:
            self.backoff_until = max(self.backoff_until, time.monotonic() + (seconds or self.policy.backoff_seconds))


class Zone1CorpusEngine:
    def __init__(
        self,
        *,
        adapter: PortalAdapter,
        project_root: Path,
        workers: int = 8,
        force: bool = False,
    ) -> None:
        self.adapter = adapter
        self.project_root = project_root
        self.storage = Zone1Storage(project_root, adapter.economy)
        self.workers = workers
        self.force = force
        self.session = requests.Session()
        http_adapter = HTTPAdapter(pool_connections=workers * 2, pool_maxsize=workers * 2, max_retries=0, pool_block=False)
        self.session.mount("https://", http_adapter)
        self.session.mount("http://", http_adapter)
        self.host_limiters: dict[str, _HostLimiter] = {}

    def run(self) -> dict[str, Any]:
        documents = list(self.adapter.discover())
        progress = Zone1Progress(label=f"{self.adapter.economy.title()} Zone 1", total=len(documents))
        results: list[DownloadResult] = []
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(self._process_document, document, progress): document for document in documents}
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                progress.add("success" if result.success else "failed", 1)
        progress.maybe_print(force=True)
        summary = build_summary(self.adapter.economy, results, discovered=len(documents))
        write_result_files(self.storage.output_root, self.storage.data_root, results, summary)
        if hasattr(self.adapter, "write_compat_outputs"):
            self.adapter.write_compat_outputs(results, summary)  # type: ignore[attr-defined]
        return {"summary": summary, "results": results}

    def _process_document(self, document: DocumentRef, progress: Zone1Progress) -> DownloadResult:
        if not self.force:
            cached = self._use_cache(document, progress)
            if cached:
                return cached

        existing_raw = self.storage.existing_raw(document)
        if existing_raw is not None and not self.force:
            parsed = self._normalize_existing_raw(document, existing_raw, progress)
            if parsed:
                return parsed

        try:
            document = self.adapter.resolve_current_version(document)
        except Exception as exc:
            return self._failed(document, [], f"resolve_current_version_failed: {exc}")

        attempts: list[dict[str, Any]] = []
        candidates = sorted(self.adapter.get_download_candidates(document), key=lambda c: c.priority)
        progress.add("queued", 1)
        for candidate in candidates:
            response = self._fetch(candidate, attempts)
            if response is None:
                continue
            if not self.adapter.validate_response(document, candidate, response):
                attempts.append(self._attempt(candidate, response.status_code, "response_validation_failed"))
                continue
            content = bytes(response.content)
            try:
                text = self.adapter.normalize_response(document, candidate, content)
            except Exception as exc:
                attempts.append(self._attempt(candidate, response.status_code, f"normalize_failed: {exc}"))
                continue
            if not text.strip():
                attempts.append(self._attempt(candidate, response.status_code, "normalize_empty"))
                continue
            raw_path = self.storage.save_raw(document, candidate, content)
            normalized_path = self.storage.save_normalized(document, text)
            progress.add("downloaded", 1)
            progress.add("normalized", 1)
            metadata = self._metadata(document, candidate, raw_path, normalized_path, text, "downloaded")
            self.storage.save_metadata(document, metadata)
            if hasattr(self.adapter, "materialize_output"):
                self.adapter.materialize_output(document, metadata, text)  # type: ignore[attr-defined]
            return DownloadResult(
                document_id=document.document_id,
                success=True,
                selected_candidate=candidate,
                raw_path=str(raw_path),
                normalized_path=str(normalized_path),
                attempts=attempts,
                final_error=None,
                status="downloaded",
                document=document,
                metadata=metadata,
            )

        return self._failed(document, attempts, "no_candidate_succeeded")

    def _use_cache(self, document: DocumentRef, progress: Zone1Progress) -> DownloadResult | None:
        if hasattr(self.adapter, "existing_output_result"):
            result = self.adapter.existing_output_result(document)  # type: ignore[attr-defined]
            if result is not None:
                progress.add("existing_normalized", 1)
                return result
        normalized = self.storage.existing_normalized(document)
        if normalized is None:
            return None
        raw = self.storage.existing_raw(document)
        metadata = self._metadata(document, None, raw, normalized, normalized.read_text(encoding="utf-8", errors="replace"), "existing_normalized")
        progress.add("existing_normalized", 1)
        return DownloadResult(
            document_id=document.document_id,
            success=True,
            selected_candidate=None,
            raw_path=str(raw) if raw else None,
            normalized_path=str(normalized),
            attempts=[],
            final_error=None,
            status="existing_normalized",
            document=document,
            metadata=metadata,
        )

    def _normalize_existing_raw(self, document: DocumentRef, raw: Path, progress: Zone1Progress) -> DownloadResult | None:
        if not hasattr(self.adapter, "normalize_file"):
            return None
        try:
            text = self.adapter.normalize_file(document, raw)  # type: ignore[attr-defined]
        except Exception:
            return None
        if not text.strip():
            return None
        normalized = self.storage.save_normalized(document, text)
        metadata = self._metadata(document, None, raw, normalized, text, "existing_raw")
        self.storage.save_metadata(document, metadata)
        if hasattr(self.adapter, "materialize_output"):
            self.adapter.materialize_output(document, metadata, text)  # type: ignore[attr-defined]
        progress.add("existing_raw", 1)
        return DownloadResult(
            document_id=document.document_id,
            success=True,
            selected_candidate=None,
            raw_path=str(raw),
            normalized_path=str(normalized),
            attempts=[],
            final_error=None,
            status="existing_raw",
            document=document,
            metadata=metadata,
        )

    def _fetch(self, candidate: DownloadCandidate, attempts: list[dict[str, Any]]) -> requests.Response | None:
        host = urlsplit(candidate.url).hostname or ""
        limiter = self.host_limiters.setdefault(host, _HostLimiter(self.adapter.host_policy()))
        last_error = ""
        for attempt_index in range(3):
            limiter.wait()
            with limiter.semaphore:
                try:
                    response = self.session.get(candidate.url, headers=candidate.headers or None, timeout=(10, 60), allow_redirects=True)
                except requests.RequestException as exc:
                    last_error = str(exc)
                    attempts.append(self._attempt(candidate, None, last_error))
                    time.sleep(0.5 * (attempt_index + 1))
                    continue
            if response.status_code == 200:
                return response
            attempts.append(self._attempt(candidate, response.status_code, response.text[:300] if hasattr(response, "text") else "non_200"))
            policy = self.adapter.host_policy()
            if response.status_code in policy.retryable_statuses and attempt_index < 2:
                limiter.backoff(policy.backoff_seconds * (attempt_index + 1))
                continue
            return None
        if last_error:
            attempts.append(self._attempt(candidate, None, last_error))
        return None

    @staticmethod
    def _attempt(candidate: DownloadCandidate, status: int | None, error: str) -> dict[str, Any]:
        return {
            "url": candidate.url,
            "format": candidate.format,
            "source_type": candidate.source_type,
            "status": status,
            "error": error,
            "optional": not candidate.required,
        }

    def _failed(self, document: DocumentRef, attempts: list[dict[str, Any]], error: str) -> DownloadResult:
        return DownloadResult(
            document_id=document.document_id,
            success=False,
            selected_candidate=None,
            raw_path=None,
            normalized_path=None,
            attempts=attempts,
            final_error=error,
            status="failed",
            document=document,
            metadata={
                "document_id": document.document_id,
                "title": document.title,
                "official_title": document.title,
                "collection": document.collection,
                "status": document.status,
                "download_status": "failed",
                "parse_status": "failed",
                "error": error,
            },
        )

    def _metadata(
        self,
        document: DocumentRef,
        candidate: DownloadCandidate | None,
        raw_path: Path | None,
        normalized_path: Path,
        text: str,
        status: str,
    ) -> dict[str, Any]:
        row = {
            "economy": document.economy,
            "country": document.economy.title(),
            "document_id": document.document_id,
            "title": document.title,
            "official_title": document.title,
            "collection": document.collection,
            "instrument_type": document.metadata.get("instrument_type") or document.collection,
            "document_type": document.metadata.get("document_type") or document.collection,
            "official_id": document.metadata.get("official_id") or document.document_id,
            "register_id": document.metadata.get("register_id") or document.metadata.get("official_id") or document.document_id,
            "status": document.status,
            "version_id": document.version_id or "current",
            "source_url": document.canonical_url,
            "canonical_url": document.canonical_url,
            "download_status": "success" if status == "downloaded" else "cache_hit",
            "parse_status": "success",
            "raw_file_path": str(raw_path) if raw_path else "",
            "normalized_file_path": str(normalized_path),
            "local_path": str(normalized_path),
            "metadata_path": str(self.storage.metadata_path(document)),
            "normalized_char_count": len(text),
            "sha256": sha256_file(raw_path) if raw_path and raw_path.exists() else "",
            "source_format": candidate.format if candidate else "",
            "source_type": candidate.source_type if candidate else "cache",
            "download_url": candidate.url if candidate else document.canonical_url,
            "error": "",
        }
        row.update(document.metadata)
        return row

