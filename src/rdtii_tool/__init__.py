"""Singapore-first RDTII legal-regulatory extraction tooling."""

from rdtii_tool.document_models import (
    CandidateURL,
    CorpusSection,
    DownloadResult,
    IngestionInput,
    LegalDocument,
    LegalSection,
    LifecycleInfo,
    ParserLogEntry,
)
from rdtii_tool.schemas import EvidenceRow

__all__ = [
    "CandidateURL",
    "CorpusSection",
    "DownloadResult",
    "EvidenceRow",
    "IngestionInput",
    "LegalDocument",
    "LegalSection",
    "LifecycleInfo",
    "ParserLogEntry",
]
__version__ = "0.1.0"
