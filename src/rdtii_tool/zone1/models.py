"""Shared Zone 1 source-acquisition models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol


@dataclass(slots=True)
class HostPolicy:
    max_concurrency: int = 8
    retryable_statuses: set[int] = field(default_factory=lambda: {429, 500, 502, 503, 504})
    shared_backoff: bool = True
    backoff_seconds: float = 5.0


@dataclass(slots=True)
class DocumentRef:
    economy: str
    document_id: str
    collection: str
    title: str
    canonical_url: str
    version_id: str | None
    status: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DownloadCandidate:
    url: str
    format: str
    source_type: str
    priority: int
    headers: dict[str, str] = field(default_factory=dict)
    required: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DownloadAttempt:
    url: str
    format: str
    source_type: str
    status: int | str | None
    error: str | None = None
    optional: bool = False


@dataclass(slots=True)
class DownloadResult:
    document_id: str
    success: bool
    selected_candidate: DownloadCandidate | None
    raw_path: str | None
    normalized_path: str | None
    attempts: list[dict[str, Any]]
    final_error: str | None
    status: str
    document: DocumentRef
    metadata: dict[str, Any] = field(default_factory=dict)


class PortalAdapter(Protocol):
    economy: str

    def discover(self) -> Iterable[DocumentRef]:
        ...

    def resolve_current_version(self, document: DocumentRef) -> DocumentRef:
        ...

    def get_download_candidates(self, document: DocumentRef) -> list[DownloadCandidate]:
        ...

    def validate_response(self, document: DocumentRef, candidate: DownloadCandidate, response: Any) -> bool:
        ...

    def normalize_response(self, document: DocumentRef, candidate: DownloadCandidate, content: bytes) -> str:
        ...

    def host_policy(self) -> HostPolicy:
        ...

