"""Shared downloader contract and HTTP error classification."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from rdtii_tool.document_models import CandidateURL, DownloadResult
from rdtii_tool.storage.document_store import DocumentStore


DEFAULT_USER_AGENT = (
    "RDTII-Singapore-Acquisition/0.3 "
    "(polite official-document downloader; sequential requests)"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: str | Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class Downloader(ABC):
    """Download one official candidate into the document store."""

    method_name = ""

    def __init__(
        self,
        document_store: DocumentStore,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
        prefer_cache: bool = True,
    ) -> None:
        self.document_store = document_store
        self.session = session or requests.Session()
        self.timeout = timeout
        self.prefer_cache = prefer_cache
        if hasattr(self.session, "headers"):
            self.session.headers.setdefault("User-Agent", user_agent)
            self.session.headers.setdefault(
                "Accept",
                "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            )

    @abstractmethod
    def download(self, candidate: CandidateURL) -> DownloadResult:
        """Download one candidate without raising network errors."""

    def _cached_result(
        self,
        candidate: CandidateURL,
        path: Path,
        *,
        status: str = "cache_hit",
        error_type: str = "",
        error_message: str = "",
    ) -> DownloadResult:
        return DownloadResult(
            candidate_url=candidate.url,
            normalized_url=candidate.normalized_url,
            source_name=candidate.source_name,
            method=self.method_name,
            status=status,
            error_type=error_type,
            error_message=error_message,
            saved_path=str(path),
            content_type=self._content_type_for_path(path),
            sha256=sha256_file(path),
            file_size=path.stat().st_size,
            timestamp=utc_now(),
            cache_hit=True,
        )

    @staticmethod
    def _content_type_for_path(path: Path) -> str:
        return "text/html"

    @staticmethod
    def _exception_error(exc: BaseException) -> tuple[str, str]:
        if isinstance(exc, requests.Timeout):
            return "timeout", str(exc) or "Request timed out"
        if isinstance(exc, requests.RequestException):
            return "network_error", str(exc) or exc.__class__.__name__
        return "unknown_error", str(exc) or exc.__class__.__name__

    @staticmethod
    def _response_content(response: Any) -> bytes:
        content = getattr(response, "content", None)
        if content is not None:
            return bytes(content)
        return str(getattr(response, "text", "")).encode("utf-8")

    @staticmethod
    def _content_type(response: Any) -> str:
        headers = getattr(response, "headers", {}) or {}
        return str(headers.get("Content-Type", "")).split(";", 1)[0].strip()
