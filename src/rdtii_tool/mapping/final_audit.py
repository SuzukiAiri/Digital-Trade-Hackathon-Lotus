"""Terminal deterministic finalization and GPT-5.6 global audit.

This module is deliberately terminal-only: it reads completed Zone 2 outputs,
normalises them into canonical rows, performs deterministic deduplication,
optionally asks GPT-5.6 for executable anomaly actions, applies those actions,
and writes ``final_rows.jsonl`` as the single authority for submission export.
It does not call Zone 1, Docling, Mapper, PDF Mapper or the original Reviewer.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .discovery_tags import load_legal_inventory_registry
from .indicator_specs import INDICATOR_SPEC_VERSION, INDICATOR_SPECS, P4_INDICATOR_SPEC_VERSION
from .indicator_registry import indicator_sort_key
from .model_config import openai_client
from .submission_exporter import SUBMISSION_COLUMNS, _assert_submission_outputs_match, _write_workbook, _write_xlsx
from .submission_rationale import sanitize_submission_row, validate_submission_rationale


FINAL_AUDIT_MODEL = "gpt-5.6"
FINAL_AUDIT_REASONING_EFFORT = "high"
FINAL_AUDIT_PROMPT_VERSION = "rdtii-final-auditor-v2-global-actions"
P4_FINAL_AUDIT_PROMPT_VERSION = "rdtii-p4-final-auditor-v2-boundary-actions"
FINAL_AUDIT_MODES = {"off", "cache_only", "live"}
GLOBAL_AUDIT_CHAR_BUDGET = 55_000


class FinalAuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CorrectedField(FinalAuditModel):
    name: Literal[
        "Economy",
        "Law Name",
        "Law Number / Ref",
        "Last Amended",
        "Indicator ID",
        "Article / Section",
        "Location Reference",
        "Verbatim Snippet",
        "Mapping Rationale",
        "Source URL",
        "Confidence",
        "Notes",
    ]
    value: str


class FinalAuditAction(FinalAuditModel):
    target_audit_key: str
    target_review_key: str | None = None
    target_evidence_hash: str | None = None
    target_stable_identity: str | None = None
    action: Literal["reject", "reclassify", "merge", "repair_fields", "repair_citation", "promote_near_miss", "human_review"]
    reason: str
    corrected_indicator_id: str | None = None
    merge_into_audit_key: str | None = None
    merge_into_review_key: str | None = None
    merge_into_stable_identity: str | None = None
    corrected_fields: list[CorrectedField] = Field(default_factory=list)
    replacement_quote: str | None = None
    source_quote: str | None = None
    source_reference: str | None = None
    requires_full_source_context: bool = False


class FinalAuditActionBatch(FinalAuditModel):
    actions: list[FinalAuditAction] = Field(default_factory=list)


def run_final_audit(project_root: Path, economy: str, pillars: set[int]) -> dict:
    if pillars not in ({4}, {6, 7}):
        raise RuntimeError("unsupported pillar combination")
    mode = os.environ.get("RDTII_FINAL_AUDIT_MODE", "cache_only").strip().casefold() or "cache_only"
    if mode not in FINAL_AUDIT_MODES:
        raise RuntimeError(f"Unsupported RDTII_FINAL_AUDIT_MODE={mode!r}; expected off, cache_only, or live")

    scope_slug = "p4" if pillars == {4} else "p6_p7"
    submission_dir = Path(project_root) / "outputs" / "corpus" / economy / "submission"
    if pillars == {4}:
        submission_dir = submission_dir / "p4"
    human_decisions = _load_human_decisions(submission_dir / "human_decisions.jsonl")
    source_rows, canonical_rows = _load_prefinal_rows(
        submission_dir, economy, human_decisions, scope_slug=scope_slug
    )
    registry = load_legal_inventory_registry(project_root)
    review_rows = _load_jsonl(submission_dir / "human_review.jsonl")
    framework_conclusions = _load_framework_conclusions(
        submission_dir / "framework_conclusions.json"
    )
    if pillars == {4}:
        review_rows.extend(
            _p4_deterministic_review_rows(canonical_rows, framework_conclusions)
        )
    deduped_rows, duplicate_groups = _deduplicate_rows(canonical_rows)
    dataset_hash = _dataset_hash(
        deduped_rows, review_rows, human_decisions, pillars=pillars,
        framework_conclusions=framework_conclusions,
    )
    audit_requests = _build_global_audit_requests(
        economy,
        deduped_rows,
        review_rows,
        duplicate_groups,
        dataset_hash,
        pillars=pillars,
        framework_conclusions=framework_conclusions,
    )

    action_cache_path = submission_dir / "final_audit_actions.jsonl"
    action_cache = _load_action_cache(action_cache_path)
    cache_hits = sum(1 for request in audit_requests if request["cache_key"] in action_cache)
    misses = [request for request in audit_requests if request["cache_key"] not in action_cache]

    if mode == "cache_only" and misses:
        summary = _summary(
            mode=mode,
            human_decisions=human_decisions,
            source_rows=source_rows,
            canonical_rows=canonical_rows,
            deduped_rows=deduped_rows,
            duplicate_groups=duplicate_groups,
            audit_requests=audit_requests,
            cache_hits=cache_hits,
            cache_misses=len(misses),
            actions=[],
            final_rows=[],
            human_review_rows=[],
            dataset_hash=dataset_hash,
        )
        _write_summary(submission_dir, summary)
        raise RuntimeError(
            f"final-audit cache_only mode has {len(misses)} missing GPT-5.6 global audit chunks. "
            "Set RDTII_FINAL_AUDIT_MODE=live to audit them."
        )

    if mode == "live" and misses:
        _run_live_global_audit(misses, action_cache, action_cache_path)

    cached_actions = _actions_for_requests(audit_requests, action_cache)
    actions = [] if mode == "off" else cached_actions
    final_rows, final_human_review, application_report = _apply_actions(deduped_rows, review_rows, actions, return_report=True)
    final_rows = [_assign_discovery_tag(row, registry) for row in final_rows]
    final_rows = [_sanitize_final_row(row) for row in final_rows]
    _write_final_rows(submission_dir / "final_rows.jsonl", final_rows)
    _write_action_application(submission_dir / "final_audit_action_application.jsonl", application_report)
    _write_country_submission(submission_dir, economy, final_rows, scope_slug=scope_slug)
    _write_human_review_outputs(submission_dir, final_human_review)
    summary = _summary(
        mode=mode,
        human_decisions=human_decisions,
        source_rows=source_rows,
        canonical_rows=canonical_rows,
        deduped_rows=deduped_rows,
        duplicate_groups=duplicate_groups,
        audit_requests=audit_requests,
        cache_hits=cache_hits,
        cache_misses=0 if mode in {"off", "live"} else len(misses),
        actions=actions,
        final_rows=final_rows,
        human_review_rows=final_human_review,
        dataset_hash=dataset_hash,
    )
    _write_summary(submission_dir, summary)
    return summary


def _build_canonical_rows(economy: str, rows: list[dict[str, str]], human_decisions: dict[str, dict]) -> list[dict]:
    out: list[dict] = []
    protected_keys = set(human_decisions)
    for index, row in enumerate(rows, start=1):
        clean = {column: str(row.get(column) or "") for column in SUBMISSION_COLUMNS}
        audit_key = _audit_key(clean)
        review_key = str(row.get("review_key") or row.get("Review Key") or "").strip()
        human_decision = human_decisions.get(review_key) if review_key else None
        if human_decision and str(human_decision.get("decision")).casefold() == "reject":
            continue
        clean["Economy"] = clean["Economy"] or _economy_title(economy)
        out.append(
            {
                "audit_key": audit_key,
                "row": clean,
                "source": "submission",
                "source_index": index,
                "review_key": review_key,
                "evidence_hash": _row_evidence_hash(clean),
                "stable_identity": _stable_identity(clean, review_key),
                "human_protected": bool(review_key and review_key in protected_keys),
                "decision_source": "human" if human_decision else "existing_pipeline",
                "citation_status": "verified",
                "provenance": [f"submission:{index}"],
                "merged_from": [],
            }
        )
    return out


def _load_prefinal_rows(
    submission_dir: Path,
    economy: str,
    human_decisions: dict[str, dict],
    *,
    scope_slug: str = "p6_p7",
) -> tuple[list[dict[str, str]], list[dict]]:
    final_rows_path = submission_dir / "final_rows.jsonl"
    if final_rows_path.exists():
        canonical_rows = _load_canonical_final_rows(final_rows_path, human_decisions)
        if not canonical_rows:
            raise RuntimeError(f"final_rows.jsonl has no rows; final-audit will not audit empty input: {final_rows_path}")
        return [row["row"] for row in canonical_rows], canonical_rows
    source_path = submission_dir / f"{economy}_{scope_slug}.json"
    if not source_path.exists():
        raise RuntimeError(
            f"prefinal submission input missing; run the mapping/export pipeline before final-audit: {source_path}"
        )
    source_rows = _load_submission_rows(source_path)
    if not source_rows:
        raise RuntimeError(f"prefinal submission input has no rows; final-audit will not audit empty input: {source_path}")
    return source_rows, _build_canonical_rows(economy, source_rows, human_decisions)


def _load_canonical_final_rows(path: Path, human_decisions: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        row = payload.get("row") if isinstance(payload, dict) else None
        if not isinstance(row, dict):
            raise RuntimeError(f"final_rows.jsonl row {line_no} missing row object: {path}")
        clean = {column: str(row.get(column) or "") for column in SUBMISSION_COLUMNS}
        review_key = str(payload.get("review_key") or "").strip()
        human_decision = human_decisions.get(review_key) if review_key else None
        if human_decision and str(human_decision.get("decision")).casefold() == "reject":
            continue
        payload = dict(payload)
        payload["row"] = clean
        payload["audit_key"] = str(payload.get("audit_key") or _audit_key(clean))
        payload["review_key"] = review_key
        payload["evidence_hash"] = str(payload.get("evidence_hash") or _row_evidence_hash(clean))
        payload["stable_identity"] = str(payload.get("stable_identity") or _stable_identity(clean, review_key))
        payload["human_protected"] = bool(human_decision and str(human_decision.get("decision")).casefold() == "accept")
        if payload["human_protected"]:
            payload["decision_source"] = "human"
            payload["row"]["Confidence"] = "1.00"
        else:
            payload["decision_source"] = str(payload.get("decision_source") or "existing_pipeline")
        payload["citation_status"] = str(payload.get("citation_status") or "verified")
        payload["provenance"] = list(payload.get("provenance") or [])
        payload["merged_from"] = list(payload.get("merged_from") or [])
        rows.append(payload)
    return rows


def _deduplicate_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    seen: dict[str, dict] = {}
    groups: list[dict] = []
    for row in rows:
        key = _dedup_key(row["row"])
        existing = seen.get(key)
        if existing is None:
            seen[key] = row
            continue
        existing["provenance"].extend(row.get("provenance", []))
        existing["merged_from"].append(row["audit_key"])
        existing["row"]["Notes"] = _append_note(existing["row"].get("Notes", ""), f"merged duplicate {row['audit_key']}")
        groups.append({"winner": existing["audit_key"], "removed": row["audit_key"], "dedup_key": key, "reason": "same_stable_key"})
    return list(seen.values()), groups


def _build_global_audit_requests(
    economy: str,
    rows: list[dict],
    review_rows: list[dict],
    duplicate_groups: list[dict],
    dataset_hash: str,
    *,
    pillars: set[int] | None = None,
    framework_conclusions: list[dict] | None = None,
) -> list[dict]:
    requests: list[dict] = []
    scopes = (
        (("pillar_4_global", rows),)
        if pillars == {4}
        else (
            ("pillar_6", [row for row in rows if row["row"].get("Indicator ID", "").startswith("P6-")]),
            ("pillar_7", [row for row in rows if row["row"].get("Indicator ID", "").startswith("P7-")]),
            ("cross_pillar_consistency", rows),
        )
    )
    framework_indicator_set = (
        {"P4-I2", "P4-I5", "P4-I6", "P4-I10"}
        if pillars == {4}
        else {"P7-I1", "P7-I2"}
    )
    for scope, scoped_rows in scopes:
        chunks = _chunk_rows(scoped_rows, GLOBAL_AUDIT_CHAR_BUDGET)
        for index, chunk in enumerate(chunks, start=1):
            cache_key = _global_cache_key(economy=economy, scope=scope, chunk_index=index, dataset_hash=dataset_hash)
            requests.append(
                {
                    "economy": economy,
                    "scope": scope,
                    "chunk_index": index,
                    "cache_key": cache_key,
                    "dataset_hash": dataset_hash,
                    "rows": chunk,
                    "all_index": _compact_index(scoped_rows),
                    "indicator_counts": _indicator_counts(scoped_rows),
                    "law_article_distribution": _law_article_distribution(scoped_rows),
                    "framework_rows": [
                        row["audit_key"]
                        for row in scoped_rows
                        if row["row"].get("Indicator ID") in framework_indicator_set
                    ],
                    "framework_conclusions": framework_conclusions or [],
                    "duplicate_groups": duplicate_groups,
                    "human_review_count": len(review_rows),
                    "citation_status": {"verified": len(scoped_rows)},
                }
            )
    return requests


def _run_live_global_audit(misses: list[dict], cache: dict[str, list[dict]], cache_path: Path) -> None:
    client = openai_client()
    for request in misses:
        response = client.responses.parse(
            model=FINAL_AUDIT_MODEL,
            input=_build_global_prompt(request),
            reasoning={"effort": FINAL_AUDIT_REASONING_EFFORT},
            text_format=FinalAuditActionBatch,
        )
        actions = [action.model_dump() for action in response.output_parsed.actions]
        payload = {
            "cache_key": request["cache_key"],
            "model": FINAL_AUDIT_MODEL,
            "reasoning_effort": FINAL_AUDIT_REASONING_EFFORT,
            "prompt_version": _audit_prompt_version(request["scope"]),
            "indicator_spec_version": _audit_spec_version(request["scope"]),
            "dataset_hash": request["dataset_hash"],
            "scope": request["scope"],
            "chunk_index": request["chunk_index"],
            "actions": actions,
        }
        cache[request["cache_key"]] = actions
        _append_jsonl(cache_path, payload)


def _build_global_prompt(request: dict) -> str:
    if request["scope"] == "pillar_4_global":
        return _build_p4_global_prompt(request)
    payload = {
        "instructions": (
            "You are the terminal RDTII P6/P7 global auditor. Return only executable actions for anomalies. "
            "Do not return accept/keep actions. Unmentioned rows are retained by default. Use only supplied indicator contracts and row evidence."
        ),
        "allowed_actions": ["reject", "reclassify", "merge", "repair_fields", "repair_citation", "promote_near_miss", "human_review"],
        "model": FINAL_AUDIT_MODEL,
        "prompt_version": FINAL_AUDIT_PROMPT_VERSION,
        "indicator_spec_version": INDICATOR_SPEC_VERSION,
        "economy": request["economy"],
        "scope": request["scope"],
        "all_records_compact_index": request["all_index"],
        "indicator_counts": request["indicator_counts"],
        "law_article_distribution": request["law_article_distribution"],
        "framework_rows": request["framework_rows"],
        "duplicate_groups": request["duplicate_groups"],
        "human_review_count": request["human_review_count"],
        "citation_status": request["citation_status"],
        "indicator_contracts": {
            indicator: INDICATOR_SPECS[indicator].__dict__
            for indicator in sorted({row["row"].get("Indicator ID", "") for row in request["rows"]})
            if indicator in INDICATOR_SPECS
        },
        "detailed_rows": [
            {
                "audit_key": row["audit_key"],
                "stable_identity": row.get("stable_identity", ""),
                "evidence_hash": row.get("evidence_hash", ""),
                "review_key": row.get("review_key", ""),
                "decision_source": row["decision_source"],
                "human_protected": row["human_protected"],
                "row": row["row"],
            }
            for row in request["rows"]
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _build_p4_global_prompt(request: dict) -> str:
    payload = {
        "instructions": (
            "You are the terminal RDTII Pillar 4 global auditor. Return only executable "
            "actions for anomalies; unmentioned rows are retained. Use only supplied "
            "contracts, status records, framework conclusions, and row evidence. Check "
            "P4 indicator boundaries, duplicate principal/amending-law evidence, exact "
            "citation fidelity, numeric or polluted law names, rationale/evidence "
            "conflicts, and framework element completeness. Specifically check whether "
            "P4-I1 omitted an operative local-representative, local address-for-service, "
            "first-filing, substantive-examination, or material-cost burden; whether "
            "P4-I2 contains a limitation, defence, immunity, no-injunction rule, or "
            "damages limitation instead of a positive remedy; whether P4-I3 treats an "
            "ordinary safeguarded exception as a material patent restriction; whether "
            "P4-I5 correctly separates operative copyright rights from explicit "
            "exceptions and excludes scope/procedure/inspection-only text; whether "
            "P4-I6 omitted an explicit copyright website-blocking/access-disabling "
            "remedy or accepted a general/off-topic remedy without an online copyright "
            "nexus; whether P4-I9 identifies a protected-information holder compelled "
            "to disclose to a court/authority rather than a public officer disclosing "
            "information, and accounts for safeguards; and whether P4-I10 uses narrow "
            "sectoral or government secrecy as a complete framework or overstates "
            "common-law protection without evidence, including P4-I10 common-law uncertainty. Check duplicate supporting rows "
            "within the same law and consistency between every framework conclusion "
            "and its element evidence. Do not infer missing law from no candidate."
        ),
        "allowed_actions": [
            "reject",
            "reclassify",
            "merge",
            "repair_fields",
            "repair_citation",
            "promote_near_miss",
            "human_review",
        ],
        "model": FINAL_AUDIT_MODEL,
        "prompt_version": P4_FINAL_AUDIT_PROMPT_VERSION,
        "indicator_spec_version": P4_INDICATOR_SPEC_VERSION,
        "economy": request["economy"],
        "scope": request["scope"],
        "all_records_compact_index": request["all_index"],
        "indicator_counts": request["indicator_counts"],
        "law_article_distribution": request["law_article_distribution"],
        "framework_rows": request["framework_rows"],
        "framework_conclusions": request.get("framework_conclusions", []),
        "duplicate_groups": request["duplicate_groups"],
        "human_review_count": request["human_review_count"],
        "citation_status": request["citation_status"],
        "indicator_contracts": {
            indicator: INDICATOR_SPECS[indicator].__dict__
            for indicator in sorted(
                {row["row"].get("Indicator ID", "") for row in request["rows"]},
                key=indicator_sort_key,
            )
            if indicator in INDICATOR_SPECS and indicator.startswith("P4-")
        },
        "detailed_rows": [
            {
                "audit_key": row["audit_key"],
                "stable_identity": row.get("stable_identity", ""),
                "evidence_hash": row.get("evidence_hash", ""),
                "review_key": row.get("review_key", ""),
                "decision_source": row["decision_source"],
                "human_protected": row["human_protected"],
                "row": row["row"],
            }
            for row in request["rows"]
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _audit_prompt_version(scope: str) -> str:
    return P4_FINAL_AUDIT_PROMPT_VERSION if scope == "pillar_4_global" else FINAL_AUDIT_PROMPT_VERSION


def _audit_spec_version(scope: str) -> str:
    return P4_INDICATOR_SPEC_VERSION if scope == "pillar_4_global" else INDICATOR_SPEC_VERSION


def _apply_actions(rows: list[dict], review_rows: list[dict], actions: list[dict], *, return_report: bool = False):
    by_key = {row["audit_key"]: _canonical_runtime_row(row) for row in rows}
    identity_index = _build_identity_index(by_key.values())
    unresolved_review = _filter_unresolved_human_review(review_rows, by_key)
    final_human_review: list[dict] = list(unresolved_review)
    application_report: list[dict] = []
    resolved: dict[str, dict] = {}
    applied_signatures: set[str] = set()

    for action_index, action in enumerate(actions, start=1):
        action_type = str(action.get("action") or "").strip()
        signature = _action_signature(action)
        if signature in applied_signatures:
            _record_application(application_report, action_index, action, "already_applied")
            continue
        applied_signatures.add(signature)

        if action_type == "promote_near_miss":
            promoted = _promote_near_miss(action, unresolved_review)
            if promoted:
                promoted = _canonical_runtime_row(promoted)
                by_key[promoted["audit_key"]] = promoted
                final_human_review = _remove_human_review_target(final_human_review, action)
                identity_index = _build_identity_index(by_key.values())
                _record_application(application_report, action_index, action, "applied")
            else:
                _record_application(application_report, action_index, action, "warning", "promote_near_miss_not_verifiable")
            continue

        row, target_status = _resolve_action_target(action, by_key, identity_index, resolved)
        if row is None:
            _record_application(application_report, action_index, action, target_status)
            continue
        if row.get("human_protected"):
            _record_application(application_report, action_index, action, "already_resolved", "human_protected")
            continue

        target_key = row["audit_key"]
        if action_type == "reject":
            by_key.pop(target_key, None)
            _mark_resolved(resolved, row, "rejected", action)
            identity_index = _build_identity_index(by_key.values())
            _record_application(application_report, action_index, action, "applied")
        elif action_type == "human_review":
            if _metadata_only_review_action(action):
                _record_application(application_report, action_index, action, "warning", "metadata_completion_required")
                continue
            review = _human_review_from_row(row, action)
            if _valid_human_review_record(review):
                final_human_review.append(review)
            by_key.pop(target_key, None)
            _mark_resolved(resolved, row, "human_review", action)
            identity_index = _build_identity_index(by_key.values())
            _record_application(application_report, action_index, action, "applied")
        elif action_type == "merge":
            winner, winner_status = _resolve_merge_target(action, by_key, identity_index, resolved, source_key=target_key)
            if winner is not None:
                winner["provenance"].extend(row.get("provenance", []))
                if target_key not in winner["merged_from"]:
                    winner["merged_from"].append(target_key)
                winner["row"]["Notes"] = _append_note(winner["row"].get("Notes", ""), f"merged by final audit {target_key}")
                by_key.pop(target_key, None)
                _mark_resolved(resolved, row, "merged", action, destination=winner["audit_key"])
                identity_index = _build_identity_index(by_key.values())
                _record_application(application_report, action_index, action, "applied")
            else:
                _record_application(application_report, action_index, action, winner_status, "merge_target_not_found")
        elif action_type == "reclassify":
            corrected = str(action.get("corrected_indicator_id") or "").strip()
            if corrected not in INDICATOR_SPECS:
                review = _human_review_from_row(row, action, reason="invalid_corrected_indicator")
                if _valid_human_review_record(review):
                    final_human_review.append(review)
                by_key.pop(target_key, None)
                _mark_resolved(resolved, row, "human_review", action)
                identity_index = _build_identity_index(by_key.values())
                _record_application(application_report, action_index, action, "applied", "invalid_corrected_indicator")
            else:
                row["row"]["Indicator ID"] = corrected
                _refresh_row_identity(row)
                if not _row_valid(row["row"]):
                    review = _human_review_from_row(row, action, reason="reclassified_row_failed_validation")
                    if _valid_human_review_record(review):
                        final_human_review.append(review)
                    by_key.pop(target_key, None)
                    _mark_resolved(resolved, row, "human_review", action)
                identity_index = _build_identity_index(by_key.values())
                _record_application(application_report, action_index, action, "applied")
        elif action_type == "repair_fields":
            if _corrected_fields_already_present(row["row"], action.get("corrected_fields") or []):
                _record_application(application_report, action_index, action, "already_applied")
                continue
            _apply_corrected_fields(row["row"], action.get("corrected_fields") or [])
            _refresh_row_identity(row)
            if not _row_valid(row["row"]):
                review = _human_review_from_row(row, action, reason="repaired_row_failed_validation")
                if _valid_human_review_record(review):
                    final_human_review.append(review)
                by_key.pop(target_key, None)
                _mark_resolved(resolved, row, "human_review", action)
            identity_index = _build_identity_index(by_key.values())
            _record_application(application_report, action_index, action, "applied")
        elif action_type == "repair_citation":
            if _citation_repair_already_present(row["row"], action):
                _record_application(application_report, action_index, action, "already_applied")
                continue
            if _apply_citation_repair(row["row"], action):
                _refresh_row_identity(row)
                if not _row_valid(row["row"]):
                    review = _human_review_from_row(row, action, reason="citation_repair_failed_validation")
                    if _valid_human_review_record(review):
                        final_human_review.append(review)
                    by_key.pop(target_key, None)
                    _mark_resolved(resolved, row, "human_review", action)
                identity_index = _build_identity_index(by_key.values())
                _record_application(application_report, action_index, action, "applied")
            else:
                review = _human_review_from_row(row, action, reason="citation_repair_not_verified")
                if _valid_human_review_record(review):
                    final_human_review.append(review)
                by_key.pop(target_key, None)
                _mark_resolved(resolved, row, "citation_hold", action)
                identity_index = _build_identity_index(by_key.values())
                _record_application(application_report, action_index, action, "applied", "citation_repair_not_verified")
        else:
            _record_application(application_report, action_index, action, "warning", "unknown_action")

    final_rows = [_sanitize_final_row(row) for row in by_key.values() if _row_valid(row["row"])]
    final_rows.sort(
        key=lambda row: (
            row["row"].get("Economy", ""),
            indicator_sort_key(row["row"].get("Indicator ID", "")),
            row["row"].get("Law Name", ""),
            row["row"].get("Article / Section", ""),
        )
    )
    final_human_review = _dedupe_human_review_rows(_filter_current_human_review(final_human_review, by_key))
    if return_report:
        return final_rows, final_human_review, application_report
    return final_rows, final_human_review


def _apply_corrected_fields(row: dict[str, str], fields: list[dict]) -> None:
    for item in fields:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if name in SUBMISSION_COLUMNS and name != "Discovery Tag":
            row[name] = value


def _corrected_fields_already_present(row: dict[str, str], fields: list[dict]) -> bool:
    normalized_fields = [
        (str(item.get("name") or "").strip(), str(item.get("value") or ""))
        for item in fields
        if str(item.get("name") or "").strip() in SUBMISSION_COLUMNS and str(item.get("name") or "").strip() != "Discovery Tag"
    ]
    return bool(normalized_fields) and all(str(row.get(name) or "") == value for name, value in normalized_fields)


def _citation_repair_already_present(row: dict[str, str], action: dict) -> bool:
    replacement = str(action.get("replacement_quote") or "").strip()
    if replacement and _norm(row.get("Verbatim Snippet", "")) != _norm(replacement):
        return False
    corrected_article = ""
    for field in action.get("corrected_fields") or []:
        if field.get("name") == "Article / Section":
            corrected_article = str(field.get("value") or "").strip()
    if corrected_article and str(row.get("Article / Section") or "") != corrected_article:
        return False
    return bool(replacement or corrected_article)


def _canonical_runtime_row(row: dict) -> dict:
    out = dict(row)
    clean = {column: str(out.get("row", {}).get(column) or "") for column in SUBMISSION_COLUMNS}
    out["row"] = clean
    out["audit_key"] = str(out.get("audit_key") or _audit_key(clean))
    out["review_key"] = str(out.get("review_key") or "").strip()
    out["evidence_hash"] = str(out.get("evidence_hash") or _row_evidence_hash(clean))
    out["stable_identity"] = str(out.get("stable_identity") or _stable_identity(clean, out["review_key"]))
    out["provenance"] = list(out.get("provenance") or [])
    out["merged_from"] = list(out.get("merged_from") or [])
    return out


def _sanitize_final_row(row: dict) -> dict:
    out = _canonical_runtime_row(row)
    out["row"] = sanitize_submission_row(out["row"])
    _refresh_row_identity(out, preserve_audit_key=True)
    return out


def _refresh_row_identity(row: dict, *, preserve_audit_key: bool = False) -> None:
    clean = {column: str(row.get("row", {}).get(column) or "") for column in SUBMISSION_COLUMNS}
    row["row"] = clean
    if not preserve_audit_key:
        row["audit_key"] = _audit_key(clean)
    row["evidence_hash"] = _row_evidence_hash(clean)
    row["stable_identity"] = _stable_identity(clean, str(row.get("review_key") or ""))


def _build_identity_index(rows) -> dict[str, str]:
    index: dict[str, str] = {}
    for row in rows:
        for value in _row_identity_values(row):
            if value:
                index.setdefault(value, row["audit_key"])
    return index


def _row_identity_values(row: dict) -> set[str]:
    values = {
        str(row.get("audit_key") or "").strip(),
        str(row.get("review_key") or "").strip(),
        str(row.get("evidence_hash") or "").strip(),
        str(row.get("stable_identity") or "").strip(),
    }
    values.update(str(item or "").strip() for item in row.get("merged_from") or [])
    return {value for value in values if value}


def _action_identity_values(action: dict) -> set[str]:
    values = {
        str(action.get("target_audit_key") or "").strip(),
        str(action.get("target_review_key") or "").strip(),
        str(action.get("target_evidence_hash") or "").strip(),
        str(action.get("target_stable_identity") or "").strip(),
        str(action.get("source_reference") or "").strip(),
    }
    return {value for value in values if value}


def _resolve_action_target(action: dict, by_key: dict[str, dict], identity_index: dict[str, str], resolved: dict[str, dict]) -> tuple[dict | None, str]:
    for value in _action_identity_values(action):
        if value in resolved:
            return None, "already_resolved"
        if value in by_key:
            return by_key[value], "active"
        key = identity_index.get(value)
        if key and key in by_key:
            return by_key[key], "active"
    return None, "warning"


def _resolve_merge_target(
    action: dict,
    by_key: dict[str, dict],
    identity_index: dict[str, str],
    resolved: dict[str, dict],
    *,
    source_key: str,
) -> tuple[dict | None, str]:
    values = {
        str(action.get("merge_into_audit_key") or "").strip(),
        str(action.get("merge_into_stable_identity") or "").strip(),
        str(action.get("merge_into_review_key") or "").strip(),
    }
    for value in {item for item in values if item}:
        if value == source_key:
            return None, "already_applied"
        if value in by_key:
            return by_key[value], "active"
        key = identity_index.get(value)
        if key and key in by_key and key != source_key:
            return by_key[key], "active"
        if value in resolved:
            return None, "already_resolved"
    return None, "warning"


def _mark_resolved(resolved: dict[str, dict], row: dict, status: str, action: dict, *, destination: str = "") -> None:
    payload = {
        "status": status,
        "destination": destination,
        "action": action.get("action", ""),
        "reason": action.get("reason", ""),
    }
    for value in _row_identity_values(row):
        resolved[value] = payload


def _record_application(report: list[dict], action_index: int, action: dict, status: str, detail: str = "") -> None:
    if status == "warning" and not detail:
        detail = "target_not_found"
    report.append(
        {
            "action_index": action_index,
            "target_audit_key": str(action.get("target_audit_key") or ""),
            "action": str(action.get("action") or ""),
            "status": status,
            "detail": detail,
            "reason": str(action.get("reason") or ""),
        }
    )


def _action_signature(action: dict) -> str:
    payload = json.dumps(action, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _filter_unresolved_human_review(rows: list[dict], by_key: dict[str, dict]) -> list[dict]:
    active_identities = _build_identity_index(by_key.values())
    out: list[dict] = []
    for row in rows:
        reason = str(row.get("reason") or row.get("status") or row.get("current_status") or "")
        if _non_public_review_reason(reason):
            continue
        identities = {
            str(row.get("audit_key") or ""),
            str(row.get("review_key") or ""),
            str(row.get("human_review_id") or ""),
            str(row.get("task_id") or ""),
            str(row.get("claim_id") or ""),
        }
        if any(value and (value in active_identities or value in by_key) for value in identities):
            continue
        if _valid_human_review_record(row):
            out.append(row)
    return _dedupe_human_review_rows(out)


def _filter_current_human_review(rows: list[dict], by_key: dict[str, dict]) -> list[dict]:
    active = _build_identity_index(by_key.values())
    out: list[dict] = []
    for row in rows:
        reason = str(row.get("reason") or row.get("status") or row.get("current_status") or "")
        if _non_public_review_reason(reason):
            continue
        identities = {
            str(row.get("audit_key") or ""),
            str(row.get("review_key") or ""),
            str(row.get("human_review_id") or ""),
            str(row.get("task_id") or ""),
            str(row.get("claim_id") or ""),
        }
        if any(value and (value in active or value in by_key) for value in identities) and reason not in {"citation_repair_not_verified", "citation_hold"}:
            continue
        if _valid_human_review_record(row):
            out.append(row)
    return out


def _dedupe_human_review_rows(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        key = str(row.get("review_key") or row.get("audit_key") or row.get("human_review_id") or row.get("task_id") or "")
        if not key:
            key = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _remove_human_review_target(rows: list[dict], action: dict) -> list[dict]:
    targets = _action_identity_values(action)
    if not targets:
        return rows
    out: list[dict] = []
    for row in rows:
        identities = {
            str(row.get("audit_key") or ""),
            str(row.get("review_key") or ""),
            str(row.get("human_review_id") or ""),
            str(row.get("task_id") or ""),
            str(row.get("claim_id") or ""),
        }
        if identities & targets:
            continue
        out.append(row)
    return out


def _non_public_review_reason(reason: str) -> bool:
    text = str(reason or "").casefold()
    return any(
        token in text
        for token in (
            "target_not_found",
            "already_resolved",
            "already_applied",
            "duplicate action",
            "action_order_conflict",
            "merge_target_not_found",
        )
    )


def _metadata_only_review_action(action: dict) -> bool:
    if str(action.get("action") or "") != "human_review":
        return False
    text = " ".join(str(action.get(key) or "") for key in ("reason", "reason_code")).casefold()
    metadata_terms = ("law name", "law number", "last amended", "metadata", "statutory title", "act number")
    legal_terms = (
        "citation",
        "quote",
        "quotation",
        "focal",
        "indicator",
        "required element",
        "operative",
        "retention",
        "storage",
        "transfer",
        "access",
        "authority",
        "framework",
    )
    return any(term in text for term in metadata_terms) and not any(term in text for term in legal_terms)


def _apply_citation_repair(row: dict[str, str], action: dict) -> bool:
    replacement = str(action.get("replacement_quote") or "").strip()
    source_quote = str(action.get("source_quote") or "").strip()
    corrected_article = ""
    for field in action.get("corrected_fields") or []:
        if field.get("name") == "Article / Section":
            corrected_article = str(field.get("value") or "").strip()
    if not replacement:
        return False
    if source_quote and _norm(replacement) not in _norm(source_quote):
        return False
    row["Verbatim Snippet"] = replacement
    if corrected_article:
        row["Article / Section"] = corrected_article
    return True


def _promote_near_miss(action: dict, review_rows: list[dict]) -> dict | None:
    target = str(action.get("target_audit_key") or "").strip()
    for row in review_rows:
        keys = {
            str(row.get("audit_key") or ""),
            str(row.get("review_key") or ""),
            str(row.get("human_review_id") or ""),
            str(row.get("task_id") or ""),
        }
        if target not in keys:
            continue
        citation_status = str(row.get("citation_status") or "").strip()
        if citation_status and citation_status != "verified":
            return None
        submission_row = {column: "" for column in SUBMISSION_COLUMNS}
        submission_row.update(
            {
                "Economy": str(row.get("economy") or ""),
                "Law Name": str(row.get("law_title") or row.get("Law Name") or ""),
                "Law Number / Ref": str(row.get("law_number_ref") or ""),
                "Last Amended": str(row.get("last_amended") or ""),
                "Indicator ID": str(action.get("corrected_indicator_id") or row.get("indicator") or row.get("indicator_id") or ""),
                "Article / Section": str(row.get("article") or row.get("focal_provision_id") or ""),
                "Location Reference": str(row.get("location_reference") or ""),
                "Verbatim Snippet": str(action.get("replacement_quote") or row.get("focal_quote") or row.get("verbatim_snippet") or ""),
                "Mapping Rationale": str(action.get("reason") or row.get("rationale") or ""),
                "Source URL": str(row.get("source_url") or ""),
                "Confidence": "0.90",
                "Notes": str(row.get("notes") or ""),
            }
        )
        if not _row_valid(submission_row):
            return None
        return {
            "audit_key": _audit_key(submission_row),
            "row": submission_row,
            "source": "promoted_near_miss",
            "source_index": 0,
            "review_key": str(row.get("review_key") or ""),
            "evidence_hash": _row_evidence_hash(submission_row),
            "stable_identity": _stable_identity(submission_row, str(row.get("review_key") or "")),
            "human_protected": False,
            "decision_source": "gpt56_global_audit",
            "citation_status": "verified",
            "provenance": [f"promoted:{target}"],
            "merged_from": [],
        }
    return None


def _assign_discovery_tag(row: dict, registry) -> dict:
    out = dict(row)
    clean = dict(out["row"])
    match = registry.match_row(clean)
    clean["Discovery Tag"] = match.discovery_tag
    out["row"] = clean
    out["baseline_match_key"] = match.baseline_match_key
    out["baseline_match_basis"] = match.baseline_match_basis
    out["baseline_row_id"] = match.baseline_row_id
    out["baseline_file_hash"] = match.baseline_file_hash
    return out


def _write_final_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _write_action_application(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _write_country_submission(
    submission_dir: Path,
    economy: str,
    rows: list[dict],
    *,
    scope_slug: str = "p6_p7",
) -> None:
    submission_rows = [sanitize_submission_row(row["row"]) for row in rows]
    _write_outputs(submission_dir, f"{economy}_{scope_slug}", submission_rows)


def _write_outputs(output_dir: Path, prefix: str, rows: list[dict[str, str]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [sanitize_submission_row(row) for row in rows]
    csv_path = output_dir / f"{prefix}.csv"
    json_path = output_dir / f"{prefix}.json"
    xlsx_path = output_dir / f"{prefix}.xlsx"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUBMISSION_COLUMNS)
        writer.writeheader()
        writer.writerows([{column: str(row.get(column) or "") for column in SUBMISSION_COLUMNS} for row in rows])
    json_path.write_text(json.dumps([{column: str(row.get(column) or "") for column in SUBMISSION_COLUMNS} for row in rows], ensure_ascii=False, indent=2), encoding="utf-8")
    _write_xlsx(xlsx_path, rows=rows, methodology_rows=_methodology_rows())
    _assert_submission_outputs_match(csv_path, json_path, xlsx_path)


def _write_human_review_outputs(submission_dir: Path, rows: list[dict]) -> None:
    jsonl_path = submission_dir / "human_review.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    csv_path = submission_dir / "human_review.csv"
    columns = sorted({key for row in rows for key in row}) or ["audit_key", "reason"]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([{key: _human_review_cell(row.get(key)) for key in columns} for row in rows])
    _write_workbook(
        submission_dir / "human_review.xlsx",
        [("Human Review", columns, [{key: _human_review_cell(row.get(key)) for key in columns} for row in rows])],
    )


def _human_review_cell(value) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value or "")


def _methodology_rows() -> list[dict[str, str]]:
    return [
        {"Topic": "Final authority", "Detail": "Output Data is generated only from final_rows.jsonl."},
        {"Topic": "Final audit", "Detail": "GPT-5.6 global audit returns executable anomaly actions only; unmentioned rows are retained by default."},
        {"Topic": "Discovery Tag", "Detail": "KNOWN = same economy/indicator/legal measure appears in the supplied Legal Inventory baseline. NEW = not present in that baseline."},
    ]


def _load_human_decisions(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    decisions: dict[str, dict] = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        key = str(row.get("review_key") or "").strip()
        decision = str(row.get("decision") or "").strip().casefold()
        if not key or decision not in {"accept", "reject"}:
            raise RuntimeError(f"Invalid human decision at {path}:{line_no}")
        existing = decisions.get(key)
        if existing and str(existing.get("decision")) != decision:
            raise RuntimeError(f"Conflicting human decision for review_key={key}: {path}:{line_no}")
        decisions[key] = row
    return decisions


def _load_submission_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError(f"Submission JSON must contain a list: {path}")
    return [dict(row) for row in data if isinstance(row, dict)]


def _load_framework_conclusions(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("conclusions") if isinstance(payload, dict) else None
    return [row for row in (rows or []) if isinstance(row, dict)]


def _p4_deterministic_review_rows(
    canonical_rows: list[dict],
    framework_conclusions: list[dict],
) -> list[dict]:
    reviews: list[dict] = []
    valid_indicators = {f"P4-I{index}" for index in range(1, 11)}
    for item in canonical_rows:
        row = item.get("row") or {}
        indicator = str(row.get("Indicator ID") or "")
        reason = ""
        if indicator not in valid_indicators:
            reason = "Invalid Pillar 4 indicator ID."
        elif str(row.get("Law Name") or "").strip().isdigit():
            reason = "Law Name is numeric and requires official-title metadata repair."
        elif indicator in {"P4-I4", "P4-I7", "P4-I8"}:
            notes = str(row.get("Notes") or "")
            if (
                str(row.get("Article / Section") or "") != "Treaty status"
                or "last_checked=" not in notes
                or not str(row.get("Source URL") or "").startswith("https://")
            ):
                reason = "Treaty status row is missing status reference, last-checked date, or official source."
        elif not all(
            str(row.get(field) or "").strip()
            for field in (
                "Article / Section",
                "Verbatim Snippet",
                "Mapping Rationale",
                "Source URL",
            )
        ):
            reason = "Provision evidence is missing a required citation or rationale field."
        if reason:
            reviews.append(_human_review_from_row(item, {"action": "human_review"}, reason))

    for conclusion in framework_conclusions:
        indicator = str(conclusion.get("indicator_id") or "")
        if (
            indicator in {"P4-I2", "P4-I5", "P4-I6", "P4-I10"}
            and conclusion.get("framework_status") == "absent"
            and not conclusion.get("coverage_sufficient_for_absence")
        ):
            reviews.append(
                {
                    "audit_key": f"framework:{indicator}",
                    "economy": canonical_rows[0]["row"].get("Economy", "") if canonical_rows else "",
                    "indicator_id": indicator,
                    "law_name": "Framework conclusion",
                    "article_section": "Framework conclusion",
                    "verbatim_quote": json.dumps(conclusion, ensure_ascii=False, sort_keys=True),
                    "source_url": "local:framework_conclusions.json",
                    "reason": "Framework absence lacks sufficient corpus/source coverage.",
                }
            )
    return reviews


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_action_cache(path: Path) -> dict[str, list[dict]]:
    cache: dict[str, list[dict]] = {}
    for row in _load_jsonl(path):
        key = str(row.get("cache_key") or "")
        if key:
            cache[key] = list(row.get("actions") or [])
    return cache


def _actions_for_requests(requests: list[dict], cache: dict[str, list[dict]]) -> list[dict]:
    actions: list[dict] = []
    for request in requests:
        actions.extend(cache.get(request["cache_key"], []))
    return actions


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _write_summary(submission_dir: Path, summary: dict) -> None:
    (submission_dir / "final_audit_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def _summary(
    *,
    mode: str,
    human_decisions: dict[str, dict],
    source_rows: list[dict],
    canonical_rows: list[dict],
    deduped_rows: list[dict],
    duplicate_groups: list[dict],
    audit_requests: list[dict],
    cache_hits: int,
    cache_misses: int,
    actions: list[dict],
    final_rows: list[dict],
    human_review_rows: list[dict],
    dataset_hash: str,
) -> dict:
    action_counts: dict[str, int] = {}
    for action in actions:
        key = str(action.get("action") or "")
        action_counts[key] = action_counts.get(key, 0) + 1
    p4_scope = any(request.get("scope") == "pillar_4_global" for request in audit_requests)
    return {
        "mode": mode,
        "model": FINAL_AUDIT_MODEL,
        "reasoning_effort": FINAL_AUDIT_REASONING_EFFORT,
        "prompt_version": P4_FINAL_AUDIT_PROMPT_VERSION if p4_scope else FINAL_AUDIT_PROMPT_VERSION,
        "indicator_spec_version": P4_INDICATOR_SPEC_VERSION if p4_scope else INDICATOR_SPEC_VERSION,
        "dataset_hash": dataset_hash,
        "source_rows": len(source_rows),
        "canonical_rows": len(canonical_rows),
        "deterministic_deduped_rows": len(deduped_rows),
        "duplicate_groups": len(duplicate_groups),
        "audit_chunks": len(audit_requests),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "actions": len(actions),
        "action_counts": action_counts,
        "final_rows": len(final_rows),
        "human_review_rows": len(human_review_rows),
        "human_accept": sum(1 for row in human_decisions.values() if str(row.get("decision")).casefold() == "accept"),
        "human_reject": sum(1 for row in human_decisions.values() if str(row.get("decision")).casefold() == "reject"),
    }


def _dataset_hash(
    rows: list[dict],
    review_rows: list[dict],
    human_decisions: dict[str, dict],
    *,
    pillars: set[int] | None = None,
    framework_conclusions: list[dict] | None = None,
) -> str:
    payload = {
        "rows": rows,
        "review_rows": review_rows,
        "human_decisions_hash": hashlib.sha256(json.dumps(human_decisions, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        "indicator_spec_version": P4_INDICATOR_SPEC_VERSION if pillars == {4} else INDICATOR_SPEC_VERSION,
        "framework_conclusions": framework_conclusions or [],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _global_cache_key(*, economy: str, scope: str, chunk_index: int, dataset_hash: str) -> str:
    payload = {
        "economy": economy,
        "scope": scope,
        "chunk_index": chunk_index,
        "dataset_hash": dataset_hash,
        "prompt_version": _audit_prompt_version(scope),
        "indicator_spec_version": _audit_spec_version(scope),
        "model": FINAL_AUDIT_MODEL,
        "reasoning_effort": FINAL_AUDIT_REASONING_EFFORT,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _chunk_rows(rows: list[dict], char_budget: int) -> list[list[dict]]:
    if not rows:
        return [[]]
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_size = 0
    for row in rows:
        size = len(json.dumps({"audit_key": row["audit_key"], "row": row["row"]}, ensure_ascii=False))
        if current and current_size + size > char_budget:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(row)
        current_size += size
    if current:
        chunks.append(current)
    return chunks


def _compact_index(rows: list[dict]) -> list[dict[str, str]]:
    return [
        {
            "audit_key": row["audit_key"],
            "stable_identity": row.get("stable_identity", ""),
            "evidence_hash": row.get("evidence_hash", ""),
            "review_key": row.get("review_key", ""),
            "indicator": row["row"].get("Indicator ID", ""),
            "law": row["row"].get("Law Name", ""),
            "article": row["row"].get("Article / Section", ""),
        }
        for row in rows
    ]


def _indicator_counts(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        indicator = row["row"].get("Indicator ID", "")
        counts[indicator] = counts.get(indicator, 0) + 1
    return dict(sorted(counts.items()))


def _law_article_distribution(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = f"{row['row'].get('Law Name', '')}|{row['row'].get('Article / Section', '')}"
        counts[key] = counts.get(key, 0) + 1
    return {key: value for key, value in sorted(counts.items()) if value > 1}


def _audit_key(row: dict[str, str]) -> str:
    return "row:" + _row_evidence_hash(row)[:24]


def _row_evidence_hash(row: dict[str, str]) -> str:
    basis = "|".join(str(row.get(key) or "") for key in ("Economy", "Indicator ID", "Law Name", "Article / Section", "Verbatim Snippet"))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _stable_identity(row: dict[str, str], review_key: str = "") -> str:
    payload = {
        "economy": _norm(row.get("Economy", "")),
        "indicator_id": _norm(row.get("Indicator ID", "")),
        "law_identity": _norm(row.get("Law Name", "")),
        "article": _norm(row.get("Article / Section", "")),
        "evidence_hash": _row_evidence_hash(row),
        "review_key": str(review_key or "").strip(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _dedup_key(row: dict[str, str]) -> str:
    return "|".join(
        (
            _norm(row.get("Economy", "")),
            _norm(row.get("Indicator ID", "")),
            _norm(row.get("Law Name", "")),
            _norm(row.get("Article / Section", "")),
            _norm(row.get("Verbatim Snippet", "")),
        )
    )


def _row_valid(row: dict[str, str]) -> bool:
    indicator = row.get("Indicator ID", "")
    return bool(indicator in INDICATOR_SPECS and row.get("Article / Section") and row.get("Verbatim Snippet") and row.get("Source URL"))


def _human_review_from_row(row: dict, action: dict, reason: str | None = None) -> dict:
    submission_row = row.get("row", {})
    return {
        "audit_key": row.get("audit_key", ""),
        "review_key": row.get("review_key", ""),
        "economy": submission_row.get("Economy", ""),
        "indicator_id": submission_row.get("Indicator ID", ""),
        "law_name": submission_row.get("Law Name", ""),
        "article_section": submission_row.get("Article / Section", ""),
        "verbatim_quote": submission_row.get("Verbatim Snippet", ""),
        "source_url": submission_row.get("Source URL", ""),
        "current_status": reason or action.get("action", ""),
        "reason": reason or action.get("reason", ""),
        "action": action.get("action", ""),
        "row": submission_row,
    }


def _human_review_from_action(action: dict, reason: str) -> dict:
    return {"audit_key": action.get("target_audit_key", ""), "reason": reason, "action": action.get("action", "")}


def _valid_human_review_record(row: dict) -> bool:
    if _non_public_review_reason(str(row.get("reason") or row.get("current_status") or row.get("status") or "")):
        return False
    nested = row.get("row") if isinstance(row.get("row"), dict) else {}
    economy = str(row.get("economy") or nested.get("Economy") or "").strip()
    indicator = str(row.get("indicator_id") or row.get("indicator") or nested.get("Indicator ID") or "").strip()
    law = str(row.get("law_name") or row.get("law_title") or nested.get("Law Name") or "").strip()
    article = str(row.get("article_section") or row.get("article") or nested.get("Article / Section") or "").strip()
    quote = str(row.get("verbatim_quote") or row.get("focal_quote") or nested.get("Verbatim Snippet") or "").strip()
    source_url = str(row.get("source_url") or nested.get("Source URL") or "").strip()
    review_key = str(row.get("review_key") or row.get("audit_key") or row.get("human_review_id") or row.get("task_id") or row.get("claim_id") or "").strip()
    return bool(economy and indicator and law and article and quote and source_url and review_key)


def _append_note(existing: str, note: str) -> str:
    existing = str(existing or "").strip()
    return f"{existing}; {note}" if existing else note


def _norm(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w]+", " ", text)
    return text.strip()


def _economy_title(economy: str) -> str:
    return {"singapore": "Singapore", "australia": "Australia", "malaysia": "Malaysia"}.get(economy, economy.title())
