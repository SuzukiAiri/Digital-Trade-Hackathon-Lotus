"""Persistent human review workbook and decisions for Zone 2.

This module deliberately implements only exact-match review persistence. It
does not learn rules, alter prompts, or write Mapper/Reviewer cache entries.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from html import escape
from pathlib import Path

from .economy_profiles import economy_profile
from .indicator_specs import INDICATOR_SPEC_VERSION, P4_INDICATOR_SPEC_VERSION
from .models import CandidateTask


SYSTEM_COLUMNS = [
    "Review Key",
    "Import Status",
    "Previous Decision Stale",
    "Economy",
    "Indicator",
    "Law Name",
    "Document ID",
    "Article / Provision",
    "Focal Quote",
    "Supporting References",
    "Supporting Context",
    "Source URL",
    "Coverage",
    "Sector",
    "Mapper Decision",
    "Reviewer Decision",
    "Resolver Reason",
    "Current Validated Attributes",
    "Review Reason Code",
    "Review Question",
    "Contract Version",
    "Source Fingerprint",
]

HUMAN_COLUMNS = [
    "Human Decision",
    "Human Rationale",
    "Reviewer Name",
    "Review Notes",
    "Corrected Indicator",
    "Corrected Focal Provision",
    "Corrected Focal Quote",
    "Corrected Validated Attributes JSON",
]

VISIBLE_REVIEW_COLUMNS = [
    "Human Decision",
    "Human Rationale",
    "Reviewer Name",
    "Indicator",
    "Law Name",
    "Article / Section",
    "Focal Provision",
    "Why Human Review Is Required",
    "Specific Review Question",
    "Relevant Supporting Excerpt",
    "Current Extracted Attributes",
    "Source URL",
    "Review Notes",
]
HIDDEN_REVIEW_COLUMNS = [column for column in [*SYSTEM_COLUMNS, *HUMAN_COLUMNS] if column not in VISIBLE_REVIEW_COLUMNS]
REVIEW_COLUMNS = [*VISIBLE_REVIEW_COLUMNS, *HIDDEN_REVIEW_COLUMNS]
DECISIONS = {"accepted", "rejected", "supporting_only", "technical_repair"}
INPUT_COLUMNS = {"Human Decision", "Human Rationale", "Reviewer Name", "Review Notes"}
DISPLAY_REFRESH_COLUMNS = [
    "Article / Section",
    "Focal Provision",
    "Why Human Review Is Required",
    "Specific Review Question",
    "Relevant Supporting Excerpt",
    "Current Extracted Attributes",
]


def review_dir(project_root: Path, economy_slug: str, *, scope: str = "p6_p7") -> Path:
    if scope not in {"p4", "p6_p7"}:
        raise ValueError(f"Unsupported human-review scope: {scope}")
    return project_root / "data" / "human_reviews" / economy_slug / scope


def review_key_for_task(task: CandidateTask, indicator_id: str | None = None, *, contract_version: str | None = None) -> str:
    indicator = indicator_id or task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else "")
    p4_scope = str(indicator).startswith("P4-")
    framework_element = next(
        (
            pattern.split(":", 1)[1]
            for pattern in task.matched_patterns
            if pattern.startswith("framework_element:")
        ),
        "",
    )
    return stable_review_key(
        economy=task.economy,
        indicator_id=indicator,
        document_id=task.document_id,
        citation_mode=task.citation_mode,
        source_locator=task.source_locator or task.focal_provision_id,
        source_text_hash=task.focal_text_hash or _sha(_norm(task.focal_quote or task.focal_text)),
        focal_provision_id=task.focal_provision_id,
        focal_quote=task.focal_quote or task.focal_text,
        supporting_refs=task.supporting_provision_ids,
        supporting_context=task.supporting_context,
        contract_version=contract_version or task.contract_version or INDICATOR_SPEC_VERSION,
        route_topic=task.route_topic if p4_scope else "",
        task_kind=task.task_kind if p4_scope else "",
        framework_element=framework_element if p4_scope else "",
    )


def stable_review_key(
    *,
    economy: str,
    indicator_id: str,
    document_id: str,
    citation_mode: str = "structured_provision",
    source_locator: str = "",
    source_text_hash: str = "",
    focal_provision_id: str = "",
    focal_quote: str = "",
    supporting_refs: list[str] | tuple[str, ...] | str = (),
    supporting_context: str = "",
    contract_version: str = "",
    route_topic: str = "",
    task_kind: str = "",
    framework_element: str = "",
) -> str:
    refs = supporting_refs if isinstance(supporting_refs, str) else "|".join(str(item) for item in supporting_refs)
    parts = [
        _norm(economy),
        _norm(indicator_id),
        _norm(document_id),
        _norm(citation_mode),
        _norm(source_locator or focal_provision_id),
        _norm(source_text_hash or _sha(_norm(focal_quote))),
        _sha(_norm(refs) + "|" + _norm(supporting_context)),
        _norm(contract_version),
    ]
    if route_topic or task_kind or framework_element:
        parts.extend([_norm(route_topic), _norm(task_kind), _norm(framework_element)])
    return _sha("||".join(parts))


def source_fingerprint_for_task(task: CandidateTask, indicator_id: str | None = None) -> str:
    return _sha(
        "||".join(
            [
                _norm(indicator_id or task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else "")),
                _norm(task.document_id),
                _norm(task.citation_mode),
                _norm(task.source_locator or task.focal_provision_id),
                _norm(task.focal_text_hash or ""),
                _norm(task.focal_quote or task.focal_text),
                _norm("|".join(task.supporting_provision_ids)),
                _norm(task.supporting_context),
                _norm(task.contract_version or INDICATOR_SPEC_VERSION),
            ]
        )
    )


def evidence_hash_for_task(task: CandidateTask) -> str:
    """Hash the current focal evidence text for persistent human decisions."""

    return task.focal_text_hash or _sha(_norm(task.focal_quote or task.focal_text))


def load_active_decisions(project_root: Path, economy_slug: str, *, scope: str = "p6_p7") -> dict[str, dict]:
    path = review_dir(project_root, economy_slug, scope=scope) / "decisions.jsonl"
    rows = _read_jsonl(path)
    latest: dict[str, dict] = {}
    superseded: set[str] = set()
    for row in rows:
        if row.get("supersedes_review_id"):
            superseded.add(str(row["supersedes_review_id"]))
        key = str(row.get("review_key") or "")
        if key and row.get("review_id") not in superseded:
            latest[key] = row
    return {key: row for key, row in latest.items() if row.get("review_id") not in superseded}


def import_completed_reviews(project_root: Path, economy_slug: str, *, scope: str = "p6_p7") -> dict:
    directory = review_dir(project_root, economy_slug, scope=scope)
    directory.mkdir(parents=True, exist_ok=True)
    workbook = directory / "human_review.xlsx"
    decisions_path = directory / "decisions.jsonl"
    decisions_path.touch(exist_ok=True)
    existing = _read_jsonl(decisions_path)
    latest_by_key = {str(row.get("review_key")): row for row in existing if row.get("review_key")}
    existing_fingerprints = {_decision_fingerprint(row) for row in existing}
    report = {"imported": 0, "skipped": 0, "conflicts": 0, "invalid": 0, "stale": 0}
    if not workbook.exists():
        _write_json(directory / "import_report.json", report)
        return report
    rows = _read_xlsx_sheet(workbook, "Review Queue")
    new_rows: list[dict] = []
    for row in rows:
        decision = str(row.get("Human Decision") or "").strip()
        if not decision:
            report["skipped"] += 1
            continue
        if decision not in DECISIONS:
            report["invalid"] += 1
            continue
        if not str(row.get("Human Rationale") or "").strip() or not str(row.get("Reviewer Name") or "").strip():
            report["invalid"] += 1
            continue
        if decision == "technical_repair" and not any(
            str(row.get(column) or "").strip()
            for column in (
                "Corrected Indicator",
                "Corrected Focal Provision",
                "Corrected Focal Quote",
                "Corrected Validated Attributes JSON",
            )
        ):
            report["invalid"] += 1
            continue
        if str(row.get("Previous Decision Stale") or "").casefold() == "true":
            report["stale"] += 1
            continue
        record = _decision_record_from_row(row, decision, latest_by_key.get(str(row.get("Review Key") or "")))
        fingerprint = _decision_fingerprint(record)
        if fingerprint in existing_fingerprints:
            report["skipped"] += 1
            continue
        new_rows.append(record)
        existing_fingerprints.add(fingerprint)
        latest_by_key[str(record["review_key"])] = record
        report["imported"] += 1
    if new_rows:
        with decisions_path.open("a", encoding="utf-8", newline="\n") as handle:
            for row in new_rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    _write_json(directory / "import_report.json", report)
    return report


def sync_human_review_workbook(
    project_root: Path,
    economy_slug: str,
    review_rows: list[dict],
    task_by_id: dict[str, CandidateTask],
    *,
    accepted_identities: set[tuple[str, str, str, str]] | None = None,
    output_dir: Path | None = None,
    scope: str = "p6_p7",
) -> dict:
    directory = review_dir(project_root, economy_slug, scope=scope)
    directory.mkdir(parents=True, exist_ok=True)
    import_report = import_completed_reviews(project_root, economy_slug, scope=scope)
    active = load_active_decisions(project_root, economy_slug, scope=scope)
    workbook_directory = output_dir or directory
    workbook_directory.mkdir(parents=True, exist_ok=True)
    workbook = workbook_directory / "human_review.xlsx"
    existing_rows = _read_xlsx_sheet(workbook, "Review Queue") if workbook.exists() else []
    by_key = {str(row.get("Review Key") or ""): row for row in existing_rows if row.get("Review Key")}
    accepted = accepted_identities or set()
    added = 0
    refreshed_rows: dict[str, dict] = {}
    for review in review_rows:
        if review.get("queue_type") != "human_legal_review":
            continue
        task = task_by_id.get(str(review.get("task_id") or ""))
        if task is None:
            identity = _pending_identity_for_unbound_review(review)
            if identity in accepted:
                continue
            key = _review_key_for_unbound_review(review)
            refreshed = _workbook_row_from_unbound_review(review, key)
        else:
            identity = pending_identity_for_task(task, str(review.get("indicator") or ""))
            if identity in accepted:
                continue
            key = review_key_for_task(task, str(review.get("indicator") or ""))
            refreshed = _workbook_row_from_review(review, task, key)
        if key in active:
            continue
        prior = by_key.get(key, {})
        if str(prior.get("Previous Decision Stale") or "").casefold() == "true":
            continue
        if key in by_key:
            merged = dict(by_key[key])
            for column in [*SYSTEM_COLUMNS, *DISPLAY_REFRESH_COLUMNS]:
                if column in {"Import Status", "Previous Decision Stale"}:
                    continue
                merged[column] = refreshed.get(column, merged.get(column, ""))
            for column in HUMAN_COLUMNS:
                merged[column] = by_key[key].get(column, "")
            merged["Import Status"] = "Pending"
            merged["Previous Decision Stale"] = "false"
            refreshed_rows[key] = merged
            continue
        refreshed_rows[key] = refreshed
        added += 1
    rows = list(refreshed_rows.values())
    _write_review_workbook(workbook, rows)
    report = {**import_report, "workbook_rows": len(rows), "pending_added": added, "path": str(workbook)}
    _write_json(directory / "import_report.json", report)
    return report


def _workbook_row_from_review(review: dict, task: CandidateTask, key: str) -> dict:
    attrs = review.get("reviewer_attributes") if isinstance(review.get("reviewer_attributes"), dict) else {}
    reason_code = "; ".join(review.get("uncertain_elements") or review.get("review_reasons") or [])
    resolver_reason = str(review.get("focal_uncertainty") or review.get("result_code") or reason_code or "")
    indicator = str(review.get("indicator") or task.indicator_id or "")
    return {
        "Review Key": key,
        "Import Status": "Pending",
        "Previous Decision Stale": "false",
        "Economy": economy_profile(task.economy).name,
        "Indicator": indicator,
        "Law Name": task.law_title,
        "Document ID": task.document_id,
        "Article / Provision": task.focal_provision_id,
        "Article / Section": _display_article_for_task(task),
        "Focal Quote": task.focal_quote or task.focal_text,
        "Focal Provision": task.focal_quote or task.focal_text,
        "Supporting References": "; ".join(task.supporting_provision_ids),
        "Supporting Context": task.supporting_context,
        "Relevant Supporting Excerpt": _relevant_supporting_excerpt(task.supporting_context),
        "Source URL": task.source_url or "",
        "Coverage": str(attrs.get("coverage") or ""),
        "Sector": str(attrs.get("sector") or ""),
        "Mapper Decision": json.dumps(review.get("decision") or {}, ensure_ascii=False, default=str),
        "Reviewer Decision": json.dumps(review.get("reviewer_decision") or {}, ensure_ascii=False, default=str),
        "Resolver Reason": resolver_reason,
        "Why Human Review Is Required": _human_reason_display(reason_code or resolver_reason, review),
        "Current Validated Attributes": json.dumps(attrs, ensure_ascii=False, default=str),
        "Current Extracted Attributes": _attributes_display(attrs),
        "Review Reason Code": reason_code,
        "Specific Review Question": _specific_review_question(indicator, reason_code or resolver_reason),
        "Review Question": _specific_review_question(indicator, reason_code or resolver_reason),
        "Contract Version": task.contract_version or INDICATOR_SPEC_VERSION,
        "Source Fingerprint": source_fingerprint_for_task(task, str(review.get("indicator") or "")),
        "Human Decision": "",
        "Corrected Indicator": "",
        "Corrected Focal Provision": "",
        "Corrected Focal Quote": "",
        "Corrected Validated Attributes JSON": "",
        "Human Rationale": "",
        "Reviewer Name": "",
        "Review Notes": "",
    }


def _workbook_row_from_unbound_review(review: dict, key: str) -> dict:
    indicator = str(review.get("indicator") or "")
    attrs = review.get("reviewer_attributes") if isinstance(review.get("reviewer_attributes"), dict) else {}
    reason_code = "; ".join(review.get("uncertain_elements") or review.get("review_reasons") or [])
    resolver_reason = str(review.get("focal_uncertainty") or review.get("result_code") or reason_code or "")
    article = str(review.get("focal_provision_id") or (f"PDF page {review.get('page_number')}" if review.get("page_number") else "Document-direct review"))
    return {
        "Review Key": key,
        "Import Status": "Pending",
        "Previous Decision Stale": "false",
        "Economy": economy_profile(str(review.get("economy") or "")).name,
        "Indicator": indicator,
        "Law Name": str(review.get("law_title") or review.get("document_id") or ""),
        "Document ID": str(review.get("document_id") or ""),
        "Article / Provision": article,
        "Article / Section": article,
        "Focal Quote": str(review.get("focal_quote") or review.get("verbatim_snippet") or ""),
        "Focal Provision": str(review.get("focal_quote") or review.get("verbatim_snippet") or "Document-level PDF/direct candidate requires legal review."),
        "Supporting References": "",
        "Supporting Context": "",
        "Relevant Supporting Excerpt": "",
        "Source URL": str(review.get("source_url") or ""),
        "Coverage": str(attrs.get("coverage") or ""),
        "Sector": str(attrs.get("sector") or ""),
        "Mapper Decision": json.dumps(review.get("decision") or {}, ensure_ascii=False, default=str),
        "Reviewer Decision": json.dumps(review.get("reviewer_decision") or {}, ensure_ascii=False, default=str),
        "Resolver Reason": resolver_reason,
        "Why Human Review Is Required": _human_reason_display(reason_code or resolver_reason, review),
        "Current Validated Attributes": json.dumps(attrs, ensure_ascii=False, default=str),
        "Current Extracted Attributes": _attributes_display(attrs),
        "Review Reason Code": reason_code,
        "Specific Review Question": _specific_review_question(indicator, reason_code or resolver_reason),
        "Review Question": _specific_review_question(indicator, reason_code or resolver_reason),
        "Contract Version": (
            P4_INDICATOR_SPEC_VERSION
            if indicator.startswith("P4-")
            else INDICATOR_SPEC_VERSION
        ),
        "Source Fingerprint": _sha("||".join([str(review.get("document_id") or ""), indicator, str(review.get("claim_id") or ""), article, str(review.get("focal_quote") or review.get("verbatim_snippet") or ""), resolver_reason])),
        "Human Decision": "",
        "Corrected Indicator": "",
        "Corrected Focal Provision": "",
        "Corrected Focal Quote": "",
        "Corrected Validated Attributes JSON": "",
        "Human Rationale": "",
        "Reviewer Name": "",
        "Review Notes": "",
    }


def _review_key_for_unbound_review(review: dict) -> str:
    indicator = str(review.get("indicator") or "")
    return stable_review_key(
        economy=str(review.get("economy") or ""),
        indicator_id=indicator,
        document_id=str(review.get("document_id") or ""),
        citation_mode="document_direct" if str(review.get("task_id") or "").startswith("docdirect:") else "structured_provision",
        source_locator=str(review.get("claim_id") or review.get("focal_provision_id") or review.get("page_number") or review.get("task_id") or ""),
        source_text_hash=_sha(_norm(str(review.get("focal_quote") or review.get("verbatim_snippet") or review.get("focal_uncertainty") or ""))),
        focal_provision_id=str(review.get("focal_provision_id") or ""),
        focal_quote=str(review.get("focal_quote") or review.get("verbatim_snippet") or ""),
        supporting_refs=(),
        supporting_context=str(review.get("claim_id") or review.get("focal_uncertainty") or ""),
        contract_version=(
            P4_INDICATOR_SPEC_VERSION
            if indicator.startswith("P4-")
            else INDICATOR_SPEC_VERSION
        ),
    )


def review_key_for_unbound_review(review: dict) -> str:
    return _review_key_for_unbound_review(review)


def _pending_identity_for_unbound_review(review: dict) -> tuple[str, str, str, str]:
    return (
        economy_profile(str(review.get("economy") or "")).name,
        str(review.get("indicator") or ""),
        str(review.get("document_id") or ""),
        str(review.get("focal_provision_id") or review.get("page_number") or ""),
    )


def _display_article_for_task(task: CandidateTask) -> str:
    meta = task.provision_metadata_snapshot or {}
    for key in ("article", "provision_number", "section", "heading"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    return str(task.focal_provision_id or "").strip()


def _human_reason_display(reason_code: str, review: dict) -> str:
    text = str(reason_code or "").strip()
    reviewer_decision = review.get("reviewer_decision")
    reviewer_reason = str(reviewer_decision.get("review_reason") or "") if isinstance(reviewer_decision, dict) else ""
    mapping = {
        "RETENTION_ATTRIBUTE_MISSING": "The provision requires records to be retained, but the minimum duration or trigger event is not conclusively established.",
        "MINIMUM_DURATION_VALUE": "The provision may require retention, but the minimum duration value is unclear.",
        "MINIMUM_DURATION_UNIT": "The provision may require retention, but the duration unit is unclear.",
        "TRIGGER_EVENT": "The provision may require retention, but the event that starts the period is unclear.",
        "PERSONAL_OR_IDENTIFIABLE_DATA": "It is unclear whether the information accessible to the authority is personal or otherwise identifiable data.",
        "FRAMEWORK_COVERAGE": "It is unclear whether this provision forms part of a general framework or only a sector-specific rule.",
        "JUDICIAL_AUTHORIZATION": "The provision creates an access power, but the requirement for prior judicial authorisation is unclear.",
        "JUDICIAL_AUTHORIZATION_MISSING": "The provision creates an access power, but the requirement for prior judicial authorisation is unclear.",
        "ACCOUNTABILITY_PATH": "It is unclear whether the provision creates a DPO or DPIA accountability obligation.",
        "ACCOUNTABILITY_PATH_MISSING": "It is unclear whether the provision creates a DPO or DPIA accountability obligation.",
        "REVIEWER_FINAL_DECISION_UNCERTAIN": reviewer_reason or "The reviewer marked the legal conclusion as uncertain.",
        "LEGAL_UNCERTAINTY": "The legal effect of the focal provision is uncertain and requires human review.",
    }
    for code, display in mapping.items():
        if code in text:
            return _shorten(display, 260)
    return _shorten(reviewer_reason or text or "Human legal review is required.", 260)


def _specific_review_question(indicator: str, reason_code: str) -> str:
    if indicator == "P7-I3":
        return "Does this provision itself establish a mandatory minimum retention period, and what event starts that period?"
    if indicator == "P7-I5":
        return "Does the compelled information include personal or identifiable data, and is prior judicial authorisation required?"
    if indicator == "P7-I2":
        return "Does this provision independently contribute to the cybersecurity framework, or is it only supporting/sectoral context?"
    if indicator == "P7-I1":
        return "Does this provision independently contribute to the personal data protection framework, or is it only supporting context?"
    if indicator == "P7-I4":
        return "Does the focal provision itself require designation of a DPO or completion of a DPIA?"
    return "Does the focal provision itself satisfy the indicator, or is it rejected/supporting/technical repair?"


def _relevant_supporting_excerpt(context: str) -> str:
    lines = [line.strip() for line in str(context or "").splitlines() if line.strip()]
    if not lines:
        return ""
    excerpts = []
    for line in lines[:3]:
        excerpts.append(_shorten(line, 220))
    return "\n\n".join(excerpts)


def _attributes_display(attrs: dict) -> str:
    labels = {
        "retention_periods": "Retention period",
        "trigger_event": "Trigger",
        "record_scope_basis": "Record scope",
        "accountability_path": "Accountability path",
        "judicial_authorization": "Judicial authorization",
        "framework_element": "Framework element",
        "legal_function": "Legal function",
        "coverage": "Coverage",
        "sector": "Sector",
    }
    parts: list[str] = []
    for key, label in labels.items():
        value = attrs.get(key)
        if value is None or value == "" or value == [] or value == {}:
            continue
        if key == "retention_periods" and isinstance(value, list):
            periods = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                period = " ".join(str(item.get(part) or "").strip() for part in ("value", "unit") if str(item.get(part) or "").strip())
                trigger = str(item.get("trigger_event") or "").strip()
                condition = str(item.get("condition") or "").strip()
                text = period
                if trigger:
                    text += f"; trigger: {trigger}"
                if condition:
                    text += f"; condition: {condition}"
                if text.strip():
                    periods.append(text.strip())
            if periods:
                parts.append(f"{label}: {' | '.join(periods)}")
            continue
        parts.append(f"{label}: {value}")
    return "\n".join(parts)


def _shorten(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].strip()
    return cut or text[:limit]


def _decision_record_from_row(row: dict, human_decision: str, previous: dict | None) -> dict:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    corrected_attrs = str(row.get("Corrected Validated Attributes JSON") or "").strip()
    review_key = str(row.get("Review Key") or "")
    review_id = _sha("|".join([review_key, human_decision, corrected_attrs, str(row.get("Human Rationale") or ""), str(row.get("Reviewer Name") or "")]))
    return {
        "review_id": review_id,
        "review_key": review_key,
        "economy": row.get("Economy") or "",
        "indicator_id": row.get("Indicator") or "",
        "document_id": row.get("Document ID") or "",
        "focal_provision_id": row.get("Article / Provision") or "",
        "focal_quote": row.get("Focal Quote") or "",
        "supporting_refs": [item.strip() for item in str(row.get("Supporting References") or "").split(";") if item.strip()],
        "source_fingerprint": row.get("Source Fingerprint") or "",
        "contract_version": row.get("Contract Version") or "",
        "original_mapper_decision": row.get("Mapper Decision") or "",
        "original_reviewer_decision": row.get("Reviewer Decision") or "",
        "original_resolver_reason": row.get("Resolver Reason") or "",
        "original_validated_attributes": row.get("Current Validated Attributes") or "",
        "human_decision": human_decision,
        "corrected_indicator_id": row.get("Corrected Indicator") or "",
        "corrected_focal_provision_id": row.get("Corrected Focal Provision") or "",
        "corrected_focal_quote": row.get("Corrected Focal Quote") or "",
        "corrected_validated_attributes": corrected_attrs,
        "human_rationale": row.get("Human Rationale") or "",
        "reviewer_name": row.get("Reviewer Name") or "",
        "review_notes": row.get("Review Notes") or "",
        "decision_source": "human_review",
        "reviewed_at": now,
        "imported_at": now,
        "supersedes_review_id": previous.get("review_id") if previous else None,
        "is_active": True,
    }


def _decision_fingerprint(row: dict) -> str:
    return _sha(
        json.dumps(
            {
                "review_key": row.get("review_key"),
                "human_decision": row.get("human_decision"),
                "corrected_indicator_id": row.get("corrected_indicator_id"),
                "corrected_focal_provision_id": row.get("corrected_focal_provision_id"),
                "corrected_focal_quote": row.get("corrected_focal_quote"),
                "corrected_validated_attributes": row.get("corrected_validated_attributes"),
                "human_rationale": row.get("human_rationale"),
                "reviewer_name": row.get("reviewer_name"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _write_review_workbook(path: Path, rows: list[dict]) -> None:
    instructions = [
        {"Instruction": "Human Review Instructions"},
        {"Instruction": "accepted: the focal provision itself satisfies the current indicator."},
        {"Instruction": "rejected: the focal provision does not satisfy the indicator."},
        {"Instruction": "supporting_only: the clause can support another focal provision but cannot be submitted independently."},
        {"Instruction": "technical_repair: source, citation, or structured data is incorrect and needs correction."},
        {"Instruction": "1. Read the focal provision and review issue."},
        {"Instruction": "2. Select a decision and provide a rationale and reviewer name."},
        {"Instruction": "3. Use technical_repair only when citation or structured data is incorrect."},
        {"Instruction": "Do not edit hidden system fields. The next normal map-rdtii run imports completed decisions automatically."},
    ]
    sheets = {
        "Review Queue": (REVIEW_COLUMNS, rows),
        "Instructions": (["Instruction"], instructions),
    }
    _write_xlsx(path, sheets)


def _read_xlsx_sheet(path: Path, sheet_name: str) -> list[dict]:
    if not path.exists():
        return []
    try:
        with zipfile.ZipFile(path) as zf:
            shared = _read_shared_strings(zf)
            sheet_path = _sheet_path(zf, sheet_name)
            if not sheet_path:
                return []
            root = ET.fromstring(zf.read(sheet_path))
    except Exception:
        return []
    rows: list[list[str]] = []
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    for row in root.findall(f".//{ns}row"):
        values: dict[int, str] = {}
        for cell in row.findall(f"{ns}c"):
            ref = cell.attrib.get("r", "")
            col = _col_to_index(re.sub(r"\d+", "", ref)) if ref else len(values) + 1
            ctype = cell.attrib.get("t")
            value = ""
            if ctype == "s":
                node = cell.find(f"{ns}v")
                if node is not None and node.text is not None:
                    value = shared[int(node.text)]
            elif ctype == "inlineStr":
                node = cell.find(f"{ns}is/{ns}t")
                value = node.text if node is not None and node.text is not None else ""
            else:
                node = cell.find(f"{ns}v")
                value = node.text if node is not None and node.text is not None else ""
            values[col] = value
        if values:
            rows.append([values.get(i, "") for i in range(1, max(values) + 1)])
    if not rows:
        return []
    headers = rows[0]
    return [dict(zip(headers, row + [""] * (len(headers) - len(row)))) for row in rows[1:]]


def _write_xlsx(path: Path, sheets: dict[str, tuple[list[str], list[dict]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    shared: list[str] = []
    shared_index: dict[str, int] = {}

    def sst(value: object) -> int:
        text = str(value or "")
        if text not in shared_index:
            shared_index[text] = len(shared)
            shared.append(text)
        return shared_index[text]

    sheet_xml: dict[str, str] = {}
    for sheet_idx, (name, (columns, rows)) in enumerate(sheets.items(), start=1):
        lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
        validation_xml = ""
        auto_filter_xml = ""
        pane_xml = ""
        cols_xml = ""
        if name == "Review Queue":
            last_row = max(2, len(rows) + 1)
            human_col = _col(REVIEW_COLUMNS.index("Human Decision") + 1)
            validation_xml = f'<dataValidations count="1"><dataValidation type="list" allowBlank="1" sqref="{human_col}2:{human_col}{last_row}"><formula1>"accepted,rejected,supporting_only,technical_repair"</formula1></dataValidation></dataValidations>'
            conditional_xml = f'<conditionalFormatting sqref="{human_col}2:{human_col}{last_row}"><cfRule type="containsText" priority="1" operator="containsText" text="accepted"><formula>NOT(ISERROR(SEARCH("accepted",{human_col}2)))</formula></cfRule><cfRule type="containsText" priority="2" operator="containsText" text="rejected"><formula>NOT(ISERROR(SEARCH("rejected",{human_col}2)))</formula></cfRule><cfRule type="containsText" priority="3" operator="containsText" text="supporting_only"><formula>NOT(ISERROR(SEARCH("supporting_only",{human_col}2)))</formula></cfRule><cfRule type="containsText" priority="4" operator="containsText" text="technical_repair"><formula>NOT(ISERROR(SEARCH("technical_repair",{human_col}2)))</formula></cfRule></conditionalFormatting>'
            auto_filter_xml = f'<autoFilter ref="A1:{_col(len(columns))}{last_row}"/>'
            pane_xml = '<sheetViews><sheetView workbookViewId="0"><pane xSplit="6" ySplit="1" topLeftCell="G2" activePane="bottomRight" state="frozen"/></sheetView></sheetViews>'
            widths_by_name = {
                "Human Decision": 18,
                "Human Rationale": 36,
                "Reviewer Name": 20,
                "Indicator": 12,
                "Law Name": 34,
                "Article / Section": 14,
                "Focal Provision": 55,
                "Why Human Review Is Required": 36,
                "Specific Review Question": 36,
                "Relevant Supporting Excerpt": 48,
                "Current Extracted Attributes": 30,
                "Source URL": 28,
                "Review Notes": 28,
            }
            cols_xml = "<cols>" + "".join(
                f'<col min="{idx}" max="{idx}" width="{widths_by_name.get(column, 18)}" customWidth="1"{ " hidden=\"1\"" if column in HIDDEN_REVIEW_COLUMNS else ""}/>'
                for idx, column in enumerate(columns, start=1)
            ) + "</cols>"
        elif name == "Instructions":
            cols_xml = '<cols><col min="1" max="1" width="120" customWidth="1"/></cols>'
            pane_xml = '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        if pane_xml:
            lines.append(pane_xml)
        if cols_xml:
            lines.append(cols_xml)
        lines.append("<sheetData>")
        all_rows = [columns] + [[row.get(col, "") for col in columns] for row in rows]
        for r_idx, row in enumerate(all_rows, start=1):
            row_height = "30" if r_idx == 1 else "45"
            lines.append(f'<row r="{r_idx}" ht="{row_height}" customHeight="1">')
            for c_idx, value in enumerate(row, start=1):
                style = _cell_style_id(name, columns[c_idx - 1], r_idx)
                lines.append(f'<c r="{_col(c_idx)}{r_idx}" s="{style}" t="s"><v>{sst(value)}</v></c>')
            lines.append("</row>")
        lines.append("</sheetData>")
        if auto_filter_xml:
            lines.append(auto_filter_xml)
        if name == "Review Queue":
            lines.append(conditional_xml)
        if validation_xml:
            lines.append(validation_xml)
        lines.append("</worksheet>")
        sheet_xml[f"xl/worksheets/sheet{sheet_idx}.xml"] = "".join(lines)
    workbook_sheets = []
    workbook_rels = []
    for idx, name in enumerate(sheets, start=1):
        workbook_sheets.append(f'<sheet name="{escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>')
        workbook_rels.append(f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>')
    content_types = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">']
    content_types.append('<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>')
    content_types.append('<Default Extension="xml" ContentType="application/xml"/>')
    content_types.append('<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>')
    content_types.append('<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>')
    content_types.append('<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>')
    for idx in range(1, len(sheets) + 1):
        content_types.append(f'<Override PartName="/xl/worksheets/sheet{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    content_types.append("</Types>")
    with zipfile.ZipFile(path.with_suffix(".tmp"), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "".join(content_types))
        zf.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        zf.writestr("xl/workbook.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{"".join(workbook_sheets)}</sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{"".join(workbook_rels)}<Relationship Id="rIdSharedStrings" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/><Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        zf.writestr("xl/styles.xml", _styles_xml())
        zf.writestr("xl/sharedStrings.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared)}" uniqueCount="{len(shared)}">{"".join(f"<si><t>{escape(text)}</t></si>" for text in shared)}</sst>')
        for name, xml in sheet_xml.items():
            zf.writestr(name, xml)
    path.with_suffix(".tmp").replace(path)


def pending_identity_for_task(task: CandidateTask, indicator_id: str) -> tuple[str, str, str, str]:
    return (
        str(task.economy or ""),
        str(indicator_id or task.indicator_id or ""),
        str(task.document_id or ""),
        str(task.focal_provision_id or ""),
    )


def _cell_style_id(sheet_name: str, column: str, row_index: int) -> int:
    if row_index == 1:
        return 1
    if sheet_name == "Instructions":
        return 4
    if column in HUMAN_COLUMNS:
        return 3
    return 2


def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        '</fonts>'
        '<fills count="4">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFD9D9D9"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/><bgColor indexed="64"/></patternFill></fill>'
        '</fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="5">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"><alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="2" borderId="0" xfId="0" applyFill="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
        '</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )


def _read_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    out = []
    for item in root.findall(f"{ns}si"):
        texts = [node.text or "" for node in item.findall(f".//{ns}t")]
        out.append("".join(texts))
    return out


def _sheet_path(zf: zipfile.ZipFile, sheet_name: str) -> str | None:
    ns = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "pkg": "http://schemas.openxmlformats.org/package/2006/relationships",
    }
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rel_id = None
    for sheet in workbook.findall(".//main:sheet", ns):
        if sheet.attrib.get("name") == sheet_name:
            rel_id = sheet.attrib.get(f"{{{ns['rel']}}}id")
            break
    if not rel_id:
        return None
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.findall("pkg:Relationship", ns):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib.get("Target", "")
            normalized = target.lstrip("/")
            if normalized.startswith("xl/"):
                return normalized
            return "xl/" + normalized
    return None


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _col(index: int) -> str:
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def _col_to_index(col: str) -> int:
    value = 0
    for char in col:
        value = value * 26 + (ord(char.upper()) - 64)
    return value
