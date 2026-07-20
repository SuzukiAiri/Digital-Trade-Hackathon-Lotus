"""Models used by Singapore SSO discovery, acquisition, and ingestion."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


CandidateStatus = Literal[
    "pending",
    "downloaded",
    "failed",
    "blocked",
    "cache_hit",
]
DownloadStatus = Literal["success", "cache_hit", "blocked", "failed"]
ParserStatus = Literal["success", "partial", "failed"]


@dataclass(slots=True)
class LegalSection:
    """A provision-level unit extracted from an official SSO document."""

    section_id: str = ""
    heading: str = ""
    text: str = ""
    url: str = ""
    parent_law_name: str = ""
    source_url: str = ""
    raw_context: str = ""
    part: str = ""
    division: str = ""
    schedule: str = ""
    provision_type: str = ""
    provision_number: str = ""
    editorial_annotations: list[dict[str, str]] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LegalDocument:
    """An official SSO legal document and its parsed provisions."""

    economy: str = ""
    law_name: str = ""
    law_number_or_ref: str = ""
    source_url: str = ""
    source_name: str = ""
    source_type: str = ""
    legal_rank: str = ""
    current_version_date: str = ""
    last_updated_date: str = ""
    effective_date: str = ""
    version_status: str = ""
    lifecycle_notes: str = ""
    raw_html_path: str = ""
    sections: list[LegalSection] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CandidateURL:
    """A configured official SSO URL awaiting acquisition."""

    url: str = ""
    normalized_url: str = ""
    title: str = ""
    country: str = ""
    source_name: str = ""
    source_type: str = ""
    document_type: str = ""
    discovered_by: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    download_status: CandidateStatus = "pending"
    http_status: int | None = None
    error: str = ""
    error_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.download_status not in {
            "pending",
            "downloaded",
            "failed",
            "blocked",
            "cache_hit",
        }:
            raise ValueError(
                f"Unsupported candidate status: {self.download_status}"
            )

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> "CandidateURL":
        return cls(**data)


@dataclass(slots=True)
class DownloadResult:
    """Outcome of acquiring one official SSO URL."""

    candidate_url: str = ""
    normalized_url: str = ""
    source_name: str = ""
    source_url: str = ""
    method: str = ""
    file_type: str = ""
    status: DownloadStatus = "failed"
    http_status: int | None = None
    error_type: str = ""
    error_message: str = ""
    saved_path: str = ""
    content_type: str = ""
    sha256: str = ""
    file_size: int = 0
    timestamp: str = ""
    cache_hit: bool = False
    original_url: str = ""
    direct_html_url: str = ""
    content_validation_passed: bool = False
    content_validation_signals: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in {"success", "cache_hit", "blocked", "failed"}:
            raise ValueError(f"Unsupported download status: {self.status}")

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> "DownloadResult":
        return cls(**data)


@dataclass(slots=True)
class LifecycleInfo:
    """Rule-based lifecycle metadata derived during ingestion."""

    version_status: str = "unknown"
    current_version_date: str = ""
    last_updated_date: str = ""
    effective_date: str = ""
    lifecycle_notes: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class IngestionInput:
    """One local SSO HTML file plus registry and acquisition metadata."""

    input_path: str = ""
    source_url: str = ""
    source_name: str = ""
    source_type: str = ""
    legal_rank: str = ""
    document_type: str = ""
    content_type: str = ""
    title: str = ""
    notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ParserLogEntry:
    """Machine-readable outcome for one SSO parser attempt."""

    input_path: str = ""
    source_url: str = ""
    source_name: str = ""
    parser_name: str = "sso_parser"
    parser_status: ParserStatus = "failed"
    provisions_extracted: int = 0
    error_type: str = ""
    error_message: str = ""
    timestamp: str = ""

    def __post_init__(self) -> None:
        if self.parser_status not in {"success", "partial", "failed"}:
            raise ValueError(
                f"Unsupported parser status: {self.parser_status}"
            )

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CorpusSection:
    """Provision-level corpus row for later retrieval and mapping."""

    country: str = ""
    economy: str = ""
    source_key: str = ""
    law_title: str = ""
    source_url: str = ""
    successful_url: str = ""
    source_type: str = ""
    legal_rank: str = ""
    version_status: str = "unknown"
    current_version_date: str = ""
    part: str = ""
    division: str = ""
    schedule: str = ""
    provision_type: str = ""
    provision_number: str = ""
    heading: str = ""
    text: str = ""
    word_count: int = 0
    char_count: int = 0
    raw_file_path: str = ""
    parser: str = "sso_direct_html"
    parser_status: ParserStatus = "success"
    review_flag: bool = False
    notes: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)
