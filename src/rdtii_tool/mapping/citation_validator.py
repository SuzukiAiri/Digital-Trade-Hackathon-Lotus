"""Citation validation for Zone 2 atomic evidence."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from rdtii_tool.parsers.docling_pdf import load_docling_artifact


@dataclass(frozen=True)
class CitationValidation:
    status: str
    error: str = ""


def validate_provision_citation(
    *,
    verbatim_snippet: str,
    source_text: str,
    article: str,
    expected_article: str,
    source_url: str | None,
) -> CitationValidation:
    if not source_url:
        return CitationValidation("unverifiable", "source_url_missing")
    if not article:
        return CitationValidation("failed", "article_missing")
    if expected_article and _article_key(article) != _article_key(expected_article):
        return CitationValidation("failed", "article_mismatch")
    if not verbatim_snippet.strip():
        return CitationValidation("failed", "verbatim_snippet_missing")
    if _normalise(verbatim_snippet) in _normalise(source_text):
        return CitationValidation("verified", "")
    return CitationValidation("failed", "verbatim_snippet_not_exact_source_substring")


def validate_external_citation(*, verbatim_snippet: str, source_url: str | None) -> CitationValidation:
    if not source_url:
        return CitationValidation("unverifiable", "source_url_missing")
    if not verbatim_snippet.strip():
        return CitationValidation("unverifiable", "verbatim_snippet_missing")
    return CitationValidation("verified", "")


def validate_docling_page_citation(
    *,
    verbatim_snippet: str,
    artifact_path: Path | str,
    page_number: int | None,
    article: str,
    source_url: str | None,
    expected_text_hash: str | None = None,
) -> CitationValidation:
    if not source_url:
        return CitationValidation("unverifiable", "source_url_missing")
    if not verbatim_snippet.strip():
        return CitationValidation("failed", "verbatim_snippet_missing")
    if page_number is None or page_number < 1:
        return CitationValidation("failed", "page_number_missing_or_invalid")
    artifact_path = Path(artifact_path)
    if not artifact_path.exists() or not artifact_path.stat().st_size:
        return CitationValidation("unverifiable", "docling_artifact_missing")
    try:
        artifact = load_docling_artifact(artifact_path)
    except Exception as exc:
        return CitationValidation("unverifiable", f"docling_artifact_error:{type(exc).__name__}")
    if expected_text_hash and artifact.get("document_text_hash") != expected_text_hash:
        return CitationValidation("failed", "document_text_hash_mismatch")
    if page_number > int(artifact.get("page_count") or 0):
        return CitationValidation("failed", "page_number_out_of_range")
    page_text = _docling_page_text(artifact, page_number)
    if _normalise(verbatim_snippet) not in _normalise(page_text):
        return CitationValidation("failed", "verbatim_snippet_not_exact_docling_page_substring")
    if article and not _article_confirmed(article, verbatim_snippet, artifact, page_number):
        return CitationValidation("failed", "printed_article_not_found_on_page")
    return CitationValidation("verified", "")


def _docling_page_text(artifact: dict, page_number: int) -> str:
    for page in artifact.get("pages") or []:
        if int(page.get("page_number") or 0) == int(page_number):
            return str(page.get("text") or "")
    return ""


def normalized_snippet_hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(_normalise(value).encode("utf-8")).hexdigest()


def _normalise(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = text.replace("\u00a0", " ")
    text = re.sub(r"(?<=\w)-\s+(?=\w)", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _article_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = re.sub(r"^\s*(s\.|sec\.|section|reg\.|regulation|rule|article)\s*", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def _compact_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKC", value or "").casefold())


def _article_parent_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = re.sub(r"^\s*(s\.|sec\.|section|reg\.|regulation|rule|article)\s*", "", text)
    match = re.search(r"[a-z]*\d[\da-z-]*", text)
    return _article_key(match.group(0)) if match else _article_key(text)


def _article_subpart_keys(value: str) -> set[str]:
    return {_article_key(item) for item in re.findall(r"\(([^)]+)\)", unicodedata.normalize("NFKC", value or "")) if _article_key(item)}


def _article_confirmed(article: str, quote: str, artifact: dict, page_number: int) -> bool:
    article_key = _article_key(article)
    if not article_key:
        return True
    quote_key = _compact_text(quote)
    page_key = _compact_text(_docling_page_text(artifact, page_number))
    if _safe_article_token(article_key) and (article_key in quote_key or article_key in page_key):
        return True
    parent_key = _article_parent_key(article)
    subparts = _article_subpart_keys(article)
    previous_key = _compact_text(_docling_page_text(artifact, page_number - 1)) if page_number > 1 else ""
    parent_present = bool(parent_key and (parent_key in page_key or parent_key in previous_key))
    if not parent_present:
        return False
    if not subparts:
        return _safe_article_token(parent_key)
    return any(subpart in quote_key or subpart in page_key for subpart in subparts)


def _safe_article_token(value: str) -> bool:
    if not value:
        return False
    if value.isdigit() and len(value) < 3:
        return False
    return len(value) >= 3
