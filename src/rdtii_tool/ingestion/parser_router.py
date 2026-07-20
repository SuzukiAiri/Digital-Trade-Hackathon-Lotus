"""Route HTML files through the single active SSO parser."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rdtii_tool.document_models import (
    IngestionInput,
    LegalDocument,
    ParserLogEntry,
)
from rdtii_tool.ingestion.lifecycle import LifecycleDetector
from rdtii_tool.parsers.sso_parser import SSOParser


@dataclass(slots=True)
class ParseOutcome:
    input_record: IngestionInput
    document: LegalDocument | None
    parser_log: ParserLogEntry


class ParserRouter:
    """Validate an HTML input and parse it as an SSO legal document."""

    def __init__(
        self,
        *,
        sso_parser: SSOParser | None = None,
        lifecycle_detector: LifecycleDetector | None = None,
    ) -> None:
        self.sso_parser = sso_parser or SSOParser()
        self.lifecycle_detector = lifecycle_detector or LifecycleDetector()

    def route(self, record: IngestionInput) -> ParseOutcome:
        path = Path(record.input_path)
        if not path.exists() or not path.is_file():
            return self._outcome(
                record,
                parser_status="failed",
                error_type="file_not_found",
                error_message=f"Input file not found: {path}",
            )
        if (
            path.suffix.casefold() not in {".html", ".htm"}
            and "text/html" not in record.content_type.casefold()
        ):
            return self._outcome(
                record,
                parser_status="failed",
                error_type="unsupported_file_type",
                error_message="The active parser accepts only SSO HTML",
            )

        try:
            document = self.sso_parser.parse_file(
                record.input_path,
                source_url=record.source_url,
                seed_law=record.title,
                source_type=(
                    record.document_type
                    if record.document_type
                    in {"act", "subsidiary_legislation"}
                    else record.source_type
                ),
            )
        except Exception as exc:
            return self._outcome(
                record,
                parser_status="failed",
                error_type="parse_error",
                error_message=str(exc),
            )
        return self._finalize_document(record, document)

    def _finalize_document(
        self,
        record: IngestionInput,
        document: LegalDocument,
    ) -> ParseOutcome:
        raw_text = " ".join(section.text for section in document.sections)
        lifecycle = self.lifecycle_detector.detect(
            metadata={
                "version_status": document.version_status,
                "current_version_date": document.current_version_date,
                "last_updated_date": document.last_updated_date,
                "effective_date": document.effective_date,
            },
            raw_text=raw_text,
            source_metadata=record.metadata,
        )
        document.version_status = lifecycle.version_status
        document.current_version_date = lifecycle.current_version_date
        document.last_updated_date = lifecycle.last_updated_date
        document.effective_date = lifecycle.effective_date
        document.lifecycle_notes = lifecycle.lifecycle_notes
        document.source_name = document.source_name or record.source_name
        document.source_type = document.source_type or record.source_type
        document.legal_rank = document.legal_rank or record.legal_rank
        document.source_url = document.source_url or record.source_url
        document.raw_html_path = record.input_path

        status = "success" if document.sections else "partial"
        return self._outcome(
            record,
            parser_status=status,
            document=document,
            error_type="" if document.sections else "empty_document",
            error_message=(
                ""
                if document.sections
                else "SSO document parsed but no provisions were found"
            ),
        )

    @staticmethod
    def _outcome(
        record: IngestionInput,
        *,
        parser_status: str,
        document: LegalDocument | None = None,
        error_type: str = "",
        error_message: str = "",
    ) -> ParseOutcome:
        return ParseOutcome(
            input_record=record,
            document=document,
            parser_log=ParserLogEntry(
                input_path=record.input_path,
                source_url=record.source_url,
                source_name=record.source_name,
                parser_status=parser_status,
                provisions_extracted=(
                    len(document.sections) if document else 0
                ),
                error_type=error_type,
                error_message=error_message,
                timestamp=datetime.now(timezone.utc).isoformat(),
            ),
        )
