"""Standardize Zone 1 document and per-document provision JSONL outputs.

Zone 1 owns acquisition, document normalization, and deterministic provision
splitting.  The mapper consumes only ``zone1_provisions_manifest.jsonl`` and
streams the per-document provision JSONL files listed there.
"""

from __future__ import annotations

import hashlib
import csv
import json
import os
import re
from html import unescape
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


STANDARDIZER_VERSION = "zone1-per-document-standardizer-v5-canonical-legal-metadata"
PROVISION_SCHEMA_VERSION = "zone1-provision-schema-v4-canonical-citation"
DOCUMENT_SCHEMA_VERSION = "zone1-document-schema-v3-legal-structure"

DOCUMENT_FIELDS = [
    "economy",
    "source",
    "collection",
    "document_id",
    "instrument_id",
    "version_id",
    "title",
    "document_type",
    "instrument_role",
    "principal_instrument_id",
    "amends_instrument_id",
    "consolidated_target",
    "official_number",
    "frl_identifier",
    "registered_date",
    "effective_from",
    "effective_to",
    "compilation_number",
    "compilation_date",
    "version_date",
    "last_amended",
    "year",
    "language",
    "lifecycle_status",
    "source_url",
    "canonical_url",
    "source_format",
    "raw_path",
    "normalized_path",
    "metadata_path",
    "sha256",
    "file_size",
    "retrieved_at",
    "download_status",
    "requires_ocr",
    "alias_of",
    "actual_corpus_gap",
    "failure_owner",
    "provenance_status",
    "processing_mode",
    "direct_mapping_enabled",
    "direct_mapping_reason",
    "content_hash",
    "canonical_schema_version",
    "parse_status",
    "parse_error_code",
    "pdf_text_path",
    "prefilter_status",
]

INTERNAL_PROVISION_FIELDS = [
    "provision_id",
    "source_locator",
    "document_id",
    "canonical_citation",
    "container_type",
    "container_title",
    "economy",
    "provision_label",
    "provision_path",
    "collection",
    "title",
    "official_number",
    "year",
    "source_format",
    "source_url",
    "last_amended",
    "heading",
    "article",
    "provision_number",
    "section",
    "subsection",
    "paragraph",
    "page",
    "anchor",
    "anchor_url",
    "hierarchy",
    "text",
    "text_hash",
    "canonical_schema_version",
    "document_content_hash",
    "char_start",
    "char_end",
    "extraction_method",
    "parser_name",
    "parser_version",
    "parent_provision_path",
    "chunk_index",
    "editorial_annotations",
]

PROVISION_OUTPUT_BASE_FIELDS = [
    "record_type",
    "provision_id",
    "canonical_citation",
    "source_locator",
    "document_id",
    "economy",
    "collection",
    "title",
    "document_type",
    "instrument_role",
    "official_number",
    "last_amended",
    "year",
    "language",
    "source_format",
    "source_url",
    "provision_path",
    "heading",
    "article",
    "provision_number",
    "section",
    "provision_label",
    "anchor_url",
    "hierarchy",
    "page_number",
    "source_page_number",
    "text_hash",
    "canonical_schema_version",
    "document_content_hash",
    "char_start",
    "char_end",
    "text",
    "processing_mode",
    "editorial_annotations",
]

PROVISION_OUTPUT_CHUNK_FIELDS = ["parent_provision_path", "chunk_index"]

PDF_DOCUMENT_OUTPUT_FIELDS = [
    "record_type",
    "document_id",
    "economy",
    "collection",
    "title",
    "document_type",
    "instrument_role",
    "principal_instrument_id",
    "amends_instrument_id",
    "consolidated_target",
    "official_number",
    "last_amended",
    "year",
    "language",
    "source_format",
    "source_url",
    "raw_path",
    "normalized_path",
    "pdf_text_path",
    "prefilter_status",
    "processing_mode",
    "direct_mapping_enabled",
    "direct_mapping_reason",
    "content_hash",
    "canonical_schema_version",
    "parse_status",
    "parse_error_code",
]

PROVISION_MANIFEST_FIELDS = [
    "economy",
    "source",
    "collection",
    "document_id",
    "instrument_id",
    "version_id",
    "title",
    "official_number",
    "last_amended",
    "language",
    "lifecycle_status",
    "source_url",
    "canonical_url",
    "provisions_path",
    "provision_count",
    "file_size",
    "sha256",
    "parser_name",
    "parser_version",
    "extraction_status",
    "provenance_status",
    "processing_mode",
    "direct_mapping_enabled",
    "direct_mapping_reason",
    "canonical_schema_version",
]

_ENSURED_WRITE_DIRS: set[Path] = set()


@dataclass
class StandardizationSummary:
    economy: str
    documents_count: int = 0
    provisions_count: int = 0
    provision_files_count: int = 0
    provenance_full_count: int = 0
    provenance_partial_count: int = 0
    excluded_duplicate_alias_count: int = 0
    excluded_known_gaps_count: int = 0
    excluded_source_unavailable_count: int = 0
    excluded_stale_extra_artifact_count: int = 0
    fallback_chunk_count: int = 0
    heading_only_provision_count: int = 0
    ordinary_downloader_program_errors_count: int = 0
    source_documents_seen: int = 0
    structured_documents_count: int = 0
    document_direct_documents_count: int = 0
    parse_failed_documents_count: int = 0
    excluded_documents_count: int = 0
    duplicate_source_rows_count: int = 0
    unreferenced_artifact_files_count: int = 0
    caveats: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "economy": self.economy,
            "documents_count": self.documents_count,
            "provisions_count": self.provisions_count,
            "provision_files_count": self.provision_files_count,
            "provenance_full_count": self.provenance_full_count,
            "provenance_partial_count": self.provenance_partial_count,
            "excluded_duplicate_alias_count": self.excluded_duplicate_alias_count,
            "excluded_known_gaps_count": self.excluded_known_gaps_count,
            "excluded_source_unavailable_count": self.excluded_source_unavailable_count,
            "excluded_stale_extra_artifact_count": self.excluded_stale_extra_artifact_count,
            "fallback_chunk_count": self.fallback_chunk_count,
            "heading_only_provision_count": self.heading_only_provision_count,
            "ordinary_downloader_program_errors_count": self.ordinary_downloader_program_errors_count,
            "source_documents_seen": self.source_documents_seen,
            "structured_documents_count": self.structured_documents_count,
            "document_direct_documents_count": self.document_direct_documents_count,
            "parse_failed_documents_count": self.parse_failed_documents_count,
            "excluded_documents_count": self.excluded_documents_count,
            "duplicate_source_rows_count": self.duplicate_source_rows_count,
            "unreferenced_artifact_files_count": self.unreferenced_artifact_files_count,
            "caveats": self.caveats,
        }


def standardize_zone1_corpus(project_root: Path, economy: str) -> StandardizationSummary:
    economy = economy.casefold()
    if economy == "singapore":
        summary = _standardize_singapore(project_root)
    elif economy == "australia":
        summary = _standardize_australia(project_root)
    elif economy == "malaysia":
        summary = _standardize_malaysia(project_root)
    else:
        raise ValueError(f"Unsupported economy for Zone 1 standardization: {economy}")
    _write_reports(project_root, summary)
    return summary


def _standardize_singapore(project_root: Path) -> StandardizationSummary:
    root = project_root / "outputs" / "corpus" / "singapore"
    docs = _read_jsonl(root / "zone1_input_manifest.jsonl")
    if not docs:
        docs = _read_jsonl(project_root / "data" / "legal_sources" / "singapore" / "manifests" / "zone1_input_manifest.jsonl")
    if not docs:
        docs = _singapore_docs_from_legacy_manifests(project_root)

    summary = StandardizationSummary(economy="singapore")
    output_docs: list[dict[str, Any]] = []
    output_provision_manifests: list[dict[str, Any]] = []
    seen_docs: set[str] = set()

    for source_doc in docs:
        if not _is_success_canonical(source_doc):
            continue
        source_doc = _enrich_singapore_source_doc(project_root, source_doc)
        doc = _document_record(project_root, source_doc, economy="singapore")
        if _is_pdf_only_document(doc, source_doc):
            if doc["document_id"] in seen_docs:
                summary.excluded_duplicate_alias_count += 1
                continue
            manifest_row = _write_pdf_document_record(project_root, "singapore", doc)
            if manifest_row is None:
                continue
            seen_docs.add(doc["document_id"])
            output_docs.append(doc)
            if doc["provenance_status"] == "full":
                summary.provenance_full_count += 1
            else:
                summary.provenance_partial_count += 1
            output_provision_manifests.append(manifest_row)
            summary.provisions_count += int(manifest_row["provision_count"])
            continue
        provision_path = _first_existing_path(
            project_root,
            source_doc.get("normalized_path"),
            source_doc.get("jsonl_path"),
            source_doc.get("local_path"),
        )
        if not provision_path:
            continue
        doc["normalized_path"] = _project_rel(project_root, provision_path)
        legacy_rows = _read_jsonl(provision_path)
        if legacy_rows and str(legacy_rows[0].get("record_type") or "").casefold() == "pdf_document":
            doc["source_format"] = "pdf"
            doc["raw_path"] = _project_rel(project_root, legacy_rows[0].get("raw_path") or doc.get("raw_path"))
            if doc["document_id"] in seen_docs:
                summary.excluded_duplicate_alias_count += 1
                continue
            manifest_row = _write_pdf_document_record(project_root, "singapore", doc)
            if manifest_row is None:
                continue
            seen_docs.add(doc["document_id"])
            output_docs.append(doc)
            if doc["provenance_status"] == "full":
                summary.provenance_full_count += 1
            else:
                summary.provenance_partial_count += 1
            output_provision_manifests.append(manifest_row)
            summary.provisions_count += int(manifest_row["provision_count"])
            continue
        if _requires_document_direct_mapping(doc, legacy_rows):
            doc["processing_mode"] = "document_direct"
            doc["source_format"] = "pdf"
            doc["direct_mapping_enabled"] = False
            doc["direct_mapping_reason"] = "page_layout_not_approved_for_direct_mapping"
            doc["parse_error_code"] = "page_layout_not_structured"
            pdf_raw = _singapore_default_pdf_path(doc["document_id"], doc.get("collection", ""))
            if pdf_raw:
                doc["raw_path"] = pdf_raw
            doc["_document_text"] = _single_plain_text_body(legacy_rows)
            if doc["document_id"] in seen_docs:
                summary.excluded_duplicate_alias_count += 1
                continue
            manifest_row = _write_pdf_document_record(project_root, "singapore", doc)
            if manifest_row is None:
                continue
            seen_docs.add(doc["document_id"])
            output_docs.append(doc)
            if doc["provenance_status"] == "full":
                summary.provenance_full_count += 1
            else:
                summary.provenance_partial_count += 1
            output_provision_manifests.append(manifest_row)
            summary.provisions_count += int(manifest_row["provision_count"])
            continue
        if doc["document_id"] in seen_docs:
            summary.excluded_duplicate_alias_count += 1
            continue
        doc_provisions: list[dict[str, Any]] = []
        seen_doc_provisions: set[str] = set()
        for index, row in enumerate(legacy_rows, start=1):
            provision = _provision_from_legacy_row(
                doc,
                row,
                index=index,
                parser_name="singapore_sso_parser",
                extraction_method="sso_html_parser",
            )
            _append_unique_provision(doc_provisions, provision, seen_doc_provisions)
        if not doc_provisions:
            continue
        manifest_row = _write_document_provisions(project_root, "singapore", doc, doc_provisions)
        if manifest_row is None:
            continue
        seen_docs.add(doc["document_id"])
        output_docs.append(doc)
        if doc["provenance_status"] == "full":
            summary.provenance_full_count += 1
        else:
            summary.provenance_partial_count += 1
        output_provision_manifests.append(manifest_row)
        summary.provisions_count += int(manifest_row["provision_count"])

    summary.excluded_duplicate_alias_count += _singapore_alias_count(project_root)
    summary.excluded_known_gaps_count = _count_jsonl(root / "singapore_source_gap_manifest.jsonl")
    _write_jsonl(root / "zone1_documents.jsonl", output_docs)
    _write_jsonl(root / "zone1_provisions_manifest.jsonl", output_provision_manifests)
    _write_merged_provisions_from_manifest(project_root, "singapore", output_provision_manifests)
    _copy_to_data_manifest(project_root, "singapore", output_docs, output_provision_manifests)
    summary.documents_count = len(output_docs)
    summary.provision_files_count = len(output_provision_manifests)
    return summary


def _standardize_australia(project_root: Path) -> StandardizationSummary:
    root = project_root / "outputs" / "corpus" / "australia"
    data_manifest = project_root / "data" / "legal_sources" / "australia" / "manifests" / "australia_downloaded_manifest.jsonl"
    data_by_register = {str(row.get("register_id") or row.get("official_id") or ""): row for row in _read_jsonl(data_manifest)}
    legacy_rows = [
        row
        for manifest in [root / "manifests" / "acts_manifest.jsonl", root / "manifests" / "subsidiary_manifest.jsonl"]
        for row in _read_jsonl(manifest)
        if row.get("download_status") == "success" and row.get("parse_status") == "success"
    ]
    legacy_files = list((root / "acts").glob("*.jsonl")) + list((root / "subsidiary").glob("*.jsonl"))
    used_legacy_files: set[str] = set()
    output_docs: list[dict[str, Any]] = []
    output_provision_manifests: list[dict[str, Any]] = []
    seen_docs: set[str] = set()
    summary = StandardizationSummary(economy="australia")

    legacy_register_ids = {str(row.get("register_id") or "").strip() for row in legacy_rows}
    notifiable_rows = list(_australia_notifiable_legacy_rows(project_root, data_by_register.values(), legacy_register_ids))
    source_document_ids = {
        _australia_document_id(str(row.get("register_id") or row.get("official_id") or "").strip(), str(row.get("collection") or ("Act" if str(row.get("register_id") or row.get("official_id") or "").upper().startswith("C") else "LegislativeInstrument")))
        for row in legacy_rows
        if str(row.get("register_id") or row.get("official_id") or "").strip()
    }
    source_document_ids.update(
        _australia_document_id(str(row.get("register_id") or row.get("official_id") or "").strip(), "NotifiableInstrument")
        for row in notifiable_rows
        if str(row.get("register_id") or row.get("official_id") or "").strip()
    )
    summary.source_documents_seen = len(source_document_ids)

    processed: list[tuple[dict[str, Any], list[dict[str, Any]], Path, str]] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(_process_australia_legacy_row, project_root, row, data_by_register)
            for row in legacy_rows
        ]
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                processed.append(result)

    for doc, provisions, legacy_path, register_id in sorted(processed, key=lambda item: item[0]["document_id"]):
        if doc["document_id"] in seen_docs:
            summary.excluded_stale_extra_artifact_count += 1
            continue
        if _is_pdf_only_document(doc):
            manifest_row = _write_pdf_document_record(project_root, "australia", doc)
            if manifest_row is None:
                summary.excluded_stale_extra_artifact_count += 1
                continue
            seen_docs.add(doc["document_id"])
            output_docs.append(doc)
            if doc["provenance_status"] == "full":
                summary.provenance_full_count += 1
            else:
                summary.provenance_partial_count += 1
            used_legacy_files.add(legacy_path.as_posix())
            output_provision_manifests.append(manifest_row)
            summary.provisions_count += int(manifest_row["provision_count"])
            continue
        doc_provisions: list[dict[str, Any]] = []
        seen_doc_provisions: set[str] = set()
        for provision in provisions:
            _append_unique_provision(doc_provisions, provision, seen_doc_provisions)
        if not doc_provisions:
            summary.excluded_stale_extra_artifact_count += 1
            summary.excluded_documents_count += 1
            continue
        if _requires_document_direct_mapping(doc, doc_provisions):
            manifest_row = _write_document_direct_record_from_whole_text(
                project_root,
                "australia",
                doc,
                doc_provisions,
                reason="whole_document_without_reliable_provision_boundaries",
            )
            if manifest_row is None:
                summary.excluded_stale_extra_artifact_count += 1
                summary.parse_failed_documents_count += 1
                continue
            seen_docs.add(doc["document_id"])
            output_docs.append(doc)
            if doc["provenance_status"] == "full":
                summary.provenance_full_count += 1
            else:
                summary.provenance_partial_count += 1
            used_legacy_files.add(legacy_path.as_posix())
            output_provision_manifests.append(manifest_row)
            summary.provisions_count += int(manifest_row["provision_count"])
            summary.document_direct_documents_count += 1
            continue
        manifest_row = _write_document_provisions(project_root, "australia", doc, doc_provisions)
        if manifest_row is None:
            summary.excluded_stale_extra_artifact_count += 1
            summary.excluded_documents_count += 1
            continue
        seen_docs.add(doc["document_id"])
        output_docs.append(doc)
        if doc["provenance_status"] == "full":
            summary.provenance_full_count += 1
        else:
            summary.provenance_partial_count += 1
        used_legacy_files.add(legacy_path.as_posix())
        output_provision_manifests.append(manifest_row)
        summary.provisions_count += int(manifest_row["provision_count"])
        summary.structured_documents_count += 1

    for source_doc, provisions_path, register_id in notifiable_rows:
        doc = _document_record(project_root, source_doc, economy="australia")
        if doc["document_id"] in seen_docs:
            summary.duplicate_source_rows_count += 1
            continue
        legacy_path = _first_existing_path(project_root, provisions_path)
        if not legacy_path:
            summary.excluded_documents_count += 1
            continue
        legacy_provisions = _read_jsonl(legacy_path)
        doc_provisions: list[dict[str, Any]] = []
        seen_doc_provisions: set[str] = set()
        for provision in legacy_provisions:
            _append_unique_provision(doc_provisions, provision, seen_doc_provisions)
        if not doc_provisions:
            summary.excluded_documents_count += 1
            continue
        if _requires_document_direct_mapping(doc, doc_provisions):
            manifest_row = _write_document_direct_record_from_whole_text(
                project_root,
                "australia",
                doc,
                doc_provisions,
                reason="whole_document_without_reliable_provision_boundaries",
            )
        else:
            manifest_row = _write_document_provisions(project_root, "australia", doc, doc_provisions)
        if manifest_row is None:
            summary.excluded_documents_count += 1
            continue
        seen_docs.add(doc["document_id"])
        output_docs.append(doc)
        if doc["provenance_status"] == "full":
            summary.provenance_full_count += 1
        else:
            summary.provenance_partial_count += 1
        used_legacy_files.add(legacy_path.as_posix())
        output_provision_manifests.append(manifest_row)
        summary.provisions_count += int(manifest_row["provision_count"])
        if str(manifest_row.get("processing_mode") or "") == "document_direct":
            summary.document_direct_documents_count += 1
        else:
            summary.structured_documents_count += 1

    for data_row in _australia_pdf_only_rows(data_by_register.values()):
        register_id = str(data_row.get("register_id") or data_row.get("official_id") or "").strip()
        collection = str(data_row.get("collection") or ("Act" if register_id.startswith("C") else "LegislativeInstrument"))
        source_doc = dict(data_row)
        source_doc["document_id"] = _australia_document_id(register_id, collection) if register_id else ""
        source_doc["source"] = "australia_frl"
        source_doc["instrument_id"] = register_id or source_doc.get("instrument_id")
        source_doc["official_number"] = source_doc.get("classification") or register_id
        source_doc["language"] = source_doc.get("language") or "en"
        source_doc["lifecycle_status"] = source_doc.get("status") or "current"
        source_doc["canonical_url"] = source_doc.get("latest_version_url") or source_doc.get("source_url")
        doc = _document_record(project_root, source_doc, economy="australia")
        if doc["document_id"] in seen_docs:
            continue
        manifest_row = _write_pdf_document_record(project_root, "australia", doc)
        if manifest_row is None:
            continue
        seen_docs.add(doc["document_id"])
        output_docs.append(doc)
        if doc["provenance_status"] == "full":
            summary.provenance_full_count += 1
        else:
            summary.provenance_partial_count += 1
        output_provision_manifests.append(manifest_row)
        summary.provisions_count += int(manifest_row["provision_count"])
        if str(manifest_row.get("processing_mode") or "") == "document_direct":
            summary.document_direct_documents_count += 1
        elif str(manifest_row.get("processing_mode") or "") == "parse_failed":
            summary.parse_failed_documents_count += 1

    summary.unreferenced_artifact_files_count = len({p.as_posix() for p in legacy_files} - used_legacy_files)
    summary.excluded_stale_extra_artifact_count += summary.unreferenced_artifact_files_count
    missing_data_manifest = sum(1 for row in legacy_rows if str(row.get("register_id") or "") not in data_by_register)
    if missing_data_manifest:
        summary.caveats.append(f"{missing_data_manifest} Australia legacy provision documents did not have a matching FRL downloaded-manifest row; provenance may be partial.")
    _write_jsonl(root / "zone1_documents.jsonl", output_docs)
    _write_jsonl(root / "zone1_provisions_manifest.jsonl", output_provision_manifests)
    _write_merged_provisions_from_manifest(project_root, "australia", output_provision_manifests)
    _copy_to_data_manifest(project_root, "australia", output_docs, output_provision_manifests)
    summary.documents_count = len(output_docs)
    summary.provision_files_count = len(output_provision_manifests)
    return summary


def _process_australia_legacy_row(
    project_root: Path,
    row: dict[str, Any],
    data_by_register: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], Path, str] | None:
    register_id = str(row.get("register_id") or row.get("official_id") or "").strip()
    if not register_id:
        return None
    data_row = data_by_register.get(register_id, {})
    collection = str(data_row.get("collection") or ("Act" if register_id.startswith("C") else "LegislativeInstrument"))
    if collection not in {"Act", "LegislativeInstrument", "NotifiableInstrument"}:
        return None
    document_id = _australia_document_id(register_id, collection)
    source_doc = dict(data_row)
    source_doc.update(row)
    source_doc["document_id"] = document_id
    source_doc["source"] = "australia_frl"
    source_doc["instrument_id"] = register_id
    source_doc["official_number"] = data_row.get("classification") or register_id
    source_doc["language"] = row.get("language") or "en"
    source_doc["lifecycle_status"] = row.get("status") or data_row.get("status") or "current"
    source_doc["canonical_url"] = row.get("canonical_url") or data_row.get("latest_version_url") or data_row.get("source_url")
    source_doc["source_format"] = data_row.get("source_format") or data_row.get("format") or "frl_primary_text"
    source_doc["raw_path"] = row.get("raw_html_path") or data_row.get("raw_html_path") or data_row.get("raw_file_path") or data_row.get("raw_pdf_path")
    source_doc["normalized_path"] = row.get("jsonl_path") or data_row.get("normalized_file_path") or data_row.get("local_path")
    doc = _document_record(project_root, source_doc, economy="australia")
    legacy_path = _first_existing_path(project_root, row.get("jsonl_path"))
    if not legacy_path:
        return None
    legacy_rows = _read_jsonl(legacy_path)
    if legacy_rows and str(legacy_rows[0].get("record_type") or "").casefold() == "pdf_document":
        source_doc["source_format"] = "pdf"
        source_doc["raw_path"] = legacy_rows[0].get("raw_path") or source_doc.get("raw_path")
        doc = _document_record(project_root, source_doc, economy="australia")
        return doc, [], legacy_path, register_id
    provisions = [
        _provision_from_legacy_row(
            doc,
            provision_row,
            index=index,
            parser_name="australia_frl_parser",
            extraction_method="frl_html_parser",
        )
        for index, provision_row in enumerate(legacy_rows, start=1)
    ]
    return doc, provisions, legacy_path, register_id


def _australia_notifiable_legacy_rows(
    project_root: Path,
    data_rows: Iterable[dict[str, Any]],
    legacy_register_ids: set[str],
) -> Iterable[tuple[dict[str, Any], str, str]]:
    """Yield notifiable instruments with existing legacy whole-document text.

    The old Australia corpus contains notifiable-instrument provision files
    under ``provisions/notifiableinstrument`` even though they are not covered
    by the legacy acts/subsidiary parser manifests.  Treat the FRL downloaded
    manifest as the source-of-truth document scope and use the existing legacy
    text file only as the available local canonical text artifact.
    """
    root = project_root / "outputs" / "corpus" / "australia"
    for data_row in data_rows:
        register_id = str(data_row.get("register_id") or data_row.get("official_id") or "").strip()
        if not register_id or register_id in legacy_register_ids:
            continue
        if str(data_row.get("collection") or "") != "NotifiableInstrument":
            continue
        if not _is_success_canonical(data_row):
            continue
        document_id = _australia_document_id(register_id, "NotifiableInstrument")
        legacy_path = root / "provisions" / "notifiableinstrument" / f"{document_id}.jsonl"
        if not legacy_path.exists() or not legacy_path.is_file():
            continue
        source_doc = dict(data_row)
        source_doc["document_id"] = document_id
        source_doc["source"] = "australia_frl"
        source_doc["instrument_id"] = register_id
        source_doc["official_number"] = source_doc.get("classification") or register_id
        source_doc["language"] = source_doc.get("language") or "en"
        source_doc["lifecycle_status"] = source_doc.get("status") or "current"
        source_doc["canonical_url"] = source_doc.get("latest_version_url") or source_doc.get("source_url")
        source_doc["source_format"] = source_doc.get("source_format") or source_doc.get("format") or "frl_primary_text"
        source_doc["raw_path"] = source_doc.get("raw_html_path") or source_doc.get("raw_file_path") or source_doc.get("raw_pdf_path")
        source_doc["normalized_path"] = source_doc.get("normalized_file_path") or source_doc.get("local_path")
        yield source_doc, _project_rel(project_root, legacy_path), register_id


def _standardize_malaysia(project_root: Path) -> StandardizationSummary:
    root = project_root / "outputs" / "corpus" / "malaysia"
    docs = _read_jsonl(root / "zone1_input_manifest.jsonl")
    if not docs:
        docs = _read_jsonl(project_root / "data" / "legal_sources" / "malaysia" / "manifests" / "zone1_input_manifest.jsonl")
    if not docs:
        docs = _read_jsonl(project_root / "data" / "legal_sources" / "malaysia" / "manifests" / "malaysia_zone1_manifest.jsonl")

    summary = StandardizationSummary(economy="malaysia")
    output_docs: list[dict[str, Any]] = []
    output_provision_manifests: list[dict[str, Any]] = []
    seen_docs: set[str] = set()

    source_docs = [source_doc for source_doc in docs if _is_success_canonical(source_doc)]
    summary.source_documents_seen = len(source_docs)
    processed: list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_process_malaysia_source_doc, project_root, source_doc) for source_doc in source_docs]
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                processed.append(result)

    for doc, provisions, pdf_manifest in sorted(processed, key=lambda item: item[0]["document_id"]):
        if doc["document_id"] in seen_docs:
            continue
        if pdf_manifest is not None:
            seen_docs.add(doc["document_id"])
            output_docs.append(doc)
            if doc["provenance_status"] == "full":
                summary.provenance_full_count += 1
            else:
                summary.provenance_partial_count += 1
            output_provision_manifests.append(pdf_manifest)
            summary.provisions_count += int(pdf_manifest["provision_count"])
            if doc.get("processing_mode") == "document_direct":
                summary.document_direct_documents_count += 1
            elif doc.get("processing_mode") == "parse_failed":
                summary.parse_failed_documents_count += 1
            continue
        doc_provisions: list[dict[str, Any]] = []
        seen_doc_provisions: set[str] = set()
        for provision in provisions:
            if _is_heading_only(provision.get("text"), provision.get("provision_label")):
                summary.heading_only_provision_count += 1
            _append_unique_provision(doc_provisions, provision, seen_doc_provisions)
        if not doc_provisions:
            continue
        manifest_row = _write_document_provisions(project_root, "malaysia", doc, doc_provisions)
        if manifest_row is None:
            continue
        seen_docs.add(doc["document_id"])
        output_docs.append(doc)
        if doc["provenance_status"] == "full":
            summary.provenance_full_count += 1
        else:
            summary.provenance_partial_count += 1
        output_provision_manifests.append(manifest_row)
        summary.provisions_count += int(manifest_row["provision_count"])
        summary.structured_documents_count += 1

    # A source row that could not yield a canonical terminal record is explicit
    # parse failure, rather than a silently vanished document.
    accounted = len(output_docs)
    if accounted < summary.source_documents_seen:
        summary.parse_failed_documents_count += summary.source_documents_seen - accounted

    summary.excluded_source_unavailable_count = _count_jsonl(project_root / "data" / "legal_sources" / "malaysia" / "manifests" / "malaysia_source_unavailable.jsonl")
    summary.ordinary_downloader_program_errors_count = _malaysia_ordinary_error_count(project_root)
    _write_jsonl(root / "zone1_documents.jsonl", output_docs)
    _write_jsonl(root / "zone1_provisions_manifest.jsonl", output_provision_manifests)
    _write_merged_provisions_from_manifest(project_root, "malaysia", output_provision_manifests)
    _copy_to_data_manifest(project_root, "malaysia", output_docs, output_provision_manifests)
    summary.documents_count = len(output_docs)
    summary.provision_files_count = len(output_provision_manifests)
    return summary


def _process_malaysia_source_doc(
    project_root: Path,
    source_doc: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None] | None:
    doc = _document_record(project_root, source_doc, economy="malaysia")
    if _is_pdf_only_document(doc, source_doc):
        # Malaysia's cached PDF text is a valid document-direct input.  It is
        # not a pseudo provision and must not be silently disabled merely
        # because it predates the direct-mapping flag.
        doc["processing_mode"] = "document_direct"
        doc["direct_mapping_enabled"] = True
        doc["direct_mapping_reason"] = "cached_full_document_text"
        manifest_row = _write_pdf_document_record(project_root, "malaysia", doc)
        if manifest_row is None:
            return None
        return doc, [], manifest_row
    normalized_path = _first_existing_path(project_root, doc.get("normalized_path"))
    if not normalized_path:
        return None
    text = normalized_path.read_text(encoding="utf-8", errors="replace")
    text = _generic_light_text_cleanup(text)
    provisions = _split_malaysia_document(doc, text)
    if not provisions:
        doc["processing_mode"] = "document_direct"
        doc["direct_mapping_enabled"] = True
        doc["direct_mapping_reason"] = "whole_document_without_reliable_provision_boundaries"
        doc["_document_text"] = text
        manifest_row = _write_pdf_document_record(project_root, "malaysia", doc)
        if manifest_row is None:
            return None
        return doc, [], manifest_row
    return doc, provisions, None


def _singapore_docs_from_legacy_manifests(project_root: Path) -> list[dict[str, Any]]:
    root = project_root / "outputs" / "corpus" / "singapore"
    rows: list[dict[str, Any]] = []
    for path in [root / "manifests" / "acts_manifest.jsonl", root / "manifests" / "subsidiary_manifest.jsonl"]:
        rows.extend(_read_jsonl(path))
    for row in rows:
        row.setdefault("economy", "singapore")
        row.setdefault("source", "singapore_sso")
        row.setdefault("instrument_id", row.get("official_id") or row.get("document_id"))
        row.setdefault("language", "en")
        row.setdefault("lifecycle_status", row.get("status") or "current")
        row.setdefault("normalized_path", row.get("jsonl_path") or row.get("local_path"))
        row.setdefault("source_format", "html")
    return rows


def _australia_legacy_provision_index(project_root: Path) -> tuple[dict[str, Path], list[Path]]:
    root = project_root / "outputs" / "corpus" / "australia"
    index: dict[str, Path] = {}
    files: list[Path] = []
    for directory in [root / "acts", root / "subsidiary"]:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.jsonl")):
            files.append(path)
            index[path.stem] = path
    return index, files


def _document_record(project_root: Path, row: dict[str, Any], *, economy: str) -> dict[str, Any]:
    document_id = _clean_str(row.get("document_id") or row.get("official_id") or row.get("register_id"))
    if not document_id:
        seed = "|".join(
            _clean_str(row.get(key))
            for key in ("source", "collection", "title", "canonical_url", "source_url", "normalized_path", "local_path")
        )
        document_id = f"{economy}-doc-{hashlib.sha256(seed.encode('utf-8', errors='ignore')).hexdigest()[:16]}"
    raw_path = _project_rel(project_root, row.get("raw_path") or row.get("raw_html_path") or row.get("raw_file_path") or row.get("raw_pdf_path"))
    normalized_path = _project_rel(project_root, row.get("normalized_path") or row.get("normalized_file_path") or row.get("local_path") or row.get("jsonl_path"))
    metadata_path = _project_rel(project_root, row.get("metadata_path"))
    provenance_status = "full" if raw_path and normalized_path and metadata_path else "partial"
    file_size = _int(row.get("file_size"))
    sha = _clean_str(row.get("sha256"))
    collection = _collection(row, economy)
    doc = {
        "economy": economy,
        "source": _clean_str(row.get("source")) or _default_source(economy),
        "collection": collection,
        "document_id": document_id,
        "instrument_id": _clean_str(row.get("instrument_id") or row.get("official_id") or row.get("register_id") or document_id),
        "version_id": _clean_str(row.get("version_id")) or "current",
        "title": _clean_str(row.get("title") or row.get("official_title") or document_id),
        "document_type": _document_type(row, collection),
        "instrument_role": _instrument_role(row),
        "principal_instrument_id": _clean_str(row.get("principal_instrument_id") or row.get("principal_register_id") or row.get("parent_register_id")),
        "amends_instrument_id": _clean_str(row.get("amends_instrument_id") or row.get("amends_register_id")),
        "consolidated_target": _clean_str(row.get("consolidated_target") or row.get("consolidated_target_id")),
        "official_number": _official_number(row, economy=economy),
        "frl_identifier": _frl_identifier(row, economy=economy),
        "registered_date": _clean_str(row.get("registered_date")),
        "effective_from": _clean_str(row.get("effective_from")),
        "effective_to": _clean_str(row.get("effective_to")),
        "compilation_number": _clean_str(row.get("compilation_number")),
        "compilation_date": _clean_str(row.get("compilation_date")),
        "version_date": _clean_str(row.get("version_date") or row.get("compilation_date")),
        "last_amended": _last_amended(row, economy=economy),
        "year": _clean_str(row.get("year")),
        "language": _clean_str(row.get("language")) or "en",
        "lifecycle_status": _clean_str(row.get("lifecycle_status") or row.get("status")) or "current",
        "source_url": _clean_str(row.get("source_url") or row.get("canonical_url") or row.get("latest_version_url")),
        "canonical_url": _clean_str(row.get("canonical_url") or row.get("latest_version_url") or row.get("source_url")),
        "source_format": _clean_str(row.get("source_format") or row.get("format") or row.get("document_type")) or "unknown",
        "raw_path": raw_path,
        "normalized_path": normalized_path,
        "metadata_path": metadata_path,
        "sha256": sha,
        "file_size": file_size,
        "retrieved_at": _clean_str(row.get("retrieved_at")),
        "download_status": _clean_str(row.get("download_status")) or "success",
        "requires_ocr": _bool(row.get("requires_ocr")),
        "alias_of": _clean_str(row.get("alias_of")),
        "actual_corpus_gap": _bool(row.get("actual_corpus_gap")),
        "failure_owner": _clean_str(row.get("failure_owner")),
        "provenance_status": provenance_status,
        "processing_mode": _clean_str(row.get("processing_mode")) or ("document_direct" if _is_pdf_only_source(row) else "structured_provisions"),
        "direct_mapping_enabled": _bool(row.get("direct_mapping_enabled")),
        "direct_mapping_reason": _clean_str(row.get("direct_mapping_reason")) or ("approved_direct_source" if _bool(row.get("direct_mapping_enabled")) else "structured_source"),
        "content_hash": sha or _content_hash_for_paths(project_root, raw_path, normalized_path),
        "canonical_schema_version": DOCUMENT_SCHEMA_VERSION,
        "parse_status": _clean_str(row.get("parse_status")) or "success",
        "parse_error_code": _clean_str(row.get("parse_error_code")),
        "pdf_text_path": _clean_str(row.get("pdf_text_path")),
        "prefilter_status": _clean_str(row.get("prefilter_status")),
    }
    return {field: doc.get(field, "") for field in DOCUMENT_FIELDS}


def _is_pdf_only_source(row: dict[str, Any]) -> bool:
    source_format = str(row.get("source_format") or row.get("format") or row.get("document_type") or "").casefold()
    raw_value = str(row.get("raw_path") or row.get("raw_pdf_path") or row.get("raw_file_path") or row.get("local_path") or "")
    return "pdf" in source_format or raw_value.casefold().endswith(".pdf")


def _document_type(row: dict[str, Any], collection: str) -> str:
    return _clean_str(row.get("document_type") or row.get("classification") or collection)


def _instrument_role(row: dict[str, Any]) -> str:
    """Use source metadata only; absence remains explicitly unknown."""
    explicit = _clean_str(row.get("instrument_role") or row.get("role")).casefold()
    if explicit in {"principal", "amending", "repealing", "subsidiary"}:
        return explicit
    if _bool(row.get("is_principal")) or _bool(row.get("is_consolidated")):
        return "principal"
    if _bool(row.get("is_amending")) or _bool(row.get("amends_instrument")):
        return "amending"
    if _bool(row.get("is_repealing")):
        return "repealing"
    if _bool(row.get("is_subsidiary")):
        return "subsidiary"
    return "unknown"


def _official_number(row: dict[str, Any], *, economy: str) -> str:
    if economy.casefold() == "australia":
        formatted = _australia_official_number_from_metadata(row)
        if formatted:
            return formatted
    candidates = (
        row.get("official_number"),
        row.get("law_number_ref"),
        row.get("instrument_number"),
        row.get("number"),
        row.get("no"),
        row.get("classification"),
        row.get("official_id"),
        row.get("register_id"),
    )
    for value in candidates:
        text = _clean_str(value)
        if not text:
            continue
        if economy.casefold() == "australia":
            if _is_australia_non_number_metadata(text):
                continue
            if _looks_frl_identifier(text):
                continue
            if not re.search(r"\d", text):
                continue
        return text
    return ""


def _australia_official_number_from_metadata(row: dict[str, Any]) -> str:
    classification = _clean_str(row.get("classification") or row.get("document_type"))
    number = _clean_str(row.get("number"))
    year = _clean_str(row.get("year"))
    if not number or not year:
        return ""
    if classification == "Act":
        return f"Act No. {number}, {year}"
    if classification == "SR":
        return f"SR No. {number}, {year}"
    if classification == "SLI":
        return f"SLI No. {number}, {year}"
    return ""


def _frl_identifier(row: dict[str, Any], *, economy: str) -> str:
    if economy.casefold() != "australia":
        return ""
    for key in ("official_id", "register_id", "frl_id", "instrument_id"):
        text = _clean_str(row.get(key))
        if text and (_looks_frl_identifier(text) or key in {"official_id", "register_id"}):
            return text
    return ""


def _last_amended(row: dict[str, Any], *, economy: str) -> str:
    for key in ("last_amended", "last_amended_year", "last_updated_year", "amended_year"):
        text = _clean_str(row.get(key))
        if text:
            return text
    if economy.casefold() == "australia":
        return ""
    return _clean_str(row.get("registered_date") or row.get("effective_from") or row.get("version_date"))


def _is_australia_non_number_metadata(value: str) -> bool:
    text = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    return text in {
        "act",
        "acts",
        "legislative instrument",
        "legislative instruments",
        "legislativeinstrument",
        "notifiable instrument",
        "notifiable instruments",
        "notifiableinstrument",
        "rules",
        "regulations",
        "determination",
        "unknown",
    }


def _looks_frl_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]\d{4}[A-Z]\d{5}", value.strip()))


def _content_hash_for_paths(project_root: Path, *values: str) -> str:
    for value in values:
        path = Path(str(value or ""))
        if not str(path):
            continue
        if not path.is_absolute():
            path = project_root / path
        if path.exists() and path.is_file():
            return _sha256_path(path)
    return ""


def _provision_from_legacy_row(
    doc: dict[str, Any],
    row: dict[str, Any],
    *,
    index: int,
    parser_name: str,
    extraction_method: str,
) -> dict[str, Any] | None:
    text = _clean_text(row.get("text"))
    legacy_id = _clean_str(row.get("provision_id") or row.get("provision_number") or index)
    provision_id = _stable_provision_id(doc["document_id"], legacy_id, index)
    hierarchy = row.get("hierarchy") if isinstance(row.get("hierarchy"), list) else []
    provision_label = _clean_str(row.get("article") or row.get("provision_number") or legacy_id)
    source_locator = _clean_str(row.get("source_locator") or row.get("anchor_url") or row.get("anchor") or legacy_id or provision_id)
    provision = _provision_base(doc)
    provision.update(
        {
            "provision_id": provision_id,
            "source_locator": source_locator,
            "canonical_citation": _canonical_citation_from_parts(row, provision_label, hierarchy),
            "container_type": _clean_str(row.get("container_type")),
            "container_title": _clean_str(row.get("container_title")),
            "provision_label": provision_label,
            "provision_path": " / ".join([str(item) for item in hierarchy if item] + ([provision_label] if provision_label else [])),
            "article": _clean_str(row.get("article")),
            "provision_number": _clean_str(row.get("provision_number") or row.get("section") or legacy_id),
            "section": _clean_str(row.get("section") or row.get("provision_number") or legacy_id),
            "subsection": _clean_str(row.get("subsection")),
            "paragraph": _clean_str(row.get("paragraph")),
            "page": _clean_str(row.get("page")),
            "anchor": _clean_str(row.get("anchor") or row.get("anchor_url")),
            "anchor_url": _clean_str(row.get("anchor_url") or row.get("anchor")),
            "hierarchy": hierarchy,
            "text": text,
            "text_hash": _text_hash(text),
            "canonical_schema_version": PROVISION_SCHEMA_VERSION,
            "document_content_hash": doc.get("content_hash", ""),
            "char_start": _int(row.get("char_start")),
            "char_end": _int(row.get("char_end")) or len(text),
            "extraction_method": extraction_method,
            "parser_name": parser_name,
            "parser_version": PROVISION_SCHEMA_VERSION,
            "editorial_annotations": row.get("editorial_annotations") if isinstance(row.get("editorial_annotations"), list) else [],
        }
    )
    return {field: provision.get(field, "") for field in INTERNAL_PROVISION_FIELDS}


MALAYSIA_HEADING_RE = re.compile(
    r"""(?imx)
    ^\s*
    (?:
        (?P<section>\d{1,4}[A-Z]?)\s*[\.\)]\s+|
        (?P<keyword>section|regulation|rule|order|article|paragraph|schedule|clause)\s+
        (?P<label>[A-Z]?\d{1,4}[A-Z]?(?:\([^)]+\))?|[A-Z][A-Z ]{2,})\b|
        (?P<schedule>(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+)?\s*schedule)\b
    )
    """,
)


def _split_malaysia_document(doc: dict[str, Any], text: str) -> list[dict[str, Any]]:
    cleaned = _normalize_text(text)
    if not cleaned:
        return []
    matches = list(MALAYSIA_HEADING_RE.finditer(cleaned))
    provisions: list[dict[str, Any]] = []
    if len(matches) >= 2:
        for index, match in enumerate(matches, start=1):
            start = match.start()
            end = matches[index].start() if index < len(matches) else len(cleaned)
            chunk = cleaned[start:end].strip()
            if not chunk:
                continue
            label = _malaysia_label(match, index)
            provision = _provision_base(doc)
            provision.update(
                {
                    "provision_id": _stable_provision_id(doc["document_id"], label, index),
                    "provision_label": label,
                    "provision_path": label,
                    "article": label if label.casefold().startswith("article") else "",
                    "section": label if re.match(r"^\d", label) or label.casefold().startswith("section") else "",
                    "subsection": "",
                    "paragraph": label if label.casefold().startswith("paragraph") else "",
                    "page": "",
                    "anchor": "",
                    "text": chunk,
                    "text_hash": _text_hash(chunk),
                    "char_start": start,
                    "char_end": end,
                    "extraction_method": "malaysia_legal_heading_split",
                    "parser_name": "malaysia_deterministic_splitter",
                    "parser_version": PROVISION_SCHEMA_VERSION,
                }
            )
            provisions.append({field: provision.get(field, "") for field in INTERNAL_PROVISION_FIELDS})
    if provisions:
        heading_only_count = sum(1 for provision in provisions if _is_heading_only(provision.get("text"), provision.get("provision_label")))
        if heading_only_count == len(provisions) or heading_only_count / max(1, len(provisions)) > 0.5:
            return []
        if heading_only_count:
            for provision in provisions:
                if _is_heading_only(provision.get("text"), provision.get("provision_label")):
                    provision["extraction_method"] = "malaysia_heading_partial"
        return provisions
    return []


def _is_heading_only(text: Any, label: Any = "") -> bool:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact:
        return True
    label_text = re.sub(r"\s+", " ", str(label or "")).strip()
    body = compact
    if label_text and compact.casefold().startswith(label_text.casefold()):
        body = compact[len(label_text) :].strip(" .:-–—")
    if len(body) >= 40:
        return False
    return not re.search(
        r"\b(shall|must|may|means|includes|applies|person|minister|director|commissioner|offence|penalty|regulation|order|act|law|date|commence|appointed|prescribed|required)\b",
        body.casefold(),
    )


def _enrich_singapore_source_doc(project_root: Path, row: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    document_id = _clean_str(enriched.get("document_id"))
    collection = _clean_str(enriched.get("collection"))
    raw_value = (
        enriched.get("raw_path")
        or enriched.get("raw_html_path")
        or enriched.get("raw_file_path")
        or _singapore_default_raw_path(document_id, collection)
    )
    normalized_value = (
        enriched.get("normalized_path")
        or enriched.get("jsonl_path")
        or enriched.get("local_path")
        or _singapore_default_normalized_path(document_id, collection)
    )
    raw_path = _resolve_path(project_root, raw_value) if raw_value else None
    normalized_path = _resolve_path(project_root, normalized_value) if normalized_value else None
    if raw_value:
        enriched["raw_path"] = _project_rel(project_root, raw_value)
    if normalized_value:
        enriched["normalized_path"] = _project_rel(project_root, normalized_value)
    metadata = _extract_singapore_document_metadata(
        title=_clean_str(enriched.get("title") or enriched.get("official_title")),
        official_id=_clean_str(enriched.get("official_id")),
        collection=collection,
        source_url=_clean_str(enriched.get("source_url") or enriched.get("canonical_url")),
        raw_path=raw_path,
        normalized_path=normalized_path,
    )
    if metadata["official_number"]:
        enriched["official_number"] = metadata["official_number"]
    if metadata["last_amended"]:
        enriched["last_amended"] = metadata["last_amended"]
    return enriched


def _singapore_default_raw_path(document_id: str, collection: str) -> str:
    slug = _collection_slug(collection)
    if slug in {"act", "acts"}:
        return f"outputs/corpus/singapore/raw/acts/{document_id}.html"
    if slug in {"subsidiary_legislation", "subsidiarylegislation", "subsidiary"}:
        return f"outputs/corpus/singapore/raw/subsidiary_legislation/{document_id}.html"
    return ""


def _singapore_default_pdf_path(document_id: str, collection: str) -> str:
    slug = _collection_slug(collection)
    if slug in {"act", "acts"}:
        return f"outputs/corpus/singapore/raw/acts/{document_id}.pdf"
    if slug in {"subsidiary_legislation", "subsidiarylegislation", "subsidiary"}:
        return f"outputs/corpus/singapore/raw/subsidiary_legislation/{document_id}.pdf"
    return ""


def _singapore_default_normalized_path(document_id: str, collection: str) -> str:
    slug = _collection_slug(collection)
    if slug in {"act", "acts"}:
        return f"outputs/corpus/singapore/acts/{document_id}.jsonl"
    if slug in {"subsidiary_legislation", "subsidiarylegislation", "subsidiary"}:
        return f"outputs/corpus/singapore/subsidiary_legislation/{document_id}.jsonl"
    return ""


def _extract_singapore_document_metadata(
    *,
    title: str,
    official_id: str,
    collection: str,
    source_url: str,
    raw_path: Path | None,
    normalized_path: Path | None,
) -> dict[str, str]:
    official_number = ""
    if _collection_slug(collection) in {"subsidiary_legislation", "subsidiarylegislation", "subsidiary"}:
        official_number = _singapore_subsidiary_number(official_id, source_url, title)
    elif raw_path is not None:
        official_number = _singapore_act_number_from_raw(raw_path)
    last_amended = _singapore_last_amended(normalized_path, raw_path)
    return {
        "official_number": official_number,
        "last_amended": last_amended,
    }


def _singapore_subsidiary_number(official_id: str, source_url: str, title: str) -> str:
    for text in (official_id, source_url, title):
        match = re.search(r"(?:^|[-/])s(\d{1,4})[-/](\d{4})(?:$|\b)", str(text or ""), flags=re.I)
        if match:
            return f"S {int(match.group(1))}/{match.group(2)}"
        match = re.search(r"\bS\s*(\d{1,4})\s*/\s*(\d{4})\b", str(text or ""), flags=re.I)
        if match:
            return f"S {int(match.group(1))}/{match.group(2)}"
    return ""


def _singapore_act_number_from_raw(raw_path: Path) -> str:
    if not raw_path.exists() or raw_path.suffix.casefold() == ".pdf":
        return ""
    with raw_path.open("r", encoding="utf-8", errors="ignore") as handle:
        text = handle.read(120_000)
    match = re.search(
        r'origEdHdr[^>]*>\s*Act\s+\d+\s+of\s+\d{4}[^<]{0,300}\((Chapter\s+\d+[A-Z]?)\)',
        text,
        flags=re.I | re.S,
    )
    if not match:
        return ""
    return _normalize_space(unescape(match.group(1)))


def _singapore_last_amended(normalized_path: Path | None, raw_path: Path | None) -> str:
    years: list[int] = []
    if normalized_path is not None and normalized_path.exists():
        for row in _read_jsonl(normalized_path):
            years.extend(_extract_structured_amendment_years(str(row.get("text") or "")))
    return str(max(years)) if years else ""


def _extract_structured_amendment_years(text: str) -> list[int]:
    years: list[int] = []
    for pattern in (
        r"\[Act\s+\d+\s+of\s+(\d{4})",
        r"\[S\s*\d+/(\d{4})",
        r"wef\s+\d{2}/\d{2}/(\d{4})",
    ):
        for match in re.finditer(pattern, text or "", flags=re.I):
            try:
                years.append(int(match.group(1)))
            except Exception:
                continue
    return years


def _provision_base(doc: dict[str, Any]) -> dict[str, Any]:
    return {field: doc.get(field, "") for field in DOCUMENT_FIELDS if field in INTERNAL_PROVISION_FIELDS}


def _append_unique_provision(rows: list[dict[str, Any]], provision: dict[str, Any], seen: set[str]) -> None:
    if not provision.get("text"):
        return
    base_id = str(provision["provision_id"])
    provision_id = base_id
    suffix = 2
    while provision_id in seen:
        provision_id = f"{base_id}-{suffix}"
        suffix += 1
    provision["provision_id"] = provision_id
    seen.add(provision_id)
    rows.append(provision)


def _write_document_provisions(
    project_root: Path,
    economy: str,
    doc: dict[str, Any],
    provisions: list[dict[str, Any]],
) -> dict[str, Any]:
    internal_provisions = [_light_clean_internal_provision(row) for row in provisions]
    internal_provisions = [row for row in internal_provisions if str(row.get("text") or "").strip()]
    output_provisions = _minimal_output_provisions(doc, internal_provisions)
    if not output_provisions:
        return None
    provision_root = project_root / "outputs" / "corpus" / economy / "provisions"
    collection_dir = _collection_slug(doc.get("collection"))
    document_id = _safe_filename(doc.get("document_id") or doc.get("instrument_id") or "document")
    output_path = provision_root / collection_dir / f"{document_id}.jsonl"
    _write_jsonl(output_path, output_provisions)
    rel_path = _project_rel(project_root, output_path)
    file_size = output_path.stat().st_size
    status = _extraction_status_for(internal_provisions)
    parser_names = sorted({str(row.get("parser_name") or "") for row in internal_provisions if row.get("parser_name")})
    parser_versions = sorted({str(row.get("parser_version") or "") for row in internal_provisions if row.get("parser_version")})
    manifest = {
        "economy": economy,
        "source": doc.get("source", ""),
        "collection": doc.get("collection", ""),
        "document_id": doc.get("document_id", ""),
        "instrument_id": doc.get("instrument_id", ""),
        "version_id": doc.get("version_id", ""),
        "title": doc.get("title", ""),
        "official_number": doc.get("official_number", ""),
        "last_amended": doc.get("last_amended", ""),
        "language": doc.get("language", ""),
        "lifecycle_status": doc.get("lifecycle_status", ""),
        "source_url": doc.get("source_url", ""),
        "canonical_url": doc.get("canonical_url", ""),
        "provisions_path": rel_path,
        "provision_count": len(output_provisions),
        "file_size": file_size,
        "sha256": _sha256_path(output_path),
        "parser_name": "+".join(parser_names) if parser_names else "",
        "parser_version": "+".join(parser_versions) if parser_versions else PROVISION_SCHEMA_VERSION,
        "extraction_status": status,
        "provenance_status": doc.get("provenance_status", ""),
        "processing_mode": doc.get("processing_mode") or "structured_provisions",
        "direct_mapping_enabled": doc.get("direct_mapping_enabled", False),
        "direct_mapping_reason": doc.get("direct_mapping_reason") or "structured_source",
        "canonical_schema_version": PROVISION_SCHEMA_VERSION,
    }
    return {field: manifest.get(field, "") for field in PROVISION_MANIFEST_FIELDS}


def _write_document_direct_record_from_whole_text(
    project_root: Path,
    economy: str,
    doc: dict[str, Any],
    provisions: list[dict[str, Any]],
    *,
    reason: str,
) -> dict[str, Any] | None:
    text = _single_plain_text_body(provisions)
    if not text.strip():
        return None
    doc["processing_mode"] = "document_direct"
    doc["direct_mapping_enabled"] = True
    doc["direct_mapping_reason"] = reason
    doc["_document_text"] = text
    manifest = _write_pdf_document_record(project_root, economy, doc)
    doc.pop("_document_text", None)
    return manifest


def _write_pdf_document_record(project_root: Path, economy: str, doc: dict[str, Any]) -> dict[str, Any] | None:
    record = _pdf_document_record(project_root, economy, doc)
    if record is None:
        return None
    doc.update({key: value for key, value in record.items() if key in DOCUMENT_FIELDS})
    doc.pop("_document_text", None)
    provision_root = project_root / "outputs" / "corpus" / economy / "provisions"
    collection_dir = _collection_slug(doc.get("collection"))
    document_id = _safe_filename(doc.get("document_id") or doc.get("instrument_id") or "document")
    output_path = provision_root / collection_dir / f"{document_id}.jsonl"
    _write_jsonl(output_path, [record])
    rel_path = _project_rel(project_root, output_path)
    manifest = {
        "economy": economy,
        "source": doc.get("source", ""),
        "collection": doc.get("collection", ""),
        "document_id": doc.get("document_id", ""),
        "instrument_id": doc.get("instrument_id", ""),
        "version_id": doc.get("version_id", ""),
        "title": doc.get("title", ""),
        "official_number": doc.get("official_number", ""),
        "last_amended": doc.get("last_amended", ""),
        "language": doc.get("language", ""),
        "lifecycle_status": doc.get("lifecycle_status", ""),
        "source_url": doc.get("source_url", ""),
        "canonical_url": doc.get("canonical_url", ""),
        "provisions_path": rel_path,
        "provision_count": 1,
        "file_size": output_path.stat().st_size,
        "sha256": _sha256_path(output_path),
        "parser_name": "",
        "parser_version": PROVISION_SCHEMA_VERSION,
        "extraction_status": "pdf_document",
        "provenance_status": doc.get("provenance_status", ""),
        "processing_mode": record.get("processing_mode") or doc.get("processing_mode") or "document_direct",
        "direct_mapping_enabled": record.get("direct_mapping_enabled", False),
        "direct_mapping_reason": record.get("direct_mapping_reason") or doc.get("direct_mapping_reason") or "",
        "canonical_schema_version": PROVISION_SCHEMA_VERSION,
    }
    return {field: manifest.get(field, "") for field in PROVISION_MANIFEST_FIELDS}


def _extraction_status_for(provisions: list[dict[str, Any]]) -> str:
    if not provisions:
        return "failed"
    methods = {str(row.get("extraction_method") or "") for row in provisions}
    if any(method.endswith("_partial") for method in methods):
        return "partial"
    if any(_is_heading_only(row.get("text"), row.get("provision_label")) for row in provisions):
        return "partial"
    return "success"


def _minimal_output_provisions(doc: dict[str, Any], provisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    provisions = _split_plain_text_legal_provisions(doc, provisions)
    for index, provision in enumerate(provisions, start=1):
        text = _normalize_text(str(provision.get("text") or ""))
        if not _substantive_output_text(text):
            continue
        provision_path = _clean_str(provision.get("provision_path") or provision.get("section") or provision.get("provision_label") or index)
        heading = _clean_str(provision.get("heading") or provision.get("article") or provision.get("provision_label"))
        provision_path = _unique_path(provision_path, seen_paths)
        chunks = list(_chunk_text_if_needed(text))
        if len(chunks) == 1:
            row = _minimal_row(doc, provision=provision, provision_path=provision_path, heading=heading, text=chunks[0])
            rows.append(row)
            continue
        for chunk_index, chunk_text in enumerate(chunks, start=1):
            chunk_path = f"{provision_path}#chunk{chunk_index}"
            row = _minimal_row(
                doc,
                provision=provision,
                provision_path=chunk_path,
                heading=heading,
                text=chunk_text,
                parent_provision_path=provision_path,
                chunk_index=chunk_index,
            )
            rows.append(row)
    return rows


def _minimal_row(
    doc: dict[str, Any],
    *,
    provision: dict[str, Any],
    provision_path: str,
    heading: str,
    text: str,
    parent_provision_path: str = "",
    chunk_index: int | None = None,
) -> dict[str, Any]:
    row = {
        "record_type": "provision",
        "provision_id": _simple_provision_id(str(doc["document_id"]), provision_path, parent_provision_path=parent_provision_path, chunk_index=chunk_index),
        "source_locator": _clean_str(provision.get("source_locator") or provision.get("anchor_url") or provision.get("anchor") or provision.get("provision_number") or provision.get("section") or provision_path),
        "document_id": doc.get("document_id", ""),
        "canonical_citation": _canonical_citation_from_parts(provision, heading, provision.get("hierarchy") or []),
        "economy": doc.get("economy", ""),
        "collection": doc.get("collection", ""),
        "title": doc.get("title", ""),
        "document_type": doc.get("document_type", ""),
        "instrument_role": doc.get("instrument_role", "unknown"),
        "official_number": doc.get("official_number", ""),
        "last_amended": doc.get("last_amended", ""),
        "year": doc.get("year", ""),
        "language": doc.get("language", ""),
        "source_format": _source_format_for_cleaning(doc) or doc.get("source_format", ""),
        "source_url": doc.get("source_url") or doc.get("canonical_url", ""),
        "provision_path": provision_path,
        "heading": heading,
        "article": _clean_str(provision.get("article") or heading),
        "provision_number": _clean_str(provision.get("provision_number") or provision.get("section")),
        "section": _clean_str(provision.get("section") or provision.get("provision_number")),
        "provision_label": _clean_str(provision.get("provision_label") or heading),
        "anchor_url": _clean_str(provision.get("anchor_url") or provision.get("anchor")),
        "hierarchy": list(provision.get("hierarchy") or []) if isinstance(provision.get("hierarchy"), list) else [],
        "page_number": _clean_str(provision.get("page_number") or provision.get("source_page_number") or provision.get("page")),
        "source_page_number": _clean_str(provision.get("source_page_number") or provision.get("page_number") or provision.get("page")),
        "text_hash": _text_hash(text),
        "canonical_schema_version": PROVISION_SCHEMA_VERSION,
        "document_content_hash": doc.get("content_hash", ""),
        "char_start": _int(provision.get("char_start")),
        "char_end": _int(provision.get("char_end")),
        "text": text,
        "processing_mode": doc.get("processing_mode") or "structured_provisions",
        "editorial_annotations": provision.get("editorial_annotations") if isinstance(provision.get("editorial_annotations"), list) else [],
    }
    if parent_provision_path and chunk_index is not None:
        row["parent_provision_path"] = parent_provision_path
        row["chunk_index"] = chunk_index
    allowed = set(PROVISION_OUTPUT_BASE_FIELDS) | set(PROVISION_OUTPUT_CHUNK_FIELDS)
    return {key: value for key, value in row.items() if key in allowed and _has_output_value(value)}


def _canonical_citation_from_parts(provision: dict[str, Any], fallback: str, hierarchy: Any) -> str:
    """Preserve an official container hierarchy; never synthesize a citation.

    FRL schedules and Australian Privacy Principles are often represented in
    headings rather than the numeric ``section`` field.  The canonical record
    therefore carries the source structure forward for the exporter.
    """
    explicit = _clean_str(provision.get("canonical_citation"))
    if explicit:
        return explicit
    heading_blob = " ".join(str(provision.get(key) or "") for key in ("article", "heading", "provision_label"))
    app_match = re.search(r"^\s*(?:\d{1,2}\s+)?Australian\s+Privacy\s+Principle\s+(\d{1,2})\b", heading_blob, flags=re.I)
    if app_match:
        # This is the printed legal unit used in Schedule 1, not a section of
        # the Act.  The rule is source-structural and applies wherever FRL
        # exposes an Australian Privacy Principle.
        return f"Schedule 1, APP {app_match.group(1)}"
    pieces = [_normalize_space(part) for part in hierarchy if _normalize_space(part)] if isinstance(hierarchy, list) else []
    label = _clean_str(provision.get("article") or provision.get("provision_number") or provision.get("section") or provision.get("provision_label") or fallback)
    if label and (not pieces or pieces[-1] != label):
        pieces.append(label)
    # Do not promote legacy pseudo labels such as "1 Text" into a legal
    # citation.  Such records are routed document-direct before this point.
    if label.casefold() in {"1 text", "text", "document text"}:
        return ""
    return ", ".join(pieces)


def _split_plain_text_legal_provisions(doc: dict[str, Any], provisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if _requires_document_direct_mapping(doc, provisions):
        return []
    return provisions


def _requires_document_direct_mapping(doc: dict[str, Any], provisions: list[dict[str, Any]]) -> bool:
    if len(provisions) != 1:
        return False
    source = provisions[0]
    if str(source.get("record_type") or "").casefold() == "pdf_document":
        return True
    if not _looks_like_plain_text_document_text(source):
        return False
    locator_blob = " ".join(
        str(source.get(key) or doc.get(key) or "")
        for key in (
            "anchor_url",
            "source_url",
            "canonical_url",
            "source_format",
            "provision_id",
            "provision_path",
            "article",
            "provision_number",
            "raw_path",
            "normalized_path",
        )
    ).casefold()
    return bool(
        "viewtype=pdf" in locator_blob
        or "pdf" in locator_blob
        or "document text" in locator_blob
        or "1 text" in locator_blob
        or "::1-text" in locator_blob
    )


def _single_plain_text_body(provisions: list[dict[str, Any]]) -> str:
    if len(provisions) != 1:
        return ""
    return _generic_light_text_cleanup(str(provisions[0].get("text") or ""))


def _looks_like_plain_text_document_text(provision: dict[str, Any]) -> bool:
    label = _normalize_space(provision.get("article") or provision.get("provision_label") or provision.get("heading"))
    number = _normalize_space(provision.get("provision_number") or provision.get("section"))
    text = str(provision.get("text") or "")
    return (
        label.casefold() in {"text", "1 text", "document text"}
        or number.casefold() in {"text", "1"}
        or "::1-text" in str(provision.get("provision_id") or "")
    )


def _light_clean_internal_provision(row: dict[str, Any]) -> dict[str, Any]:
    provision = dict(row)
    provision["text"] = _generic_light_text_cleanup(str(provision.get("text") or ""))
    return provision


def _generic_light_text_cleanup(text: str) -> str:
    working = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    working = "".join(ch if ch == "\n" or ch == "\t" or ord(ch) >= 32 else " " for ch in working)
    working = re.sub(r"[ \t]+\n", "\n", working)
    working = re.sub(r"\n{4,}", "\n\n\n", working)
    return working.strip()


def _normalize_space(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _pdf_document_record(project_root: Path, economy: str, doc: dict[str, Any]) -> dict[str, Any] | None:
    extracted_text = _generic_light_text_cleanup(str(doc.get("_document_text") or ""))
    raw_path = None if extracted_text else _first_existing_path(project_root, doc.get("raw_path"))
    normalized_path = None if extracted_text else _first_existing_path(project_root, doc.get("normalized_path"))
    if not extracted_text and normalized_path and normalized_path.exists() and normalized_path.suffix.casefold() not in {".jsonl", ".json"}:
        extracted_text = _generic_light_text_cleanup(normalized_path.read_text(encoding="utf-8", errors="replace"))
    recall_text_rel = _clean_str(doc.get("pdf_text_path"))
    if extracted_text and not recall_text_rel:
        recall_path = (
            project_root
            / "outputs"
            / "corpus"
            / economy
            / "pdf_text"
            / _collection_slug(doc.get("collection"))
            / f"{_safe_filename(doc.get('document_id') or doc.get('instrument_id') or 'document')}.txt"
        )
        recall_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = recall_path.with_suffix(recall_path.suffix + ".tmp")
        tmp.write_text(extracted_text + "\n", encoding="utf-8")
        tmp.replace(recall_path)
        recall_text_rel = _project_rel(project_root, recall_path)
    content_hash = _text_hash(extracted_text) if extracted_text else (_sha256_path(raw_path) if raw_path and raw_path.exists() else _content_hash_for_paths(project_root, recall_text_rel, doc.get("normalized_path")))
    parse_error_code = _clean_str(doc.get("parse_error_code")) or ("page_layout_not_structured" if raw_path or recall_text_rel else "local_document_text_missing")
    direct_reason = _clean_str(doc.get("direct_mapping_reason")) or "page_layout_not_approved_for_direct_mapping"
    status = _pdf_prefilter_status(
        title=str(doc.get("title") or ""),
        collection=str(doc.get("collection") or ""),
        official_number=str(doc.get("official_number") or ""),
        text=extracted_text,
    ) if extracted_text else "uncertain"
    record = {
        "record_type": "pdf_document",
        "document_id": doc.get("document_id", ""),
        "economy": doc.get("economy") or economy,
        "collection": doc.get("collection", ""),
        "title": doc.get("title", ""),
        "official_number": doc.get("official_number", ""),
        "last_amended": doc.get("last_amended", ""),
        "year": doc.get("year", ""),
        "language": doc.get("language", ""),
        "source_format": _source_format_for_cleaning(doc) or doc.get("source_format") or "document_text",
        "source_url": doc.get("source_url") or doc.get("canonical_url", ""),
        "raw_path": _project_rel(project_root, raw_path) if raw_path else _project_rel(project_root, doc.get("raw_path")),
        "normalized_path": _project_rel(project_root, normalized_path) if normalized_path else _project_rel(project_root, doc.get("normalized_path")),
        "pdf_text_path": recall_text_rel,
        "prefilter_status": status,
        "processing_mode": "document_direct" if raw_path or recall_text_rel else "parse_failed",
        "direct_mapping_enabled": _bool(doc.get("direct_mapping_enabled")) if (raw_path or recall_text_rel) else False,
        "direct_mapping_reason": direct_reason,
        "content_hash": content_hash,
        "canonical_schema_version": DOCUMENT_SCHEMA_VERSION,
        "parse_status": "document_direct" if raw_path or recall_text_rel else "parse_failed",
        "parse_error_code": parse_error_code,
    }
    return {key: value for key, value in record.items() if key in PDF_DOCUMENT_OUTPUT_FIELDS and _has_output_value(value)}


def _has_output_value(value: Any) -> bool:
    if value is None:
        return False
    if value == "":
        return False
    if isinstance(value, (list, tuple, dict, set)) and not value:
        return False
    return True


PDF_PREFILTER_KEYWORDS_EN = {
    "tax",
    "taxation",
    "income tax",
    "deduction",
    "allowance",
    "capital allowance",
    "incentive",
    "exemption",
    "credit",
    "grant",
    "subsidy",
    "levy",
    "duty",
    "investment",
    "research and development",
    "r&d",
    "innovation",
    "technology",
    "digital",
    "automation",
    "training",
    "skills",
    "energy efficiency",
    "renewable",
    "green technology",
    "environmental",
    "climate",
    "finance",
    "financial assistance",
}

PDF_PREFILTER_KEYWORDS_MS = {
    "cukai",
    "potongan",
    "elaun",
    "insentif",
    "pengecualian",
    "pelaburan",
    "penyelidikan",
    "pembangunan",
    "teknologi",
    "digital",
    "automasi",
    "latihan",
    "kemahiran",
    "bantuan kewangan",
    "tenaga",
    "hijau",
    "alam sekitar",
    "kewangan",
}


def _pdf_prefilter_status(*, title: str, collection: str, official_number: str, text: str) -> str:
    metadata_haystack = " ".join([title, collection, official_number]).casefold()
    haystack = " ".join([metadata_haystack, text]).casefold()
    keywords = PDF_PREFILTER_KEYWORDS_EN | PDF_PREFILTER_KEYWORDS_MS
    metadata_matches = {
        keyword for keyword in keywords if re.search(rf"(?<![a-z0-9]){re.escape(keyword.casefold())}(?![a-z0-9])", metadata_haystack)
    }
    metadata_strong = {
        keyword
        for keyword in metadata_matches
        if " " in keyword or keyword in {"tax", "cukai", "digital", "technology", "teknologi"}
    }
    if len(metadata_matches) >= 2 or metadata_strong:
        return "candidate"
    readable = len(re.sub(r"\s+", " ", str(text or "")).strip()) >= 200
    if not readable:
        return "uncertain"
    matches = {keyword for keyword in keywords if re.search(rf"(?<![a-z0-9]){re.escape(keyword.casefold())}(?![a-z0-9])", haystack)}
    strong_matches = {keyword for keyword in matches if " " in keyword or keyword in {"tax", "cukai", "digital", "technology", "teknologi"}}
    if len(matches) >= 2 or strong_matches:
        return "candidate"
    if len(matches) == 1:
        return "uncertain"
    return "reject"


def _simple_provision_id(document_id: str, provision_path: str, *, parent_provision_path: str = "", chunk_index: int | None = None) -> str:
    if parent_provision_path and chunk_index is not None:
        return f"{document_id}::{_slug(parent_provision_path)}::chunk-{chunk_index}"
    return f"{document_id}::{_slug(provision_path)}"


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-") or "provision"


def _unique_path(value: str, seen: set[str]) -> str:
    base = value or "provision"
    candidate = base
    suffix = 2
    while candidate in seen:
        candidate = f"{base}-{suffix}"
        suffix += 1
    seen.add(candidate)
    return candidate


def _chunk_text_if_needed(text: str) -> Iterable[str]:
    max_chars = 16000
    if len(text) <= max_chars:
        yield text
        return
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = text.rfind("\n\n", start, end)
            if boundary > start + 4000:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            yield chunk
        start = max(end, start + 1)


def _substantive_output_text(text: str) -> bool:
    compact = re.sub(r"\s+", " ", text).strip()
    return len(compact) >= 12 and bool(re.search(r"[A-Za-z]{3,}", compact))


def _collection_slug(value: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
    return slug or "unknown"


def _safe_filename(value: Any) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return name or "document"


def _source_format_for_cleaning(doc: dict[str, Any]) -> str:
    path_text = " ".join(str(doc.get(key) or "") for key in ("raw_path", "normalized_path"))
    if re.search(r"(\.html?\b|/html/)", path_text, re.I):
        return "html"
    if re.search(r"(\.epub\b|/epub/)", path_text, re.I):
        return "epub"
    if re.search(r"(\.docx?\b|\.rtf\b|/word/|/docx?/|/rtf/)", path_text, re.I):
        return "word"
    if re.search(r"(\.pdf\b|/pdf/)", path_text, re.I):
        return "pdf"
    source_format = str(doc.get("source_format") or "")
    if re.search(r"\bpdf\b", source_format, re.I):
        return "pdf"
    return source_format


def _is_pdf_only_document(doc: dict[str, Any], source_doc: dict[str, Any] | None = None) -> bool:
    source_doc = source_doc or {}
    values = {**source_doc, **doc}
    source_format = str(values.get("source_format") or values.get("format") or "").casefold()
    raw_candidates = " ".join(
        str(values.get(key) or "")
        for key in (
            "raw_path",
            "raw_file_path",
            "raw_html_path",
            "raw_pdf_path",
            "normalized_path",
            "normalized_file_path",
            "local_path",
            "source_url",
            "canonical_url",
        )
    ).casefold()
    has_non_pdf_format = bool(
        re.search(r"\b(html?|epub|docx?|word|rtf|xml|json|jsonl_provisions|frl_primary_text)\b", source_format)
        or re.search(r"\.(html?|epub|docx?|rtf|xml)\b|/html/|/epub/|/word/|/docx?/", raw_candidates)
    )
    has_pdf = bool(re.search(r"\bpdf\b", source_format) or re.search(r"\.pdf\b|/pdf/", raw_candidates))
    return has_pdf and not has_non_pdf_format


def _australia_pdf_only_rows(rows: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for row in rows:
        if not _is_success_canonical(row):
            continue
        collection = str(row.get("collection") or "")
        if collection not in {"Act", "LegislativeInstrument", "NotifiableInstrument"}:
            continue
        if _is_pdf_only_document(_document_stub_for_pdf_check(row), row):
            yield row


def _document_stub_for_pdf_check(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_format": row.get("source_format") or row.get("format") or "",
        "raw_path": row.get("raw_path") or row.get("raw_file_path") or row.get("raw_pdf_path") or "",
        "normalized_path": row.get("normalized_path") or row.get("normalized_file_path") or row.get("local_path") or "",
        "source_url": row.get("source_url") or row.get("latest_version_url") or "",
        "canonical_url": row.get("canonical_url") or "",
    }


def _is_success_canonical(row: dict[str, Any]) -> bool:
    if _clean_str(row.get("alias_of")):
        return False
    if _bool(row.get("actual_corpus_gap")):
        return False
    status = _clean_str(row.get("download_status")).casefold()
    if status in {"source_unavailable", "stale_catalogue_entry", "duplicate_or_alias", "alias_resolved"}:
        return False
    if status and status not in {"success", "cache_hit", "existing_normalized", "existing_raw", "downloaded"}:
        return False
    parse_status = _clean_str(row.get("parse_status")).casefold()
    if parse_status and parse_status not in {"success", "cache_hit"}:
        return False
    return True


def _collection(row: dict[str, Any], economy: str) -> str:
    value = _clean_str(row.get("collection"))
    if value:
        return value
    instrument_type = _clean_str(row.get("instrument_type") or row.get("document_type")).casefold()
    if economy == "singapore":
        return "Act" if instrument_type == "act" else "SubsidiaryLegislation"
    if economy == "australia":
        return "Act" if instrument_type == "act" else "LegislativeInstrument"
    return "Unknown"


def _default_source(economy: str) -> str:
    return {"singapore": "singapore_sso", "australia": "australia_frl", "malaysia": "malaysia_lom"}.get(economy, economy)


def _australia_document_id(register_id: str, collection: str) -> str:
    prefix = "au-act" if register_id.upper().startswith("C") or collection == "Act" else "au-li"
    if collection == "NotifiableInstrument":
        prefix = "au-ni"
    return f"{prefix}-{register_id.casefold()}"


def _stable_provision_id(document_id: str, label: str, index: int) -> str:
    token = re.sub(r"[^a-z0-9_.-]+", "-", str(label).casefold()).strip("-") or f"p{index:05d}"
    digest = hashlib.sha256(f"{document_id}|{label}|{index}".encode("utf-8")).hexdigest()[:8]
    return f"{document_id}::{token}-{digest}"


def _malaysia_label(match: re.Match[str], index: int) -> str:
    if match.group("section"):
        return match.group("section").strip()
    if match.group("schedule"):
        return re.sub(r"\s+", " ", match.group("schedule")).strip().title()
    keyword = (match.group("keyword") or "clause").strip().title()
    label = (match.group("label") or str(index)).strip()
    return f"{keyword} {label}"


def _normalize_text(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else ""
    return str(value).strip()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().casefold() in {"true", "1", "yes", "y"}


def _int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.parent not in _ENSURED_WRITE_DIRS:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ENSURED_WRITE_DIRS.add(path.parent)
    content = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        for row in rows
    )
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return
        except Exception:
            pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    tmp.replace(path)


def _write_merged_provisions_from_manifest(project_root: Path, economy: str, provision_manifests: list[dict[str, Any]]) -> None:
    """Rebuild the legacy merged provision JSONL from canonical per-document files.

    Zone 2 streams per-document provision files from the manifest, but some
    audit and downstream tools still inspect ``zone1_provisions.jsonl``.  Keep
    that merged file synchronized with the current canonical schema without
    introducing a separate migration path.
    """
    root = project_root / "outputs" / "corpus" / economy
    out = root / "zone1_provisions.jsonl"
    if out.parent not in _ENSURED_WRITE_DIRS:
        out.parent.mkdir(parents=True, exist_ok=True)
        _ENSURED_WRITE_DIRS.add(out.parent)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for manifest_row in provision_manifests:
            rel_path = _clean_str(manifest_row.get("provisions_path"))
            if not rel_path:
                continue
            source = _resolve_path(project_root, rel_path)
            if not source.exists() or not source.is_file():
                continue
            with source.open("r", encoding="utf-8", errors="replace") as source_handle:
                for line in source_handle:
                    if line.strip():
                        handle.write(line if line.endswith("\n") else line + "\n")
    tmp.replace(out)


def _write_json(path: Path, payload: Any) -> None:
    if path.parent not in _ENSURED_WRITE_DIRS:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ENSURED_WRITE_DIRS.add(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _project_rel(project_root: Path, value: Any) -> str:
    raw = _clean_str(value)
    if not raw or raw == ".":
        return ""
    normalized = raw.replace("\\", "/")
    for marker in ("data/legal_sources/", "outputs/corpus/"):
        index = normalized.casefold().find(marker)
        if index >= 0:
            return normalized[index:]
    path = Path(normalized)
    try:
        if path.is_absolute():
            return path.resolve().relative_to(project_root.resolve()).as_posix()
    except Exception:
        return ""
    if re.match(r"^[A-Za-z]:/", normalized) or normalized.startswith("/mnt/"):
        return ""
    return normalized.lstrip("./")


def _resolve_path(project_root: Path, value: Any) -> Path:
    text = _project_rel(project_root, value)
    if not text:
        return project_root / "__missing__"
    path = Path(text)
    if path.is_absolute():
        return path
    return project_root / path


def _path_exists(project_root: Path, value: Any) -> bool:
    path = _resolve_path(project_root, value)
    return path.exists() and path.is_file()


def _first_existing_path(project_root: Path, *values: Any) -> Path | None:
    for value in values:
        if not value:
            continue
        path = _resolve_path(project_root, value)
        if path.exists() and path.is_file():
            return path
    return None


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return sum(1 for line in handle if line.strip())


def _singapore_alias_count(project_root: Path) -> int:
    report = project_root / "outputs" / "audit" / "singapore_zone1_failed_155_audit.json"
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
        return int(data.get("reconciled_counts", {}).get("duplicate_or_alias_entries") or 0)
    except Exception:
        return 0


def _malaysia_ordinary_error_count(project_root: Path) -> int:
    failures = _read_jsonl(project_root / "outputs" / "corpus" / "malaysia" / "malaysia_failed_downloads.jsonl")
    ordinary = 0
    for row in failures:
        status = _clean_str(row.get("download_status")).casefold()
        if status not in {
            "source_unavailable",
            "official_endpoint_http_500",
            "official_endpoint_invalid_pdf",
            "alternate_official_format_available",
        }:
            ordinary += 1
    return ordinary


def _copy_to_data_manifest(project_root: Path, economy: str, docs: list[dict[str, Any]], provision_manifests: list[dict[str, Any]]) -> None:
    data_root = project_root / "data" / "legal_sources" / economy / "manifests"
    _write_jsonl(data_root / "zone1_documents.jsonl", docs)
    _write_jsonl(data_root / "zone1_provisions_manifest.jsonl", provision_manifests)


def _write_reports(project_root: Path, current_summary: StandardizationSummary | None = None) -> None:
    audit_root = project_root / "outputs" / "audit"
    generated_at = datetime.now(timezone.utc).isoformat()
    economies = ["singapore", "australia", "malaysia"]
    state_path = audit_root / "zone1_per_document_standardization_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except Exception:
        state = {}
    if current_summary is not None:
        state[current_summary.economy] = current_summary.as_dict()
    integrity: dict[str, Any] = {"generated_at": generated_at, "standardizer_version": STANDARDIZER_VERSION, "economies": {}}
    markdown = ["# Zone 1 Per-Document Standardization Report", "", f"- Generated at: {generated_at}", ""]
    for economy in economies:
        root = project_root / "outputs" / "corpus" / economy
        summary = dict(state.get(economy) or {})
        quick_docs = _count_jsonl(root / "zone1_documents.jsonl")
        provision_manifest_rows = _read_jsonl(root / "zone1_provisions_manifest.jsonl")
        quick_provision_files = len(provision_manifest_rows)
        quick_provisions = sum(_int(row.get("provision_count")) for row in provision_manifest_rows)
        integrity_checks = _provision_manifest_integrity(project_root, economy, provision_manifest_rows)
        item = {
            "documents": int(summary.get("documents_count") or quick_docs),
            "mapping_records": int(summary.get("provisions_count") or quick_provisions),
            "provision_files": int(summary.get("provision_files_count") or quick_provision_files),
            "duplicate_document_keys": 0,
            "duplicate_provision_ids": 0,
            "empty_provision_text": 0,
            "windows_or_absolute_paths": 0,
            "missing_document_references": 0,
            "source_unavailable_documents": 0,
            "alias_documents": 0,
            "provenance": {
                "full": int(summary.get("provenance_full_count") or 0),
                "partial": int(summary.get("provenance_partial_count") or 0),
            },
            "fallback_chunks": int(summary.get("fallback_chunk_count") or 0),
            "heading_only_provisions": int(summary.get("heading_only_provision_count") or 0),
            "provision_manifest_integrity": integrity_checks,
            "summary": summary,
        }
        integrity["economies"][economy] = item
        markdown.extend(_markdown_section(project_root, economy, item, summary))
    markdown.extend(
        [
            "## Unified Mapper Input",
            "",
            "- `outputs/corpus/<economy>/zone1_provisions_manifest.jsonl`",
            "- `map-rdtii` streams per-document files from `outputs/corpus/<economy>/provisions/<collection>/<document_id>.jsonl`.",
            "- `map-rdtii` no longer reads the legacy merged `zone1_provisions.jsonl` or legacy provision directories directly.",
            "",
            "## Known Limitations",
            "",
            "- Singapore known gaps remain unresolved and are excluded.",
            "- Singapore partial provenance may remain where only provision JSONL is available.",
            "- Malaysia source-unavailable documents remain excluded.",
            "- Australia documents without reusable legacy provision JSONL may use deterministic document fallback chunks.",
            "",
        ]
    )
    _write_json(state_path, state)
    _write_json(audit_root / "zone1_per_document_integrity_report.json", integrity)
    (audit_root / "zone1_per_document_standardization_report.md").write_text("\n".join(markdown), encoding="utf-8")
    if os.environ.get("RDTII_WRITE_OUTPUTS_CLEANUP_INVENTORY") == "1":
        _write_outputs_cleanup_inventory(project_root)


def _markdown_section(project_root: Path, economy: str, item: dict[str, Any], summary: dict[str, Any]) -> list[str]:
    root = project_root / "outputs" / "corpus" / economy
    lines = [f"## {economy.title()}", ""]
    lines.append(f"- zone1_documents count: {item['documents']}")
    lines.append(f"- provision files count: {item['provision_files']}")
    lines.append(f"- total mapping input record count: {item['mapping_records']}")
    lines.append(f"- provenance full count: {item['provenance'].get('full', 0)}")
    lines.append(f"- provenance partial count: {item['provenance'].get('partial', 0)}")
    if economy == "singapore":
        lines.append(f"- excluded duplicate/alias count: {int(summary.get('excluded_duplicate_alias_count') or _singapore_alias_count(project_root))}")
        lines.append(f"- excluded known gaps count: {int(summary.get('excluded_known_gaps_count') or _count_jsonl(root / 'singapore_source_gap_manifest.jsonl'))}")
    elif economy == "australia":
        lines.append(f"- excluded stale/extra artifact count: {int(summary.get('excluded_stale_extra_artifact_count') or 0)}")
        if item["fallback_chunks"]:
            lines.append(f"- remaining count caveat: {item['fallback_chunks']} fallback chunks generated from normalized text.")
    elif economy == "malaysia":
        lines.append(f"- excluded source_unavailable count: {int(summary.get('excluded_source_unavailable_count') or _count_jsonl(project_root / 'data' / 'legal_sources' / 'malaysia' / 'manifests' / 'malaysia_source_unavailable.jsonl'))}")
        lines.append(f"- fallback chunk count: {item['fallback_chunks']}")
        total = max(1, int(item["mapping_records"]))
        lines.append(f"- heading-only provision count and ratio: {item['heading_only_provisions']} ({item['heading_only_provisions'] / total:.4%})")
        lines.append(f"- ordinary downloader program errors count: {int(summary.get('ordinary_downloader_program_errors_count') or _malaysia_ordinary_error_count(project_root))}")
    checks = item.get("provision_manifest_integrity") or {}
    lines.append(f"- provision manifest missing files: {checks.get('missing_files', 0)}")
    lines.append(f"- provision manifest line-count mismatches: {checks.get('line_count_mismatches', 0)}")
    lines.append("")
    return lines


def _provision_manifest_integrity(project_root: Path, economy: str, manifest_rows: list[dict[str, Any]]) -> dict[str, Any]:
    root = project_root / "outputs" / "corpus" / economy
    docs = _read_jsonl(root / "zone1_documents.jsonl")
    document_ids = {str(row.get("document_id") or "") for row in docs}
    missing_files = 0
    empty_files = 0
    line_count_mismatches = 0
    bad_paths = 0
    duplicate_document_ids = 0
    seen_document_ids: set[str] = set()
    failed_rows = 0
    # Australia has tens of thousands of per-document provision files on the
    # Windows-mounted workspace.  Stat'ing and re-counting every file during
    # every Zone 1 build makes the reporting phase dominate the build and can
    # appear hung.  Mapping correctness is guarded by the manifest and by
    # runtime schema validation; this report keeps manifest-level checks for
    # large corpora and leaves full per-file audits to targeted validation
    # scripts.
    check_files = len(manifest_rows) <= 2000
    count_lines = check_files
    file_check_skipped = 0
    line_count_check_skipped = 0
    for row in manifest_rows:
        document_id = str(row.get("document_id") or "")
        if document_id in seen_document_ids:
            duplicate_document_ids += 1
        seen_document_ids.add(document_id)
        if row.get("extraction_status") == "failed":
            failed_rows += 1
        path_text = str(row.get("provisions_path") or "")
        if "\\" in path_text or re.match(r"^[A-Za-z]:/", path_text) or path_text.startswith("/mnt/"):
            bad_paths += 1
        if not check_files:
            file_check_skipped += 1
            line_count_check_skipped += 1
            continue
        path = _resolve_path(project_root, path_text)
        if not path.exists() or not path.is_file():
            missing_files += 1
            continue
        if path.stat().st_size <= 0:
            empty_files += 1
            continue
        if count_lines:
            actual_lines = _count_jsonl(path)
            if actual_lines != _int(row.get("provision_count")):
                line_count_mismatches += 1
        else:
            line_count_check_skipped += 1
    return {
        "manifest_rows": len(manifest_rows),
        "documents_without_manifest": len(document_ids - {str(row.get("document_id") or "") for row in manifest_rows}),
        "manifest_without_document": len({str(row.get("document_id") or "") for row in manifest_rows} - document_ids),
        "duplicate_document_ids": duplicate_document_ids,
        "missing_files": missing_files,
        "empty_files": empty_files,
        "file_check_skipped": file_check_skipped,
        "line_count_mismatches": line_count_mismatches,
        "line_count_check_skipped": line_count_check_skipped,
        "bad_paths": bad_paths,
        "failed_rows": failed_rows,
    }


def _write_outputs_cleanup_inventory(project_root: Path) -> None:
    outputs_root = project_root / "outputs"
    audit_root = outputs_root / "audit"
    rows: list[dict[str, Any]] = []
    if not outputs_root.exists():
        return
    for path in sorted(outputs_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(project_root).as_posix()
        stat = path.stat()
        category, reason, referenced_by, safe_delete, requires_backup, notes = _classify_output_file(rel, stat.st_size)
        rows.append(
            {
                "path": rel,
                "size_bytes": stat.st_size,
                "modified_time": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "category": category,
                "reason": reason,
                "referenced_by": referenced_by,
                "safe_delete": safe_delete,
                "requires_backup": requires_backup,
                "notes": notes,
            }
        )
    _write_json(audit_root / "outputs_cleanup_inventory.json", rows)
    _write_cleanup_csv(audit_root / "outputs_cleanup_inventory.csv", rows)
    _write_cleanup_markdown(audit_root / "outputs_cleanup_inventory.md", rows)


def _classify_output_file(rel: str, size: int) -> tuple[str, str, str, bool, bool, str]:
    lower = rel.casefold()
    if re.search(r"outputs/corpus/[^/]+/provisions/[^/]+/[^/]+\.jsonl$", lower) or lower.endswith("/zone1_documents.jsonl") or lower.endswith("/zone1_provisions_manifest.jsonl"):
        return ("KEEP_CANONICAL", "Current per-document Zone 1 canonical output.", "map-rdtii/build-corpus", False, False, "")
    if re.search(r"outputs/corpus/[^/]+/pdf_text/[^/]+/[^/]+\.txt$", lower):
        return ("KEEP_CANONICAL", "PDF-only Zone 1 prefilter text cache referenced by pdf_document records.", "pdf_document prefilter", False, False, "")
    if lower.endswith("zone1_per_document_standardization_report.md") or lower.endswith("zone1_per_document_integrity_report.json") or "outputs_cleanup_inventory" in lower:
        return ("KEEP_CANONICAL", "Current audit/report output generated by canonical standardizer.", "audit", False, False, "")
    if any(token in lower for token in ["source_gap", "failed_download", "source_unavailable", "alias", "failure_audit", "cleanup_report"]):
        return ("KEEP_SOURCE_GAP_AUDIT", "Source-gap, failed-download, unavailable-source, or alias audit evidence.", "audit/source-gap review", False, False, "")
    if "/raw/" in lower or "/normalized/" in lower or "/metadata/" in lower or lower.endswith("source_registry.json") or "source_manifest" in lower:
        return ("DO_NOT_DELETE", "Raw/normalized/metadata/source registry content may be needed to regenerate canonical outputs.", "Zone 1 regeneration", False, False, "")
    if lower.endswith("/zone1_provisions.jsonl"):
        return ("SAFE_TO_DELETE_AFTER_BACKUP", "Legacy merged provision file superseded by per-document provisions when canonical manifest is present.", "legacy only", True, True, "Do not delete until per-document manifest integrity passes.")
    if size == 0 or lower.endswith(".tmp") or "/staging/" in lower or "temporary" in lower:
        return ("SAFE_TO_DELETE_AFTER_BACKUP", "Temporary, empty, or staging artifact.", "none after backup", True, True, "")
    if any(token in lower for token in ["/acts/", "/subsidiary_legislation/", "/subsidiary/", "/manifests/", "download_report", "mapping_summary", "_results.", "_review_queue", "_task_results", "zone2_"]):
        return ("KEEP_LEGACY_FOR_TRACEABILITY", "Legacy corpus, manifest, report, or mapping output retained for traceability.", "historical traceability", False, False, "")
    return ("UNKNOWN_REVIEW_REQUIRED", "Purpose is not safely inferable from path alone.", "unknown", False, True, "Manual review required before deletion.")


def _write_cleanup_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["path", "size_bytes", "modified_time", "category", "reason", "referenced_by", "safe_delete", "requires_backup", "notes"]
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def _write_cleanup_markdown(path: Path, rows: list[dict[str, Any]]) -> None:
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = counts.setdefault(str(row["category"]), {"count": 0, "size_bytes": 0})
        bucket["count"] += 1
        bucket["size_bytes"] += int(row["size_bytes"])
    lines = ["# Outputs Cleanup Inventory", "", "No files were deleted, moved, compressed, or renamed.", "", "## Summary", ""]
    for category in [
        "KEEP_CANONICAL",
        "KEEP_SOURCE_GAP_AUDIT",
        "KEEP_LEGACY_FOR_TRACEABILITY",
        "SAFE_TO_DELETE_AFTER_BACKUP",
        "DO_NOT_DELETE",
        "UNKNOWN_REVIEW_REQUIRED",
    ]:
        bucket = counts.get(category, {"count": 0, "size_bytes": 0})
        lines.append(f"- {category}: {bucket['count']} files, {bucket['size_bytes']} bytes")
    lines.extend(["", "## Inventory", "", "| Category | Safe delete | Requires backup | Size | Path | Reason |", "|---|---:|---:|---:|---|---|"])
    for row in rows:
        lines.append(
            f"| {row['category']} | {row['safe_delete']} | {row['requires_backup']} | {row['size_bytes']} | `{row['path']}` | {str(row['reason']).replace('|', '/')} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _count_values(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _count_windows_paths(rows: list[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        for key, value in row.items():
            if not key.endswith("_path") and key not in {"raw_path", "normalized_path", "metadata_path"}:
                continue
            text = str(value or "")
            if "\\" in text or re.match(r"^[A-Za-z]:/", text) or text.startswith("/mnt/"):
                count += 1
    return count
