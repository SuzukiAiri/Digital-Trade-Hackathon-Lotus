"""Downloaded-document ingestion and section corpus production."""

from rdtii_tool.ingestion.corpus_writer import SectionCorpusWriter
from rdtii_tool.ingestion.lifecycle import LifecycleDetector
from rdtii_tool.ingestion.manager import DocumentIngestionManager
from rdtii_tool.ingestion.parser_router import ParserRouter

__all__ = [
    "DocumentIngestionManager",
    "LifecycleDetector",
    "ParserRouter",
    "SectionCorpusWriter",
]
