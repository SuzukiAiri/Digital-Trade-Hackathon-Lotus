"""Candidate and document persistence."""

from rdtii_tool.storage.candidate_store import CandidateURLStore, normalize_url
from rdtii_tool.storage.document_store import DocumentStore

__all__ = ["CandidateURLStore", "DocumentStore", "normalize_url"]
