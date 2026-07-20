"""Deterministic post-LLM validation for mapping decisions."""

from __future__ import annotations

import json
import re
import unicodedata

from .indicator_specs import (
    FRAMEWORK_REVIEW_ELEMENTS,
    FRAMEWORK_REVIEW_EXCLUSIONS,
    canonical_framework_element,
    framework_element_code,
    framework_legal_function_supported,
    focal_required_elements,
    required_review_elements,
    INDICATOR_SPECS,
    P6_REVIEW_ELEMENTS,
    P6_REVIEW_EXCLUSIONS,
    P7_REVIEW_ELEMENTS_BY_GROUP,
    P7_REVIEW_EXCLUSIONS_BY_GROUP,
    P4_REVIEW_ELEMENTS_BY_GROUP,
    P4_REVIEW_EXCLUSIONS_BY_GROUP,
)
from .economy_profiles import domestic_terms, foreign_terms
from .models import CandidateTask, IndicatorMatch, MappingDecision, ReviewerAttributes, ReviewerDecision, ReviewerElementAssessment, ReviewerExclusionAssessment, ReviewerOptionalCheck, ValidatedTaskResult


VALIDATION_VERSION = "rdtii-validation-v9-unified-focal-contract"
P4_VALIDATION_VERSION = "rdtii-p4-validation-v3-independent-framework-elements"

P6_TREATY_REVIEW_ELEMENTS = ("BINDING_AGREEMENT_IN_FORCE", "DATA_FLOW_COMMITMENT", "OFFICIAL_SOURCE")
P6_TREATY_REVIEW_EXCLUSIONS = ("NON_BINDING_OR_NOT_IN_FORCE", "NO_DATA_FLOW_COMMITMENT", "SOURCE_UNVERIFIABLE")

TECHNICAL_DETAILS = {
    "INVALID_OUTPUT_SCHEMA",
    "EMPTY_FOCAL_TEXT",
    "EVIDENCE_ID_NOT_FOUND",
    "EVIDENCE_NOT_IN_SOURCE",
    "FOCAL_CLAUSE_MISMATCH",
    "REVIEWER_SCHEMA_ERROR",
    "REVIEWER_MODEL_UNAVAILABLE",
    "PIPELINE_ERROR",
    "LLM_ERROR",
}


def normalize_reviewer_decision(
    reviewer: ReviewerDecision,
    *,
    required_elements: tuple[str, ...],
    allowed_exclusions: tuple[str, ...],
    allowed_evidence_ids: set[str],
    require_record_scope_basis: bool = False,
) -> tuple[ReviewerDecision, dict]:
    """Normalize recoverable Reviewer schema drift before deterministic status.

    Recoverable:
    - duplicate element/exclusion rows are merged deterministically;
    - extra element/exclusion rows are moved to optional_checks.

    Hard retry triggers:
    - unknown evidence IDs;
    - missing required element or exclusion rows;
    - missing P7 retention record_scope_basis when required.
    """

    issues = {
        "unknown_evidence_ids": [],
        "missing_required_elements": [],
        "missing_exclusions": [],
        "extra_elements": [],
        "extra_exclusions": [],
        "duplicates_merged": [],
        "missing_record_scope_basis": False,
    }
    optional_checks = list(reviewer.optional_checks)

    element_rows: dict[str, list[ReviewerElementAssessment]] = {}
    for item in reviewer.element_assessments:
        code = _canonical_review_element_code(item.element_code)
        if code in required_elements:
            if code != item.element_code:
                item = item.model_copy(update={"element_id": code})
            element_rows.setdefault(code, []).append(item)
        else:
            issues["extra_elements"].append(item.element_code)
            optional_checks.append(
                ReviewerOptionalCheck(
                    check_code=item.element_code,
                    status=item.status,
                    evidence_ids=item.evidence_ids,
                    reason=item.reason,
                )
            )
    merged_elements: list[ReviewerElementAssessment] = []
    for code in required_elements:
        rows = element_rows.get(code) or []
        if not rows:
            issues["missing_required_elements"].append(code)
            continue
        if len(rows) > 1:
            issues["duplicates_merged"].append(code)
        merged_elements.append(_merge_element_rows(code, rows))

    exclusion_rows: dict[str, list[ReviewerExclusionAssessment]] = {}
    for item in reviewer.exclusion_assessments:
        if item.exclusion_code in allowed_exclusions:
            exclusion_rows.setdefault(item.exclusion_code, []).append(item)
        else:
            issues["extra_exclusions"].append(item.exclusion_code)
            optional_checks.append(
                ReviewerOptionalCheck(
                    check_code=item.exclusion_code,
                    status=item.status,
                    evidence_ids=item.evidence_ids,
                    reason=item.reason,
                )
            )
    merged_exclusions: list[ReviewerExclusionAssessment] = []
    for code in allowed_exclusions:
        rows = exclusion_rows.get(code) or []
        if not rows:
            issues["missing_exclusions"].append(code)
            continue
        if len(rows) > 1:
            issues["duplicates_merged"].append(code)
        merged_exclusions.append(_merge_exclusion_rows(code, rows))

    for item in [*merged_elements, *merged_exclusions, *optional_checks]:
        for evidence_id in item.evidence_ids:
            if evidence_id not in allowed_evidence_ids:
                issues["unknown_evidence_ids"].append(evidence_id)

    if require_record_scope_basis and reviewer.record_scope_basis is None:
        issues["missing_record_scope_basis"] = True

    normalized = reviewer.model_copy(
        update={
            "elements": merged_elements,
            "exclusions": merged_exclusions,
            "optional_checks": optional_checks,
        }
    )
    issues["unknown_evidence_ids"] = sorted(set(issues["unknown_evidence_ids"]))
    issues["missing_required_elements"] = sorted(set(issues["missing_required_elements"]))
    issues["missing_exclusions"] = sorted(set(issues["missing_exclusions"]))
    issues["extra_elements"] = sorted(set(issues["extra_elements"]))
    issues["extra_exclusions"] = sorted(set(issues["extra_exclusions"]))
    issues["duplicates_merged"] = sorted(set(issues["duplicates_merged"]))
    return normalized, issues


def _canonical_review_element_code(code: str) -> str:
    if code == "EXPLICIT_SINGAPORE_STORAGE_LOCATION":
        return "EXPLICIT_DOMESTIC_STORAGE_LOCATION"
    return code


def reviewer_schema_retry_needed(issues: dict) -> bool:
    return bool(
        issues.get("unknown_evidence_ids")
        or issues.get("missing_required_elements")
        or issues.get("missing_exclusions")
        or issues.get("missing_record_scope_basis")
    )


def reviewer_retry_instructions(issues: dict) -> str:
    parts = []
    if issues.get("unknown_evidence_ids"):
        parts.append(f"Replace unknown evidence IDs: {', '.join(issues['unknown_evidence_ids'])}. Use only allowed_evidence_ids.")
    if issues.get("missing_required_elements"):
        parts.append(f"Add exactly one element_assessment for each missing element: {', '.join(issues['missing_required_elements'])}.")
    if issues.get("missing_exclusions"):
        parts.append(f"Add exactly one exclusion_assessment for each missing exclusion: {', '.join(issues['missing_exclusions'])}.")
    if issues.get("missing_record_scope_basis"):
        parts.append("Set record_scope_basis to one allowed P7-I3 basis.")
    return " ".join(parts)


def _merge_element_rows(code: str, rows: list[ReviewerElementAssessment]) -> ReviewerElementAssessment:
    order = {"supported": 3, "uncertain": 2, "not_supported": 1}
    status = max((row.status for row in rows), key=lambda value: order[value])
    evidence_ids = _dedupe_text(evidence_id for row in rows for evidence_id in row.evidence_ids)
    reason = " | ".join(_dedupe_text(row.reason for row in rows))
    return ReviewerElementAssessment(element_id=code, status=status, evidence_ids=evidence_ids, reason=reason)


def _merge_exclusion_rows(code: str, rows: list[ReviewerExclusionAssessment]) -> ReviewerExclusionAssessment:
    order = {"triggered": 3, "uncertain": 2, "not_triggered": 1}
    status = max((row.status for row in rows), key=lambda value: order[value])
    evidence_ids = _dedupe_text(evidence_id for row in rows for evidence_id in row.evidence_ids)
    reason = " | ".join(_dedupe_text(row.reason for row in rows))
    return ReviewerExclusionAssessment(exclusion_id=code, status=status, evidence_ids=evidence_ids, reason=reason)

def validate_decision(
    task: CandidateTask,
    decision: MappingDecision | None,
    *,
    model_name: str,
    prompt_version: str,
    cache_key: str,
    llm_call: bool,
    cache_hit: bool,
    retries: int,
    error: str | None = None,
) -> ValidatedTaskResult:
    if decision is None:
        return _result(task, "error", None, None, ["MODEL_ERROR"], [], "", prompt_version, model_name, cache_key, llm_call, cache_hit, retries, error, technical_detail=error or "LLM decision was not produced")

    if decision.decision == "no_match":
        return _result(
            task,
            "rejected",
            decision,
            None,
            ["NO_MATCH"],
            [],
            decision.rationale,
            prompt_version,
            model_name,
            cache_key,
            llm_call,
            cache_hit,
            retries,
            None,
        )

    if decision.decision == "uncertain" and not decision.matches:
        if _is_true_review(decision):
            return _result(task, "review", decision, None, ["LEGAL_UNCERTAINTY"], ["LEGAL_UNCERTAINTY"], decision.rationale, prompt_version, model_name, cache_key, llm_call, cache_hit, retries, None)
        return _result(task, "rejected", decision, None, ["NO_MATCH"], [], decision.rationale, prompt_version, model_name, cache_key, llm_call, cache_hit, retries, None)

    prechecked: list[dict] = []
    rejected_codes: list[str] = []
    warnings: list[str] = []
    for match in decision.matches:
        if match.indicator not in task.candidate_indicators:
            rejected_codes.append("INVALID_OUTPUT_SCHEMA")
            continue
        p4_framework_element = task.route_topic.startswith("P4_") and task.task_kind == "framework_element"
        if match.legal_function != "operative_rule" and not p4_framework_element:
            rejected_codes.append("NOT_OPERATIVE_RULE")
            continue
        if not task.focal_text.strip():
            rejected_codes.append("EMPTY_FOCAL_TEXT")
            continue
        if not match.evidence_ids or any(evidence_id not in task.evidence_segments for evidence_id in match.evidence_ids):
            rejected_codes.append("EVIDENCE_ID_NOT_FOUND")
            continue
        if "S1" not in set(match.evidence_ids):
            warnings.append("MAPPER_EVIDENCE_DOES_NOT_INCLUDE_FOCAL_CLAUSE")
        quote = _quote_from_evidence_ids(task, match.evidence_ids)
        if not quote:
            rejected_codes.append("EVIDENCE_NOT_IN_SOURCE")
            continue
        prechecked.append(_match_record(match, quote, [], "prechecked"))

    if prechecked:
        first = prechecked[0]
        return _result(
            task,
            "accepted",
            decision,
            first["indicator"],
            [],
            [],
            decision.rationale,
            prompt_version,
            model_name,
            cache_key,
            llm_call,
            cache_hit,
            retries,
            None,
            warnings=warnings,
            accepted_matches=prechecked,
        )

    status = "error" if _has_any_code(rejected_codes, tuple(TECHNICAL_DETAILS)) else "rejected"
    return _result(task, status, decision, None, rejected_codes or ["NO_MATCH"], [], decision.rationale, prompt_version, model_name, cache_key, llm_call, cache_hit, retries, None, warnings=warnings)

def apply_reviewer_decisions(
    result: ValidatedTaskResult,
    reviewer_decisions: list[ReviewerDecision],
    *,
    reviewer_model_name: str,
    reviewer_cache_key: str | None,
    reviewer_llm_call: bool,
    reviewer_cache_hit: bool,
    task: CandidateTask | None = None,
) -> ValidatedTaskResult:
    if result.status not in {"accepted", "review"} or not (result.accepted_matches or result.review_matches):
        return result
    records = [*result.accepted_matches, *result.review_matches]
    accepted: list[dict] = []
    review: list[dict] = []
    rejected_codes: list[str] = []
    failed_required_elements: list[str] = []
    uncertain_elements_all: list[str] = []
    uncertain_exclusions_all: list[str] = []
    triggered_exclusions: list[str] = []
    focal_uncertainties: list[str] = []
    focal_roles: list[str] = []
    record_scope_basis: list[str] = []
    technical_details: list[str] = []
    affected_evidence_ids: list[str] = []
    expected_repair_actions: list[str] = []
    resolved_attribute_rows: list[ReviewerAttributes] = []
    supporting_only_seen = False
    reviewer_by_record = list(zip(records, reviewer_decisions))
    if not reviewer_by_record:
        rejected_codes.append("REVIEWER_MODEL_UNAVAILABLE")

    for record, reviewer in reviewer_by_record:
        resolved = resolve_reviewed_status(result, record, reviewer, task)
        failed_required_elements.extend(resolved.get("failed_required_elements", []))
        uncertain_elements = resolved.get("uncertain_elements", [])
        uncertain_exclusions = resolved.get("uncertain_exclusions", [])
        uncertain_elements_all.extend(uncertain_elements)
        uncertain_exclusions_all.extend(uncertain_exclusions)
        triggered_exclusions.extend(resolved.get("triggered_exclusions", []))
        if resolved.get("focal_uncertainty"):
            focal_uncertainties.append(str(resolved["focal_uncertainty"]))
        if resolved.get("focal_role"):
            focal_roles.append(str(resolved["focal_role"]))
        if resolved.get("record_scope_basis"):
            record_scope_basis.append(str(resolved["record_scope_basis"]))
        affected_evidence_ids.extend(resolved.get("affected_evidence_ids", []))
        if resolved.get("technical_detail"):
            technical_details.append(str(resolved["technical_detail"]))
        if resolved.get("expected_repair_action"):
            expected_repair_actions.append(str(resolved["expected_repair_action"]))
        resolved_attributes = resolved.get("reviewer_attributes")
        if not isinstance(resolved_attributes, ReviewerAttributes):
            resolved_attributes = reviewer.attributes
        resolved_reviewer = resolved.get("reviewer")
        if not isinstance(resolved_reviewer, ReviewerDecision):
            resolved_reviewer = reviewer
        resolved_attribute_rows.append(resolved_attributes)
        if resolved["status"] == "accepted":
            for indicator in resolved.get("accepted_indicators", []):
                accepted_record = dict(record, indicator=indicator, status="accepted", result_code=None, focal_role=reviewer.focal_role, record_scope_basis=reviewer.record_scope_basis, reviewer_attributes=resolved_attributes.model_dump(), reviewer=resolved_reviewer.model_dump())
                if resolved.get("p4_framework_element"):
                    accepted_record["p4_framework_element"] = resolved["p4_framework_element"]
                if resolved.get("p4_framework_facts"):
                    accepted_record["p4_framework_facts"] = resolved["p4_framework_facts"]
                accepted.append(accepted_record)
        elif resolved["status"] == "human_legal_review":
            review.append(dict(record, status="human_legal_review", result_code=resolved["result_code"], uncertain_elements=uncertain_elements, uncertain_exclusions=uncertain_exclusions, focal_uncertainty=resolved.get("focal_uncertainty"), focal_role=reviewer.focal_role, record_scope_basis=reviewer.record_scope_basis, reviewer_attributes=resolved_attributes.model_dump(), reviewer=resolved_reviewer.model_dump()))
        elif resolved.get("focal_role") == "supporting_only":
            supporting_only_seen = True
            rejected_codes.append("NO_MATCH")
        elif resolved["status"] == "technical_repair":
            rejected_codes.append(resolved["result_code"])
        else:
            rejected_codes.append(resolved["result_code"])

    update = {
        "reviewer_model_name": reviewer_model_name,
        "reviewer_cache_key": reviewer_cache_key,
        "reviewer_llm_call": reviewer_llm_call,
        "reviewer_cache_hit": reviewer_cache_hit,
        "reviewer_decision": reviewer_decisions[0] if reviewer_decisions else None,
        "failed_required_elements": sorted(set(failed_required_elements)),
        "uncertain_exclusions": sorted(set(uncertain_exclusions_all)),
        "focal_uncertainty": "; ".join(_dedupe_text(focal_uncertainties)) or None,
        "triggered_exclusions": sorted(set(triggered_exclusions)),
        "affected_evidence_ids": sorted(set(affected_evidence_ids)),
        "expected_repair_action": "; ".join(_dedupe_text(expected_repair_actions)) or None,
        "focal_role": focal_roles[0] if focal_roles else None,
        "record_scope_basis": record_scope_basis[0] if record_scope_basis else None,
        "reviewer_attributes": resolved_attribute_rows[0] if resolved_attribute_rows else reviewer_decisions[0].attributes if reviewer_decisions else None,
    }
    if accepted:
        first = accepted[0]
        update.update({
            "status": "accepted",
            "queue_type": "none",
            "indicator": first.get("indicator"),
            "accepted_matches": accepted,
            "review_matches": [],
            "result_code": None,
            "failure_codes": [],
            "review_reasons": [],
        })
    elif review:
        first = review[0]
        update.update({
            "status": "human_legal_review",
            "queue_type": "human_legal_review",
            "indicator": first.get("indicator"),
            "accepted_matches": [],
            "review_matches": review,
            "result_code": "LEGAL_UNCERTAINTY",
            "failure_codes": ["LEGAL_UNCERTAINTY"],
            "review_reasons": ["LEGAL_UNCERTAINTY"],
            "uncertain_elements": sorted(set().union(*(set(item.get("uncertain_elements", [])) for item in review))),
            "uncertain_exclusions": sorted(set().union(*(set(item.get("uncertain_exclusions", [])) for item in review))),
            "focal_uncertainty": "; ".join(_dedupe_text(str(item.get("focal_uncertainty") or "") for item in review)) or None,
        })
    else:
        codes = [code for code in rejected_codes if code] or ["REQUIRED_ELEMENT_MISSING"]
        result_code = "TECHNICAL_INPUT_ERROR" if _has_any_code(codes, ("TECHNICAL_INPUT_ERROR", "MODEL_ERROR")) else codes[0]
        queue_type = "technical_repair" if result_code in {"TECHNICAL_INPUT_ERROR", "MODEL_ERROR"} else "none"
        if supporting_only_seen and result_code == "NO_MATCH":
            update["focal_role"] = "supporting_only"
        update.update({
            "status": "technical_repair" if queue_type == "technical_repair" else "rejected",
            "queue_type": queue_type,
            "indicator": result.indicator,
            "accepted_matches": [],
            "review_matches": records if supporting_only_seen and queue_type == "none" else [],
            "result_code": result_code,
            "failure_codes": [result_code],
            "review_reasons": [],
            "technical_detail": ("; ".join(_dedupe_text(technical_details)) or result_code) if queue_type == "technical_repair" else result.technical_detail,
            "expected_repair_action": ("; ".join(_dedupe_text(expected_repair_actions)) or "inspect_reviewer_or_pipeline_error") if queue_type == "technical_repair" else result.expected_repair_action,
        })
    return result.model_copy(update=update)


def resolve_reviewed_status(
    result: ValidatedTaskResult,
    record: dict,
    reviewer: ReviewerDecision,
    task: CandidateTask | None = None,
) -> dict:
    route_topic = result.route_topic
    indicator = str(record.get("indicator") or result.indicator or "")
    if route_topic.startswith("P4_") and task is not None and task.task_kind == "framework_element":
        allowed_elements = FRAMEWORK_REVIEW_ELEMENTS.get(indicator, tuple())
        allowed_exclusions = FRAMEWORK_REVIEW_EXCLUSIONS.get(indicator, tuple())
    elif route_topic in P4_REVIEW_ELEMENTS_BY_GROUP:
        allowed_elements = P4_REVIEW_ELEMENTS_BY_GROUP[route_topic]
        allowed_exclusions = P4_REVIEW_EXCLUSIONS_BY_GROUP[route_topic]
    elif route_topic == "P6_LOCATION":
        allowed_elements = P6_REVIEW_ELEMENTS
        allowed_exclusions = P6_REVIEW_EXCLUSIONS
    elif route_topic == "P6_TREATY":
        allowed_elements = P6_TREATY_REVIEW_ELEMENTS
        allowed_exclusions = P6_TREATY_REVIEW_EXCLUSIONS
    elif route_topic == "P7_DATA_PROTECTION_FRAMEWORK":
        allowed_elements = FRAMEWORK_REVIEW_ELEMENTS["P7-I1"]
        allowed_exclusions = FRAMEWORK_REVIEW_EXCLUSIONS["P7-I1"]
    elif route_topic == "P7_CYBERSECURITY_FRAMEWORK":
        allowed_elements = FRAMEWORK_REVIEW_ELEMENTS["P7-I2"]
        allowed_exclusions = FRAMEWORK_REVIEW_EXCLUSIONS["P7-I2"]
    else:
        allowed_elements = P7_REVIEW_ELEMENTS_BY_GROUP.get(route_topic, tuple())
        allowed_exclusions = P7_REVIEW_EXCLUSIONS_BY_GROUP.get(route_topic, tuple())
    if (
        route_topic in {"P7_DATA_PROTECTION_FRAMEWORK", "P7_CYBERSECURITY_FRAMEWORK"}
        or indicator in {"P4-I2", "P4-I5", "P4-I6", "P4-I10"}
    ):
        return _resolve_framework_review_contract(result, record, reviewer, task)
    top_errors = _reviewer_top_level_errors(reviewer)
    if top_errors:
        return _resolved_error(";".join(top_errors), reviewer, "fix_reviewer_schema_or_status_matrix")
    if reviewer.decision == "no_match":
        return _resolved_rejected("NO_MATCH", failed=[], reviewer=reviewer)
    if reviewer.decision == "supporting_only":
        return {
            "status": "rejected",
            "result_code": "NO_MATCH",
            "accepted_indicators": [],
            "failed_required_elements": [],
            "uncertain_elements": [],
            "uncertain_exclusions": [],
            "triggered_exclusions": [],
            "focal_role": "supporting_only",
            "record_scope_basis": reviewer.record_scope_basis,
        }
    if reviewer.decision == "uncertain":
        return _resolved_review(["REVIEWER_FINAL_DECISION_UNCERTAIN"], [], reviewer)
    if reviewer.decision != "match":
        return _resolved_error("REVIEWER_SCHEMA_ERROR", reviewer, "fix_reviewer_schema_or_status_matrix")
    schema_errors = _reviewer_schema_errors(reviewer, allowed_elements, allowed_exclusions, task)
    if schema_errors:
        return _resolved_error(";".join(schema_errors), reviewer, "fix_reviewer_schema_or_evidence_ids")
    focal_supporting = _focal_clause_supporting_only(result, record, task)
    if focal_supporting:
        return {
            "status": "rejected",
            "result_code": "NO_MATCH",
            "accepted_indicators": [],
            "failed_required_elements": [],
            "uncertain_elements": [],
            "uncertain_exclusions": [],
            "triggered_exclusions": [],
            "focal_role": "supporting_only",
            "record_scope_basis": reviewer.record_scope_basis,
            "focal_uncertainty": focal_supporting,
        }
    if reviewer.focal_integrity.status == "technical_mismatch" or (
        reviewer.focal_integrity.status == "source_mismatch"
        and _is_true_source_mismatch(reviewer.focal_integrity.reason)
    ):
        return _resolved_error(reviewer.focal_integrity.reason or "source_mismatch", reviewer, "repair_focal_clause_or_parser_mapping")
    if reviewer.focal_role == "supporting_only":
        return {
            "status": "rejected",
            "result_code": "NO_MATCH",
            "accepted_indicators": [],
            "failed_required_elements": [],
            "uncertain_elements": [],
            "uncertain_exclusions": [],
            "triggered_exclusions": [],
            "focal_role": "supporting_only",
            "record_scope_basis": reviewer.record_scope_basis,
        }

    element_status = {item.element_code: item.status for item in reviewer.element_assessments}
    exclusion_status = {item.exclusion_code: item.status for item in reviewer.exclusion_assessments}
    human_review_override = _is_human_review_decision(reviewer)
    if route_topic == "P7_RETENTION":
        _apply_retention_scope_basis(element_status, reviewer)
        if not human_review_override:
            _apply_retention_non_record_rules(element_status, task)
    if route_topic == "P7_ACCOUNTABILITY":
        reviewer = _apply_accountability_context_rules(element_status, exclusion_status, reviewer, task)
        _suppress_data_protection_general_compliance_exclusion(element_status, exclusion_status)
    if route_topic == "P7_GOVERNMENT_ACCESS":
        reviewer = _apply_government_access_context_rules(reviewer, task)
        if _p7i5_personal_data_uncertain(task):
            element_status["PERSONAL_OR_IDENTIFIABLE_DATA"] = "uncertain"
    triggered = [code for code, status in exclusion_status.items() if status == "triggered"]
    uncertain_exclusions = [code for code, status in exclusion_status.items() if status == "uncertain"]
    if route_topic == "P6_LOCATION":
        focal_gate = _indicator_focal_acceptance_gate(indicator, task)
        if focal_gate:
            return _resolved_review([focal_gate], [], reviewer)
        return _resolve_p6_fact_matrix(record, element_status, exclusion_status, reviewer, task)
    if route_topic == "P6_TREATY":
        return _resolve_treaty_fact_matrix(allowed_elements, allowed_exclusions, element_status, exclusion_status, reviewer)
    if indicator in {"P4-I1", "P4-I3", "P4-I9"}:
        return _resolve_p4_fact_matrix(
            indicator,
            allowed_elements,
            allowed_exclusions,
            element_status,
            exclusion_status,
            reviewer,
            task,
        )

    # The reviewer/element contract is authoritative.  Free-text rationale is
    # an audit explanation, never a second decision channel.
    if indicator == "P7-I4":
        focal_gate = _indicator_focal_acceptance_gate(indicator, task)
        if focal_gate:
            return _resolved_review([focal_gate], [], reviewer)
        path_statuses = (element_status.get("DPO_PATH"), element_status.get("DPIA_PATH"))
        if "supported" not in path_statuses:
            if "uncertain" in path_statuses:
                return _resolved_review(["DPO_PATH", "DPIA_PATH"], [], reviewer)
            return _resolved_rejected("REQUIRED_ELEMENT_MISSING", failed=["DPO_PATH", "DPIA_PATH"], reviewer=reviewer)
        reviewer = _reconcile_accountability_path(reviewer, element_status)
        if reviewer.attributes.accountability_path in {None, "uncertain"}:
            return _resolved_review(["ACCOUNTABILITY_PATH"], [], reviewer)
    required = _required_for_indicator(indicator, route_topic, element_status)
    if not human_review_override:
        focal_missing = _required_focal_support_missing(indicator, required, reviewer)
        if focal_missing:
            return {
                "status": "rejected",
                "result_code": "NO_MATCH",
                "accepted_indicators": [],
                "failed_required_elements": focal_missing,
                "uncertain_elements": [],
                "uncertain_exclusions": [],
                "triggered_exclusions": [],
                "focal_role": "supporting_only",
                "record_scope_basis": reviewer.record_scope_basis,
                "focal_uncertainty": "required element is supported only by supporting context; focal evidence does not independently establish it",
            }
    failed = [code for code in required if element_status.get(code) in {None, "not_supported"}]
    uncertain = [code for code in required if element_status.get(code) == "uncertain"]
    if triggered:
        return _resolved_rejected("EXCLUSION_TRIGGERED", triggered=triggered, reviewer=reviewer)
    if failed:
        return _resolved_rejected("REQUIRED_ELEMENT_MISSING", failed=failed, reviewer=reviewer)
    if indicator == "P7-I3":
        focal_gate = _indicator_focal_acceptance_gate(indicator, task)
        if focal_gate:
            return _resolved_review([focal_gate], [], reviewer)
        missing = _missing_retention_attributes(reviewer)
        if missing:
            return _resolved_review(missing, [], reviewer)
    if indicator == "P7-I5" and reviewer.attributes.judicial_authorization in {None, "uncertain"}:
        return _resolved_review(["JUDICIAL_AUTHORIZATION"], [], reviewer)
    if uncertain or uncertain_exclusions or reviewer.focal_integrity.status in {"uncertain", "incomplete_context"} or reviewer.focal_role == "uncertain":
        return _resolved_review(uncertain, uncertain_exclusions, reviewer)
    if all(element_status.get(code) == "supported" for code in required) and all(exclusion_status.get(code) == "not_triggered" for code in allowed_exclusions):
        return _resolved_accepted([indicator], reviewer)
    return _resolved_error("REVIEWER_SCHEMA_ERROR", reviewer, "fix_reviewer_schema_or_status_matrix")


def _resolve_framework_review_contract(
    result: ValidatedTaskResult,
    record: dict,
    reviewer: ReviewerDecision,
    task: CandidateTask | None,
) -> dict:
    top_errors = _reviewer_top_level_errors(reviewer)
    if top_errors:
        return _resolved_error(";".join(top_errors), reviewer, "fix_reviewer_schema_or_status_matrix")
    if reviewer.decision == "no_match":
        return _resolved_rejected("NO_MATCH", failed=[], reviewer=reviewer)
    if reviewer.decision == "supporting_only":
        return {
            "status": "rejected",
            "result_code": "NO_MATCH",
            "accepted_indicators": [],
            "failed_required_elements": [],
            "uncertain_elements": [],
            "uncertain_exclusions": [],
            "triggered_exclusions": [],
            "focal_role": "supporting_only",
            "record_scope_basis": reviewer.record_scope_basis,
        }
    if reviewer.decision == "uncertain":
        return _resolved_review(["REVIEWER_FINAL_DECISION_UNCERTAIN"], [], reviewer)
    if reviewer.decision != "match":
        return _resolved_error("REVIEWER_SCHEMA_ERROR", reviewer, "fix_reviewer_schema_or_status_matrix")
    indicator = str(record.get("indicator") or result.indicator or "")
    allowed_elements = FRAMEWORK_REVIEW_ELEMENTS.get(indicator, tuple())
    allowed_exclusions = FRAMEWORK_REVIEW_EXCLUSIONS.get(indicator, tuple())
    schema_errors = _reviewer_schema_errors(reviewer, allowed_elements, allowed_exclusions, task)
    if schema_errors:
        return _resolved_error(";".join(schema_errors), reviewer, "fix_reviewer_schema_or_evidence_ids")
    if reviewer.focal_role == "supporting_only":
        return {
            "status": "rejected",
            "result_code": "NO_MATCH",
            "accepted_indicators": [],
            "failed_required_elements": [],
            "uncertain_elements": [],
            "uncertain_exclusions": [],
            "triggered_exclusions": [],
            "focal_role": "supporting_only",
            "record_scope_basis": reviewer.record_scope_basis,
        }
    focal_supporting = _focal_clause_supporting_only(result, record, task)
    if focal_supporting:
        return {
            "status": "rejected",
            "result_code": "NO_MATCH",
            "accepted_indicators": [],
            "failed_required_elements": [],
            "uncertain_elements": [],
            "uncertain_exclusions": [],
            "triggered_exclusions": [],
            "focal_role": "supporting_only",
            "record_scope_basis": reviewer.record_scope_basis,
            "focal_uncertainty": focal_supporting,
        }
    element_status = {item.element_code: item.status for item in reviewer.element_assessments}
    exclusion_status = {item.exclusion_code: item.status for item in reviewer.exclusion_assessments}
    if indicator == "P4-I6":
        online_check = next(
            (check for check in reviewer.optional_checks if check.check_code == "P4_ONLINE_NEXUS"),
            None,
        )
        if online_check is None:
            return _resolved_error(
                "P4 online-nexus review field missing",
                reviewer,
                "rerun_p4_framework_reviewer",
            )
        if online_check.status == "not_supported":
            return _resolved_rejected("REQUIRED_ELEMENT_MISSING", failed=["ONLINE_NEXUS"], reviewer=reviewer)
        if online_check.status != "supported":
            return _resolved_review(["ONLINE_NEXUS"], [], reviewer)
        focal_text = _fold(task.focal_text if task is not None else "")
        if not _has(
            focal_text,
            (
                "online",
                "internet",
                "website",
                "online location",
                "digital copy",
                "digital transmission",
                "electronic transmission",
                "streaming",
                "uploading",
                "communication to the public",
                "making available",
                "network service",
                "network connection provider",
                "internet service provider",
            ),
        ):
            return _resolved_rejected(
                "REQUIRED_ELEMENT_MISSING",
                failed=["ONLINE_NEXUS"],
                reviewer=reviewer,
            )
    return _resolve_framework_element_fact_matrix(indicator, allowed_elements, allowed_exclusions, element_status, exclusion_status, reviewer, task)


def _resolve_p4_fact_matrix(
    indicator: str,
    allowed_elements: tuple[str, ...],
    allowed_exclusions: tuple[str, ...],
    element_status: dict[str, str],
    exclusion_status: dict[str, str],
    reviewer: ReviewerDecision,
    task: CandidateTask | None,
) -> dict:
    triggered = [code for code in allowed_exclusions if exclusion_status.get(code) == "triggered"]
    uncertain_exclusions = [code for code in allowed_exclusions if exclusion_status.get(code) == "uncertain"]
    required = required_review_elements(indicator)
    failed = [code for code in required if element_status.get(code) in {None, "not_supported"}]
    uncertain = [code for code in required if element_status.get(code) == "uncertain"]
    if triggered:
        return _resolved_rejected("EXCLUSION_TRIGGERED", triggered=triggered, reviewer=reviewer)
    if failed:
        return _resolved_rejected("REQUIRED_ELEMENT_MISSING", failed=failed, reviewer=reviewer)
    if uncertain or uncertain_exclusions:
        return _resolved_review(uncertain, uncertain_exclusions, reviewer)

    focal = _fold(task.focal_text if task is not None else "")
    facts = _p4_structured_review_facts(reviewer)
    if indicator == "P4-I1":
        qualifying = _has(
            focal,
            (
                "foreign applicant",
                "non-resident applicant",
                "resident agent",
                "local agent",
                "local representative",
                "resident representative",
                "first application",
                "first apply",
                "first filing",
                "filing abroad",
                "file abroad",
                "permission to file abroad",
                "foreign filing permission",
                "substantive examination",
                "examined as to substance",
                "examination as to substance",
            ),
        )
        local_markers = [
            "local",
            "resident",
            "within the jurisdiction",
            "in the jurisdiction",
            "within the country",
            "in the country",
        ]
        if task is not None and task.economy:
            local_markers.append(_fold(task.economy))
        local_service = _has(focal, ("address for service", "local address")) and _has(
            focal,
            tuple(local_markers),
        )
        material_fee = _has(focal, ("excessive fee", "additional fee for foreign", "higher fee for foreign"))
        typed_fee = _fold(str(facts.get("fee_or_cost_burden") or ""))
        typed_material_fee = _has(focal, ("fee", "cost")) and _has(
            typed_fee,
            ("excessive", "high", "higher", "additional", "material", "disproportionate"),
        )
        if not qualifying and not local_service and not material_fee and not typed_material_fee:
            return _resolved_rejected(
                "REQUIRED_ELEMENT_MISSING",
                failed=["MATERIAL_APPLICATION_BURDEN"],
                reviewer=reviewer,
            )
    elif indicator == "P4-I3":
        operative = _has(
            focal,
            (
                "must ",
                "shall ",
                "shall not",
                "may grant",
                "may authorise",
                "may authorize",
                "may not",
                "may use",
                "is entitled to use",
                "order the grant",
                "revoke",
                "cease to have effect",
                "is limited",
                "are limited",
            ),
        )
        restriction = _has(
            focal,
            (
                "compulsory licence",
                "compulsory license",
                "government use",
                "crown use",
                "working requirement",
                "work the patented",
                "use the patented invention",
                "no injunction",
                "injunction shall not",
                "injunction may not",
                "damages are limited",
                "damages shall not",
                "no damages",
                "account of profits shall not",
            ),
        )
        if not (operative and restriction):
            return _resolved_rejected(
                "REQUIRED_ELEMENT_MISSING",
                failed=["PATENT_RIGHT_RESTRICTION", "PRACTICAL_LEGAL_EFFECT"],
                reviewer=reviewer,
            )
    elif indicator == "P4-I9":
        protected = _has(
            focal,
            (
                "trade secret",
                "source code",
                "algorithm",
                "confidential business information",
                "proprietary information",
                "technical specification",
                "commercially sensitive information",
            ),
        )
        disclosure = _has(
            focal,
            (
                "must disclose",
                "shall disclose",
                "must provide",
                "shall provide",
                "must submit",
                "shall submit",
                "must produce",
                "shall produce",
                "may require",
                "order production",
                "grant access",
            ),
        )
        compulsion = _has(
            focal,
            (
                "offence",
                "penalty",
                "liable",
                "refuse",
                "revoke",
                "court may order",
                "order production",
                "required by notice",
                "must ",
                "shall ",
            ),
        )
        ordinary_discovery = _has(focal, ("discovery", "disclosure of documents")) and _has(
            focal,
            ("confidentiality order", "protective order", "confidentiality club", "restricted access"),
        )
        if ordinary_discovery:
            return _resolved_rejected(
                "EXCLUSION_TRIGGERED",
                triggered=["ORDINARY_DISCOVERY_WITH_ADEQUATE_SAFEGUARDS"],
                reviewer=reviewer,
            )
        official_actor = _has(
            focal,
            (
                "public officer",
                "government officer",
                "authorised officer",
                "authorized officer",
                "member of the authority",
                "employee of the authority",
                "officer may disclose",
                "officer shall disclose",
                "officer must disclose",
                "shall not disclose information received",
            ),
        )
        compelled_actor = _fold(str(facts.get("compelled_actor") or ""))
        information_holder = _fold(str(facts.get("information_holder") or ""))
        holder_role = _has(
            f"{compelled_actor} {information_holder}",
            (
                "company",
                "business",
                "undertaking",
                "right holder",
                "rights holder",
                "owner",
                "applicant",
                "licensee",
                "licence holder",
                "regulated entity",
                "provider",
                "information holder",
                "technology provider",
            ),
        )
        focal_holder_direction = _has(
            focal,
            (
                "company must",
                "company shall",
                "business must",
                "undertaking must",
                "applicant must",
                "licensee must",
                "licence holder must",
                "regulated entity must",
                "provider must",
                "holder must",
                "authority may require the company",
                "authority may require the holder",
                "court may order the company",
                "court may order the holder",
            ),
        )
        if official_actor and not focal_holder_direction:
            return _resolved_rejected(
                "EXCLUSION_TRIGGERED",
                triggered=["PUBLIC_OFFICIAL_DISCLOSURE_OR_INTERNAL_SHARING"],
                reviewer=reviewer,
            )
        missing = []
        if not protected:
            missing.append("PROTECTED_SUBJECT")
        if not disclosure:
            missing.append("COMPULSORY_DISCLOSURE_ACTION")
        if not compulsion:
            missing.append("LEGAL_COMPULSION")
        if missing:
            return _resolved_rejected("REQUIRED_ELEMENT_MISSING", failed=missing, reviewer=reviewer)
        if not holder_role and not focal_holder_direction:
            return _resolved_rejected(
                "REQUIRED_ELEMENT_MISSING",
                failed=["LEGALLY_COMPELLED_HOLDER"],
                reviewer=reviewer,
            )

    required_facts = {
        "P4-I1": ("affected_applicant", "operative_requirement", "legal_effect"),
        "P4-I3": ("restriction_type", "triggering_conditions", "practical_legal_effect"),
        "P4-I9": ("protected_subject", "information_holder", "compelled_actor", "disclosure_action", "non_compliance_consequence"),
    }.get(indicator, ())
    missing_facts = [
        field
        for field in required_facts
        if facts.get(field) in (None, "", [], {})
    ]
    if missing_facts and not _is_human_review_decision(reviewer):
        return _resolved_review(
            [f"REQUIRED_ATTRIBUTE:{field}" for field in missing_facts],
            [],
            reviewer,
        )
    return _resolved_accepted([indicator], reviewer)


def _p4_structured_review_facts(reviewer: ReviewerDecision) -> dict:
    for check in reviewer.optional_checks:
        if check.check_code != "P4_STRUCTURED_FACTS":
            continue
        try:
            payload = json.loads(check.reason or "{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}

def _reviewer_top_level_errors(reviewer: ReviewerDecision) -> list[str]:
    if any(check.check_code == "REVIEWER_DECISION_MISSING_IN_CACHE" for check in reviewer.optional_checks):
        return ["REVIEWER_SCHEMA_ERROR"]
    if reviewer.decision not in {"match", "no_match", "supporting_only", "uncertain"}:
        return ["REVIEWER_SCHEMA_ERROR"]
    return []


def _is_human_review_decision(reviewer: ReviewerDecision) -> bool:
    return any(check.check_code == "HUMAN_REVIEW" and check.status == "applied" for check in reviewer.optional_checks)


def _resolve_framework_element_fact_matrix(
    indicator: str,
    allowed_elements: tuple[str, ...],
    allowed_exclusions: tuple[str, ...],
    element_status: dict[str, str],
    exclusion_status: dict[str, str],
    reviewer: ReviewerDecision,
    task: CandidateTask | None,
) -> dict:
    triggered = [code for code in allowed_exclusions if exclusion_status.get(code) == "triggered"]
    uncertain_exclusions = [code for code in allowed_exclusions if exclusion_status.get(code) == "uncertain"]
    p4_fields = _p4_framework_review_fields(reviewer) if indicator.startswith("P4-") else {}
    claimed = p4_fields.get("framework_element") or reviewer.attributes.framework_element
    claimed_name = canonical_framework_element(indicator, str(claimed or ""))
    claimed_code = framework_element_code(indicator, str(claimed or ""))
    candidate_name = _framework_task_candidate_element(task)
    payload_candidate = canonical_framework_element(
        indicator,
        p4_fields.get("candidate_element") or reviewer.attributes.framework_candidate_element,
    )
    legal_function = reviewer.attributes.framework_legal_function
    if not claimed or not legal_function:
        return _resolved_review(["FRAMEWORK_ELEMENT_REVIEW_FIELDS"], uncertain_exclusions, reviewer)
    if payload_candidate and candidate_name and payload_candidate != candidate_name:
        return _resolved_error("framework candidate element does not match task candidate element", reviewer, "rerun_framework_reviewer_for_candidate_element")
    if candidate_name and claimed_name and candidate_name != claimed_name:
        return _resolved_error("framework candidate element does not match reviewer claimed element", reviewer, "rerun_framework_reviewer_for_candidate_element")
    if not claimed_code or claimed_code not in allowed_elements:
        return _resolved_rejected("REQUIRED_ELEMENT_MISSING", failed=[claimed], reviewer=reviewer)
    legal_ok, legal_reason = framework_legal_function_supported(indicator, claimed_name or str(claimed), legal_function)
    if not legal_ok:
        return _resolved_rejected("REQUIRED_ELEMENT_MISSING", failed=[legal_reason], reviewer=reviewer)
    if indicator.startswith("P4-"):
        quality_result = _p4_framework_quality_gate(
            indicator,
            claimed_name,
            p4_fields,
            reviewer,
            task,
        )
        if quality_result is not None:
            return quality_result
    focal_form = _framework_focal_form(task, reviewer)
    operative_p4_exception = (
        indicator == "P4-I5"
        and claimed_name == "copyright_exceptions"
        and task is not None
        and _has(
            _fold(task.focal_text),
            ("permitted use", "does not infringe", "is not an infringement"),
        )
    )
    if not operative_p4_exception and focal_form in {
        "definition_only",
        "publication_only",
        "legal_status_only",
        "guide_or_outline_only",
        "cross_reference_only",
        "procedural_only",
        "consequential_amendment",
    }:
        return _resolved_rejected("FRAMEWORK_FOCAL_NOT_INDEPENDENT", failed=["FRAMEWORK_FOCAL_NOT_INDEPENDENT"], reviewer=reviewer)
    if focal_form == "mixed_uncertain":
        return _resolved_review(["FRAMEWORK_FOCAL_INDEPENDENCE_UNCERTAIN"], uncertain_exclusions, reviewer)
    if not _framework_claimed_element_has_focal_support(reviewer, claimed_code):
        return {
            "status": "rejected",
            "result_code": "NO_MATCH",
            "accepted_indicators": [],
            "failed_required_elements": [],
            "uncertain_elements": [],
            "uncertain_exclusions": [],
            "triggered_exclusions": [],
            "focal_role": "supporting_only",
            "record_scope_basis": reviewer.record_scope_basis,
            "focal_uncertainty": "claimed framework element is supported only by supporting context; focal E1 does not independently support it",
        }
    supported = [claimed_code] if element_status.get(claimed_code) == "supported" else []
    uncertain = [code for code in allowed_elements if element_status.get(code) == "uncertain"]
    if triggered:
        return _resolved_rejected("EXCLUSION_TRIGGERED", triggered=triggered, reviewer=reviewer)
    if not supported:
        return _resolved_rejected("REQUIRED_ELEMENT_MISSING", failed=[claimed_code], reviewer=reviewer)
    element_uncertainty = list(uncertain)
    coverage_uncertain = reviewer.attributes.coverage in {None, "", "uncertain"}
    if coverage_uncertain and indicator not in {"P4-I2", "P4-I5", "P4-I6"}:
        element_uncertainty.append("FRAMEWORK_COVERAGE")
    if element_uncertainty or uncertain_exclusions:
        return _resolved_review(element_uncertainty, uncertain_exclusions, reviewer)
    resolved = _resolved_accepted([indicator], reviewer)
    if indicator.startswith("P4-"):
        resolved["p4_framework_element"] = claimed_name
        resolved["p4_framework_facts"] = p4_fields
    return resolved


def _p4_framework_review_fields(reviewer: ReviewerDecision) -> dict:
    for check in reviewer.optional_checks:
        if check.check_code != "P4_FRAMEWORK_ELEMENT":
            continue
        try:
            payload = json.loads(check.reason or "{}")
        except json.JSONDecodeError:
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload
    return {}


def _p4_framework_quality_gate(
    indicator: str,
    element: str,
    fields: dict,
    reviewer: ReviewerDecision,
    task: CandidateTask | None,
) -> dict | None:
    focal = _fold(task.focal_text if task is not None else "")
    title = _fold(task.law_title if task is not None else "")
    evidence_character = str(fields.get("evidence_character") or "")
    remedy_direction = str(fields.get("remedy_direction") or "")
    coverage = str(fields.get("coverage") or reviewer.attributes.coverage or "")

    if evidence_character in {"definition_only", "scope_only", "procedure_only", "cross_reference_only"}:
        return _resolved_rejected(
            "FRAMEWORK_FOCAL_NOT_INDEPENDENT",
            failed=["FRAMEWORK_FOCAL_NOT_INDEPENDENT"],
            reviewer=reviewer,
        )
    if evidence_character in {"", "uncertain"}:
        return _resolved_review(["P4_FRAMEWORK_EVIDENCE_CHARACTER"], [], reviewer)
    if evidence_character == "supporting_or_sectoral" and not (
        indicator == "P4-I10" and coverage == "sectoral"
    ):
        return {
            "status": "rejected",
            "result_code": "NO_MATCH",
            "accepted_indicators": [],
            "failed_required_elements": [],
            "uncertain_elements": [],
            "uncertain_exclusions": [],
            "triggered_exclusions": [],
            "focal_role": "supporting_only",
            "record_scope_basis": reviewer.record_scope_basis,
            "focal_uncertainty": "P4 framework evidence is supporting-only rather than an independent element",
        }

    if indicator == "P4-I2":
        limitation = _p4_remedy_limitation(focal) or _has(
            focal,
            (
                "no injunction",
                "injunction shall not",
                "injunction may not",
                "shall not grant an injunction",
                "damages are limited",
                "damages shall not",
                "no damages",
                "not entitled to damages",
                "account of profits shall not",
                "not entitled to an account of profits",
                "defence to infringement",
                "defense to infringement",
                "not liable",
            ),
        )
        if remedy_direction in {"limits_remedy", "defence_or_immunity"} or limitation:
            return _resolved_rejected(
                "EXCLUSION_TRIGGERED",
                triggered=["LIMITATION_DEFENCE_OR_IMMUNITY"],
                reviewer=reviewer,
            )
        if remedy_direction != "grants_remedy":
            return _resolved_review(["P4_POSITIVE_REMEDY_DIRECTION"], [], reviewer)
        if element == "ordinary_civil_or_administrative_remedies" and _has(
            focal,
            ("interim injunction", "interlocutory injunction", "preliminary injunction", "ex parte relief"),
        ) and not _has(
            focal,
            ("permanent injunction", "final injunction", "damages", "account of profits", "seizure", "destruction", "delivery up"),
        ):
            return _resolved_rejected(
                "REQUIRED_ELEMENT_MISSING",
                failed=["ORDINARY_CIVIL_OR_ADMINISTRATIVE_REMEDIES"],
                reviewer=reviewer,
            )
        if element == "provisional_measures" and not _has(
            focal,
            (
                "interim",
                "interlocutory",
                "preliminary",
                "ex parte",
                "urgent",
                "before final judgment",
                "evidence preservation",
                "property preservation",
            ),
        ):
            return _resolved_rejected(
                "REQUIRED_ELEMENT_MISSING",
                failed=["PROVISIONAL_MEASURES"],
                reviewer=reviewer,
            )

    if indicator == "P4-I5":
        procedural_only = _has(
            focal,
            (
                "inspection of records",
                "record inspection",
                "inspect the register",
                "tribunal procedure",
                "rules of procedure",
                "prescribed form",
                "prescribed manner",
                "may make regulations",
                "application of this act",
                "this act applies",
                "scope of this part",
                "groundless threats proceedings",
            ),
        )
        if procedural_only:
            return _resolved_rejected(
                "EXCLUSION_TRIGGERED",
                triggered=["SCOPE_PROCEDURE_OR_INSPECTION_ONLY"],
                reviewer=reviewer,
            )
        if element == "copyright_framework" and not _has(
            focal,
            (
                "copyright subsists",
                "exclusive right",
                "right to reproduce",
                "right to distribute",
                "right to communicate",
                "making available right",
                "copyright is infringed",
                "infringes copyright",
            ),
        ):
            return _resolved_rejected(
                "REQUIRED_ELEMENT_MISSING",
                failed=["COPYRIGHT_FRAMEWORK"],
                reviewer=reviewer,
            )
        if element == "copyright_exceptions" and not _has(
            focal,
            (
                "fair use",
                "fair dealing",
                "permitted use",
                "does not infringe",
                "is not an infringement",
                "research or study",
                "criticism or review",
                "reporting current events",
                "news reporting",
                "education",
                "library",
                "archive",
                "quotation",
                "accessibility",
                "person with a disability",
                "persons with disabilities",
                "temporary copying",
                "temporary reproduction",
                "parody",
                "satire",
            ),
        ):
            return _resolved_rejected(
                "REQUIRED_ELEMENT_MISSING",
                failed=["COPYRIGHT_EXCEPTIONS"],
                reviewer=reviewer,
            )

    if indicator == "P4-I6":
        copyright_nexus = "copyright" in title or _has(
            focal,
            (
                "copyright",
                "flagrantly infringing online location",
                "online copyright infringement",
                "copyright material",
            ),
        )
        remedy = _has(
            focal,
            (
                "injunction",
                "disable access",
                "disabling access",
                "access disabling",
                "block access",
                "blocking order",
                "damages",
                "remove the infringing",
                "restrain online",
            ),
        )
        if not copyright_nexus:
            return _resolved_rejected(
                "EXCLUSION_TRIGGERED",
                triggered=["NON_COPYRIGHT_WEBSITE_BLOCKING"],
                reviewer=reviewer,
            )
        if not remedy or remedy_direction != "grants_remedy":
            return _resolved_rejected(
                "REQUIRED_ELEMENT_MISSING",
                failed=["ONLINE_REMEDY"],
                reviewer=reviewer,
            )
        if element == "online_civil_or_administrative_remedies" and _has(
            focal,
            ("interim", "temporary", "temporarily", "interlocutory", "preliminary", "urgent", "pending final"),
        ) and not _has(
            focal,
            ("permanent", "final injunction", "damages", "final blocking order"),
        ):
            return _resolved_rejected(
                "REQUIRED_ELEMENT_MISSING",
                failed=["ONLINE_CIVIL_OR_ADMINISTRATIVE_REMEDIES"],
                reviewer=reviewer,
            )
        if element == "online_provisional_measures" and not _has(
            focal,
            ("interim", "temporary", "temporarily", "interlocutory", "preliminary", "urgent", "pending final"),
        ):
            return _resolved_rejected(
                "REQUIRED_ELEMENT_MISSING",
                failed=["ONLINE_PROVISIONAL_MEASURES"],
                reviewer=reviewer,
            )

    if indicator == "P4-I10":
        private_protection = fields.get("protected_private_or_commercial_information")
        unauthorised_conduct = fields.get("unauthorised_acquisition_use_or_disclosure")
        government_only = fields.get("government_or_official_only")
        if government_only is True:
            return _resolved_rejected(
                "EXCLUSION_TRIGGERED",
                triggered=["STATE_SECRET_OR_OFFICIAL_CONFIDENTIALITY_ONLY"],
                reviewer=reviewer,
            )
        if element == "statutory_trade_secret_protection":
            if private_protection is False or unauthorised_conduct is False:
                return _resolved_rejected(
                    "REQUIRED_ELEMENT_MISSING",
                    failed=["PRIVATE_COMMERCIAL_SECRET_PROTECTION", "UNAUTHORISED_CONDUCT"],
                    reviewer=reviewer,
                )
            if private_protection is None or unauthorised_conduct is None or government_only is None:
                return _resolved_review(["P4_TRADE_SECRET_PROTECTION_SCOPE"], [], reviewer)
        if element == "common_law_or_case_law_protection" and not _has(
            focal,
            ("breach of confidence", "common law", "equity", "equitable"),
        ):
            return _resolved_rejected(
                "REQUIRED_ELEMENT_MISSING",
                failed=["COMMON_LAW_OR_CASE_LAW_PROTECTION"],
                reviewer=reviewer,
            )
        if element == "trade_secret_remedies":
            linked_secret = _has(
                focal,
                (
                    "trade secret",
                    "confidential business information",
                    "proprietary information",
                    "commercially valuable information",
                    "breach of confidence",
                ),
            )
            actual_remedy = _has(
                focal,
                ("injunction", "damages", "account of profits", "delivery up", "destruction", "liable", "penalty"),
            )
            if not linked_secret or not actual_remedy:
                return _resolved_rejected(
                    "REQUIRED_ELEMENT_MISSING",
                    failed=["TRADE_SECRET_REMEDIES"],
                    reviewer=reviewer,
                )
    return None


def _framework_focal_form(task: CandidateTask | None, reviewer: ReviewerDecision) -> str:
    """Classify whether the focal clause can stand alone as framework evidence.

    This is intentionally deterministic and narrow.  It reads only the focal
    legal text/heading and reviewer element structure; it does not interpret
    free-text mapping rationale or confidence.
    """

    if task is None:
        return ""
    focal = _fold(task.focal_text)
    heading = _fold(task.section_heading)
    blob = f"{heading} {focal}".strip()
    if not blob:
        return "mixed_uncertain"

    substantive = _has(
        focal,
        (
            "must ",
            "shall ",
            "is required to",
            "must not",
            "shall not",
            "may direct",
            "may require",
            "may conduct",
            "may investigate",
            "may make an assessment",
            "must notify",
            "must report",
            "must comply",
            "civil penalty",
        ),
    )
    if _has(heading, ("guide to this part", "simplified outline")) or re.search(r"\b(?:guide|simplified outline)\b", heading):
        return "guide_or_outline_only" if not substantive else "mixed_uncertain"
    if re.fullmatch(r".{0,120}\bis not a legislative instrument\.?(?:\s+\w.*)?", focal) or (
        "not a legislative instrument" in focal and not substantive
    ):
        return "legal_status_only"
    if re.match(r"^\s*(?:\(\d+\)\s*)?(?:in this|for the purposes of|a |an |the )?[\w/ -]{2,80}\s+(?:means|includes|is)\b", focal) and not substantive:
        return "definition_only"
    if _has(focal, ("may publish information", "publish information relating to", "publish the information", "make public")) and not _has(
        focal, ("must publish", "must notify", "must report", "may conduct", "may direct", "may require")
    ):
        return "publication_only"
    if _has(focal, ("see section", "see subsection", "has the meaning given by", "within the meaning of")) and not substantive:
        return "cross_reference_only"
    if _has(focal, ("commences", "application provision", "transitional", "consequential amendment")) and not substantive:
        return "consequential_amendment"
    return ""


def _framework_claimed_element_has_focal_support(reviewer: ReviewerDecision, claimed_code: str) -> bool:
    for item in reviewer.element_assessments:
        if item.element_code == claimed_code:
            return "E1" in item.evidence_ids
    return False


def _framework_task_candidate_element(task: CandidateTask | None) -> str:
    if task is None:
        return ""
    for pattern in task.matched_patterns:
        if pattern.startswith("framework_element:"):
            return pattern.split(":", 1)[1].strip()
    parts = task.task_id.split(":")
    return parts[-2].strip() if len(parts) >= 2 else ""


def _resolve_treaty_fact_matrix(
    allowed_elements: tuple[str, ...],
    allowed_exclusions: tuple[str, ...],
    element_status: dict[str, str],
    exclusion_status: dict[str, str],
    reviewer: ReviewerDecision,
) -> dict:
    triggered = [code for code in allowed_exclusions if exclusion_status.get(code) == "triggered"]
    uncertain_exclusions = [code for code in allowed_exclusions if exclusion_status.get(code) == "uncertain"]
    failed = [code for code in allowed_elements if element_status.get(code) in {None, "not_supported"}]
    uncertain = [code for code in allowed_elements if element_status.get(code) == "uncertain"]
    if triggered:
        return _resolved_rejected("EXCLUSION_TRIGGERED", triggered=triggered, reviewer=reviewer)
    if failed:
        return _resolved_rejected("REQUIRED_ELEMENT_MISSING", failed=failed, reviewer=reviewer)
    if uncertain or uncertain_exclusions:
        return _resolved_review(uncertain, uncertain_exclusions, reviewer)
    return _resolved_accepted(["P6-I5"], reviewer)


def _resolve_p6_fact_matrix(
    record: dict,
    elements: dict[str, str],
    exclusions: dict[str, str],
    reviewer: ReviewerDecision,
    task: CandidateTask | None = None,
) -> dict:
    elements = dict(elements)
    exclusions = dict(exclusions)
    text = _record_text(record)
    if _looks_non_data_asset_transfer(text):
        elements["INFORMATION_BEARING_OBJECT"] = "not_supported"
        exclusions["NON_DATA_ASSET_TRANSFER"] = "triggered"
    if _looks_storage_only(text):
        if elements.get("MANDATORY_DOMESTIC_PROCESSING") == "supported":
            elements["MANDATORY_DOMESTIC_PROCESSING"] = "not_supported"
        if elements.get("ABSOLUTE_TRANSFER_PROHIBITION") == "supported" and not _has(text, ("must not transfer", "shall not transfer", "not transfer outside", "not be transferred outside")):
            elements["ABSOLUTE_TRANSFER_PROHIBITION"] = "not_supported"
        if elements.get("CONDITIONAL_TRANSFER_PATH") == "supported" and not _has(text, ("consent", "adequacy", "comparable protection", "safeguard", "binding corporate", "certification", "approval", "transfer assessment", "prescribed requirement")):
            elements["CONDITIONAL_TRANSFER_PATH"] = "not_supported"
    if _looks_third_party_cdd_reliance(text):
        elements["CONDITIONAL_TRANSFER_PATH"] = "not_supported"
    if not _p6_i2_focal_storage_supported(record, task):
        if elements.get("LOCAL_STORAGE_ACTION") == "supported" or elements.get("EXPLICIT_DOMESTIC_STORAGE_LOCATION") == "supported":
            elements["LOCAL_STORAGE_ACTION"] = "not_supported"
            elements["EXPLICIT_DOMESTIC_STORAGE_LOCATION"] = "not_supported"
    if _p6_i4_law_enforcement_exception_only(record, elements):
        exclusions["LAW_ENFORCEMENT_COOPERATION"] = "not_triggered"
    hard_exclusions = (
        "GOVERNMENT_COOPERATION",
        "LAW_ENFORCEMENT_COOPERATION",
        "REGULATORY_COOPERATION",
        "DEVICE_DISPOSAL_OR_DATA_ERASURE",
        "NON_DATA_ASSET_TRANSFER",
        "CONFIDENTIALITY_ONLY",
        "GOVERNMENT_INTERNAL_ONLY",
    )
    triggered = [code for code in hard_exclusions if exclusions.get(code) == "triggered"]
    uncertain_exclusions = [code for code in hard_exclusions if exclusions.get(code) == "uncertain"]
    if triggered:
        return _resolved_rejected("EXCLUSION_TRIGGERED", triggered=triggered, reviewer=reviewer)

    accepted: list[str] = []
    indicator_from_record = str(record.get("indicator") or "")
    all_p6_candidates = ("P6-I1", "P6-I2", "P6-I3", "P6-I4")
    candidates = (indicator_from_record,) if indicator_from_record in all_p6_candidates else all_p6_candidates
    required_by_indicator = {indicator: required_review_elements(indicator) for indicator in all_p6_candidates}
    for indicator in candidates:
        req = required_by_indicator[indicator]
        if not all(elements.get(code) == "supported" for code in req):
            continue
        # A shared route may contain context for several P6 indicators.  A
        # different subsection cannot supply a focal-required transfer or
        # storage element for the current submission row.
        focal_missing = _required_focal_support_missing(indicator, req, reviewer)
        if focal_missing:
            continue
        if indicator == "P6-I1":
            location = elements.get("CROSS_BORDER_NEXUS") == "supported" or elements.get("MANDATORY_DOMESTIC_PROCESSING") == "supported"
            prohibition = elements.get("ABSOLUTE_TRANSFER_PROHIBITION") == "supported" or elements.get("MANDATORY_DOMESTIC_PROCESSING") == "supported"
            if location and prohibition and elements.get("ORDINARY_COMPLIANCE_PATH") != "supported":
                accepted.append(indicator)
        elif indicator == "P6-I4":
            if elements.get("CONDITIONAL_TRANSFER_PATH") == "supported":
                accepted.append(indicator)
        else:
            accepted.append(indicator)

    if accepted:
        accepted = _dedupe_p6_indicators(accepted, record)
        return _resolved_accepted(accepted, reviewer)

    focal_missing = [
        code
        for indicator in candidates
        for code in _required_focal_support_missing(indicator, required_by_indicator[indicator], reviewer)
    ]
    if focal_missing:
        return {
            "status": "rejected",
            "result_code": "NO_MATCH",
            "accepted_indicators": [],
            "failed_required_elements": sorted(set(focal_missing)),
            "uncertain_elements": [],
            "uncertain_exclusions": [],
            "triggered_exclusions": [],
            "focal_role": "supporting_only",
            "record_scope_basis": reviewer.record_scope_basis,
            "focal_uncertainty": "focal-required element is supported only by context",
        }

    relevant_required = sorted(set(code for indicator in candidates for code in required_by_indicator[indicator]))
    failed = [code for code in relevant_required if elements.get(code) == "not_supported"]
    uncertain = [code for code in relevant_required if elements.get(code) == "uncertain"]
    if uncertain or uncertain_exclusions or reviewer.focal_integrity.status in {"uncertain", "incomplete_context"} or reviewer.focal_role == "uncertain":
        return _resolved_review(uncertain, uncertain_exclusions, reviewer)
    return _resolved_rejected("REQUIRED_ELEMENT_MISSING", failed=failed, reviewer=reviewer)


def _p6_i4_law_enforcement_exception_only(record: dict, elements: dict[str, str]) -> bool:
    indicator = str(record.get("indicator") or "")
    if indicator and indicator != "P6-I4":
        return False
    if elements.get("CONDITIONAL_TRANSFER_PATH") != "supported" or elements.get("CROSS_BORDER_NEXUS") != "supported":
        return False
    text = _record_text(record)
    general_transfer = _has(text, ("personal information", "data", "records")) and _has(
        text,
        ("overseas recipient", "not in australia", "outside", "cross-border", "discloses", "disclose", "transfer"),
    )
    exception_markers = _has(text, ("does not apply", "exception", "permitted general situation", "law enforcement", "enforcement related activities", "international agreement"))
    pure_enforcement = _has(text, ("law enforcement", "enforcement related activities")) and not _has(text, ("overseas recipient", "reasonable steps"))
    return general_transfer and exception_markers and not pure_enforcement


def _required_for_indicator(indicator: str, route_topic: str, element_status: dict[str, str] | None = None) -> tuple[str, ...]:
    centralized = required_review_elements(indicator)
    if centralized and indicator != "P7-I4":
        return centralized
    if indicator == "P7-I3":
        return P7_REVIEW_ELEMENTS_BY_GROUP["P7_RETENTION"]
    if indicator == "P7-I4":
        base = ("OPERATIVE_RULE", "MANDATORY_DESIGNATION_OR_ASSESSMENT", "PRIVACY_COMPLIANCE_FUNCTION")
        statuses = element_status or {}
        if statuses.get("DPO_PATH") == "supported" or statuses.get("DPIA_PATH") == "supported":
            return base
        return (*base, "DPO_PATH", "DPIA_PATH")
    if indicator == "P7-I5":
        return P7_REVIEW_ELEMENTS_BY_GROUP["P7_GOVERNMENT_ACCESS"]
    return P7_REVIEW_ELEMENTS_BY_GROUP.get(route_topic, tuple())


def _apply_retention_scope_basis(element_status: dict[str, str], reviewer: ReviewerDecision) -> None:
    accepted = {
        "PERSONAL_CUSTOMER_USER",
        "ACCOUNT_PAYMENT_TRANSACTION",
        "AML_KYC",
        "ACCOUNTING_TAX_FINANCIAL",
        "COMMUNICATIONS_PLATFORM_DIGITAL_SERVICE",
        "AUTHENTICATION_CYBERSECURITY_SYSTEM_EVENT",
        "PERSON_OR_TRANSACTION_TRACEABILITY",
        "OPERATIONAL_SECTOR_RECORD",
    }
    rejected = {"PHYSICAL_OPERATIONAL_ONLY", "NONE"}
    if reviewer.record_scope_basis in accepted:
        return
    if reviewer.record_scope_basis in rejected:
        element_status["IN_SCOPE_RECORD_TYPE"] = "not_supported"
    elif reviewer.record_scope_basis == "UNCERTAIN":
        element_status["IN_SCOPE_RECORD_TYPE"] = "uncertain"


def _reconcile_accountability_path(reviewer: ReviewerDecision, element_status: dict[str, str]) -> ReviewerDecision:
    dpo = element_status.get("DPO_PATH") == "supported"
    dpia = element_status.get("DPIA_PATH") == "supported"
    if dpo and dpia:
        path = "dpo_and_dpia"
    elif dpo:
        path = "dpo"
    elif dpia:
        path = "dpia"
    else:
        path = "uncertain"
    if reviewer.attributes.accountability_path != path:
        checks = list(reviewer.optional_checks)
        checks.append(
            ReviewerOptionalCheck(
                check_code="ACCOUNTABILITY_PATH_RECONCILED",
                status="applied",
                evidence_ids=[],
                reason=f"Authoritative accountability_path set from supported DPO/DPIA elements: {path}.",
            )
        )
        return reviewer.model_copy(update={"attributes": reviewer.attributes.model_copy(update={"accountability_path": path}), "optional_checks": checks})
    return reviewer


def _is_true_source_mismatch(reason: str | None) -> bool:
    text = _fold(reason or "")
    technical_terms = (
        "focal id",
        "focal provision id",
        "does not correspond",
        "cannot locate",
        "not found in source",
        "not in source",
        "different section",
        "different provision",
        "evidence text is absent",
        "source text missing",
        "parser",
    )
    legal_mismatch_terms = (
        "proposed mapping",
        "frames the clause",
        "different rule type",
        "not retention",
        "not a retention",
        "not a data",
        "not the proposed",
    )
    if _has(text, legal_mismatch_terms):
        return False
    return _has(text, technical_terms)


def _apply_retention_non_record_rules(element_status: dict[str, str], task: CandidateTask | None) -> None:
    """Reject audit cadence and review-frequency clauses as P7-I3 retention."""

    if task is None:
        return
    text = _fold(f"{task.law_title} {task.focal_provision_id} {task.focal_text} {task.supporting_context}")
    focal = _fold(task.focal_text)
    retention_action = _has_retention_action(focal)
    retention_period_rule = _has_retention_period_rule(focal)
    transfer_or_delivery_deadline = _has(
        focal,
        (
            "deliver",
            "deliver such",
            "submit",
            "furnish",
            "lodge",
            "return",
            "surrender",
            "hand over",
            "send to",
            "notify",
            "notification",
            "within",
        ),
    ) and not retention_action and not retention_period_rule
    if not retention_action and not retention_period_rule or transfer_or_delivery_deadline:
        element_status["MANDATORY_RETENTION_ACTION"] = "not_supported"
        if transfer_or_delivery_deadline:
            element_status["MINIMUM_RETENTION_DURATION"] = "not_supported"
    audit_cadence = _has(text, ("audit", "auditor", "audited")) and _has(
        text,
        (
            "frequency",
            "once every",
            "at least once every",
            "not less than once every",
            "every 2 years",
            "every two years",
            "periodic audit",
        ),
    )
    retention_action = _has(
        text,
        (
            "retain records",
            "retain the records",
            "keep records",
            "keep the records",
            "preserve records",
            "maintain records",
            "records must be retained",
            "records shall be retained",
            "records must be kept",
            "records shall be kept",
        ),
    )
    if audit_cadence and not retention_action:
        element_status["MANDATORY_RETENTION_ACTION"] = "not_supported"
        element_status["MINIMUM_RETENTION_DURATION"] = "not_supported"
        element_status["IN_SCOPE_RECORD_TYPE"] = "not_supported"
    electoral_consequence = _has(text, ("ensuing year", "name must be removed", "name is deleted", "delete the name", "deleted from the register")) and not retention_action
    if electoral_consequence:
        element_status["MANDATORY_RETENTION_ACTION"] = "not_supported"
        element_status["MINIMUM_RETENTION_DURATION"] = "not_supported"
    government_internal = _has(
        text,
        (
            "government data",
            "government record",
            "public administration",
            "public officer",
            "public service",
            "ministry",
            "department",
            "statutory board",
        ),
    ) and _has(text, ("internal", "official record", "administration", "public administration"))
    if government_internal and not _has(text, ("personal data", "customer", "account", "transaction", "subscriber", "user")):
        element_status["IN_SCOPE_RECORD_TYPE"] = "not_supported"


def _apply_accountability_context_rules(
    element_status: dict[str, str],
    exclusion_status: dict[str, str],
    reviewer: ReviewerDecision,
    task: CandidateTask | None,
) -> ReviewerDecision:
    """Deterministically classify functional DPO clauses in data-protection statutes.

    This avoids the stale-cache failure mode where a provision such as PDPA s 11(3)
    was treated as a generic compliance officer despite the focal text requiring
    designation of a person responsible for compliance with a personal-data law.
    """

    if task is None:
        return reviewer
    focal = _fold(task.focal_text)
    context = _fold(f"{task.law_title} {task.section_heading} {task.supporting_context}")
    designation = _has(focal, ("must designate", "shall designate", "must appoint", "shall appoint", "designate one or more individuals", "appoint one or more individuals"))
    privacy_scope = _has(focal, ("personal data", "data protection", "privacy", "personal-data", "protection act")) or _has(context, ("personal data", "data protection", "privacy", "personal-data", "protection act"))
    compliance_function = _has(focal, ("responsible for ensuring", "ensure that", "complies with this act", "compliance with this act", "compliance with the act"))
    if designation and privacy_scope and compliance_function:
        element_status["MANDATORY_DESIGNATION_OR_ASSESSMENT"] = "supported"
        element_status["PRIVACY_COMPLIANCE_FUNCTION"] = "supported"
        element_status["DPO_PATH"] = "supported"
        exclusion_status["GENERAL_COMPLIANCE_ROLE"] = "not_triggered"
        if reviewer.attributes.accountability_path in {None, "uncertain"}:
            return reviewer.model_copy(update={"attributes": reviewer.attributes.model_copy(update={"accountability_path": "dpo"})})
    return reviewer


def _has_retention_action(text: str) -> bool:
    return _has(
        text,
        (
            "keep",
            "kept",
            "retain",
            "retained",
            "preserve",
            "preserved",
            "maintain",
            "maintained",
            "store",
            "stored",
            "must be kept",
            "shall be kept",
            "must be retained",
            "shall be retained",
            "must be preserved",
            "shall be preserved",
            "must be maintained",
            "shall be maintained",
        ),
    )


def _has_retention_period_rule(text: str) -> bool:
    period_rule = _has(
        text,
        (
            "prescribed period",
            "applicable period",
            "minimum period",
            "period to keep",
            "period to retain",
            "period to preserve",
            "period of retention",
            "must be kept",
            "must be retained",
            "shall be kept",
            "shall be retained",
        ),
    )
    record_object = _has(text, ("record", "records", "document", "documents", "information", "books", "register", "registers"))
    duration = _has(text, ("year", "years", "month", "months", "day", "days", "period"))
    return period_rule and record_object and duration


def _required_focal_support_missing(indicator: str, required: tuple[str, ...], reviewer: ReviewerDecision) -> list[str]:
    focal_ids = {"E1", "S1"}
    core = set(focal_required_elements(indicator))
    # P6 resolver uses the same evidence IDs but its element list is shared by
    # a route; do not require non-candidate elements here.
    if not core:
        core = set(required)
    missing: list[str] = []
    by_code = {item.element_code: item for item in reviewer.element_assessments}
    for code in required:
        if code not in core:
            continue
        item = by_code.get(code)
        if item is None or item.status != "supported":
            continue
        if not (set(item.evidence_ids) & focal_ids):
            missing.append(code)
    return missing


def _indicator_focal_acceptance_gate(indicator: str, task: CandidateTask | None) -> str | None:
    """Require the focal clause itself to carry the indicator's core legal function.

    Supporting context can explain scope, definitions, or trigger details, but it
    must not supply the core obligation for an independently accepted submission
    row.
    """

    if task is None:
        return None
    focal = _fold(task.focal_text)
    if not focal:
        return "FOCAL_TEXT_MISSING"
    if indicator == "P7-I3":
        operative_focal = _strip_editorial_notes(focal)
        has_retention_obligation = _has_explicit_retention_obligation(operative_focal)
        has_record_object = _has(
            operative_focal,
            (
                "record",
                "records",
                "document",
                "documents",
                "books",
                "register",
                "registers",
                "information",
                "data",
                "account",
                "accounts",
                "transaction",
                "transactions",
            ),
        )
        has_duration_or_trigger = _has_retention_period_rule(operative_focal) or _has_concrete_duration(operative_focal) or _has(
            operative_focal,
            (
                "minimum period",
                "prescribed period",
                "applicable period",
                "retention period",
            ),
        )
        access_or_delivery_only = _has(
            operative_focal,
            (
                "produce",
                "make available",
                "provide",
                "submit",
                "furnish",
                "give to",
                "deliver",
                "surrender",
                "return",
                "inspect",
                "access",
            ),
        ) and not has_retention_obligation
        notice_duration_only = _has(operative_focal, ("notice is in force", "notice remains in force", "order is in force")) and not has_retention_obligation
        if access_or_delivery_only or notice_duration_only:
            return "FOCAL_RETENTION_OBLIGATION_MISSING"
        if not (has_retention_obligation and has_record_object and has_duration_or_trigger):
            return "FOCAL_RETENTION_CORE_ELEMENTS_MISSING"
    elif indicator == "P7-I4":
        dpo_obligation = _has(
            focal,
            (
                "must designate",
                "shall designate",
                "must appoint",
                "shall appoint",
                "is to designate",
                "is to appoint",
                "designate one or more",
                "appoint one or more",
                "data protection officer",
                "privacy officer",
            ),
        ) and _has(focal, ("responsible", "compliance", "privacy", "personal data", "personal information", "data protection"))
        dpia_obligation = _has(
            focal,
            (
                "privacy impact assessment",
                "data protection impact assessment",
                "impact assessment",
                "must conduct",
                "shall conduct",
                "must carry out",
                "shall carry out",
                "may direct",
                "must give the commissioner",
                "is required to conduct",
            ),
        ) and _has(focal, ("assessment", "impact", "privacy", "data protection", "personal information"))
        definition_only = (
            _has(focal, ("means", "definition", "is a written assessment"))
            and not _has(focal, ("must", "shall", "required to", "may direct", "must conduct", "shall conduct"))
        )
        if definition_only or not (dpo_obligation or dpia_obligation):
            return "FOCAL_ACCOUNTABILITY_OBLIGATION_MISSING"
    elif indicator == "P6-I4":
        economy_foreign_terms = foreign_terms(task.economy)
        cross_border = _has(
            focal,
            (
                *economy_foreign_terms,
                "overseas",
                "foreign country",
                "another country",
                "cross-border",
                "cross border",
                "transfer",
                "transferred",
                "disclose",
                "disclosed",
                "send",
                "sent",
                "store outside",
                "stored outside",
                "held outside",
                "processed outside",
                "offshore",
            ),
        )
        condition = _has(
            focal,
            (
                "condition",
                "conditions",
                "approval",
                "approved",
                "authorisation",
                "authorization",
                "consent",
                "permitted",
                "unless",
                "exception",
                "safeguard",
                "comparable protection",
                "binding",
                "adequate",
                "adequacy",
                "prescribed",
                "must not",
                "may only",
                "if",
            ),
        )
        if not (cross_border and condition):
            return "FOCAL_CROSS_BORDER_CONDITION_MISSING"
    return None


def _strip_editorial_notes(text: str) -> str:
    return re.split(r"\bnote\s*:", text, maxsplit=1, flags=re.I)[0].strip()


def _has_explicit_retention_obligation(focal: str) -> bool:
    return bool(
        re.search(
            r"\b(?:must|shall|is required to|are required to)\s+(?:keep|retain|preserve|maintain|store)\b",
            focal,
        )
        or re.search(
            r"\b(?:records?|documents?|books?|registers?|information|data|accounts?)\s+(?:must|shall)\s+be\s+(?:kept|retained|preserved|maintained|stored)\b",
            focal,
        )
        or re.search(
            r"\b(?:keep|retain|preserve|maintain|store)\s+(?:the\s+)?(?:records?|documents?|books?|registers?|information|data|accounts?)\b",
            focal,
        )
    )


def _suppress_data_protection_general_compliance_exclusion(
    element_status: dict[str, str],
    exclusion_status: dict[str, str],
) -> None:
    if (
        exclusion_status.get("GENERAL_COMPLIANCE_ROLE") == "triggered"
        and element_status.get("PRIVACY_COMPLIANCE_FUNCTION") == "supported"
        and element_status.get("DPO_PATH") == "supported"
    ):
        exclusion_status["GENERAL_COMPLIANCE_ROLE"] = "not_triggered"


def _apply_government_access_context_rules(reviewer: ReviewerDecision, task: CandidateTask | None) -> ReviewerDecision:
    if task is None:
        return reviewer
    text = _fold(f"{task.law_title} {task.focal_text} {task.supporting_context}")
    if "public prosecutor" in text and "court order" not in text and "order of court" not in text and reviewer.attributes.judicial_authorization == "required":
        return reviewer.model_copy(update={"attributes": reviewer.attributes.model_copy(update={"judicial_authorization": "not_required"})})
    return reviewer


def _p7i5_personal_data_uncertain(task: CandidateTask | None) -> bool:
    """Catch government-access claims where the object is only a generic customer/info term.

    This is deliberately narrow. It does not reject all "customer information"
    clauses; it only prevents accepted P7-I5 where the available focal/supporting
    text does not establish that the accessed object is personal or identifiable
    data.
    """

    if task is None:
        return False
    text = _fold(f"{task.law_title} {task.focal_text} {task.supporting_context}")
    generic_customer_info = _has(
        text,
        (
            "customer information",
            "customer records",
            "client information",
            "client records",
        ),
    )
    explicit_personal_scope = _has(
        text,
        (
            "personal data",
            "personal information",
            "identifiable",
            "identity card",
            "identification",
            "natural person",
            "individual",
            "subscriber information",
            "patient information",
        ),
    )
    if generic_customer_info and not explicit_personal_scope:
        return True
    return False


def _missing_retention_attributes(reviewer: ReviewerDecision) -> list[str]:
    missing: list[str] = []
    value = reviewer.attributes.minimum_duration_value
    unit = reviewer.attributes.minimum_duration_unit
    trigger = reviewer.attributes.trigger_event
    if not value:
        missing.append("MINIMUM_DURATION_VALUE")
    if not unit:
        missing.append("MINIMUM_DURATION_UNIT")
    if not trigger:
        missing.append("TRIGGER_EVENT")
    concrete_duration = _has_concrete_duration(f"{value or ''} {unit or ''}")
    if value or unit:
        duration_text = _fold(f"{value or ''} {unit or ''}")
        indeterminate = (
            "prescribed period",
            "prescribed duration",
            "such period",
            "specified period",
            "as prescribed",
            "period prescribed",
            "not specified",
            "unspecified",
            "unknown",
            "uncertain",
            "cannot determine",
            "not determinable",
        )
        if (not concrete_duration and _has(duration_text, indeterminate)) or (duration_text.strip() in {"period", "duration"}):
            missing.append("CALCULABLE_DURATION")
    if trigger:
        trigger_text = _fold(trigger)
        if _has(trigger_text, ("uncertain", "unknown", "not specified", "cannot determine", "not determinable")):
            missing.append("TRIGGER_EVENT")
    return missing


def _has_concrete_duration(value: str | None) -> bool:
    text = _fold(value or "")
    if not text:
        return False
    numeric = re.search(r"\b\d+\b", text) is not None or _has(
        text,
        (
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
        ),
    )
    unit = _has(text, ("day", "days", "month", "months", "year", "years", "hour", "hours"))
    return bool(numeric and unit)


def _deterministic_contradiction(
    indicator: str,
    record: dict,
    reviewer: ReviewerDecision,
    task: CandidateTask | None,
) -> str | None:
    text = _fold(
        " ".join(
            str(value or "")
            for value in (
                record.get("rationale"),
                reviewer.review_reason,
                reviewer.focal_integrity.reason,
            )
        )
    )
    if _explicit_mapping_contradiction(text, indicator):
        return "RATIONALE_ATTRIBUTE_CONTRADICTION"
    if indicator == "P7-I3" and _missing_retention_attributes(reviewer):
        return "RETENTION_ATTRIBUTE_MISSING"
    if indicator == "P7-I4" and reviewer.attributes.accountability_path in {None, "uncertain"}:
        return "ACCOUNTABILITY_PATH_MISSING"
    if indicator == "P7-I5" and reviewer.attributes.judicial_authorization in {None, "uncertain"}:
        return "JUDICIAL_AUTHORIZATION_MISSING"
    return None


def _explicit_mapping_contradiction(rationale: str | None, indicator_id: str | None = None) -> bool:
    """Return True only for explicit negative mapping conclusions.

    This deliberately ignores standalone words such as "unclear",
    "insufficient", or "unable" unless the same sentence also negates the
    current provision/evidence/text as satisfying, supporting, establishing, or
    mapping to the indicator or a required element.
    """

    text = _fold(rationale or "")
    if not text:
        return False
    indicator = _fold(indicator_id or "")
    sentences = [part.strip() for part in re.split(r"(?<=[.!?;])\s+|\n+", text) if part.strip()]
    if not sentences:
        sentences = [text]
    for sentence in sentences:
        if _sentence_explicit_mapping_contradiction(sentence, indicator):
            return True
    return False


def _sentence_explicit_mapping_contradiction(sentence: str, indicator: str) -> bool:
    sentence = re.sub(r"\bnot\s+(?:less|more|later|earlier)\s+than\b", "duration-comparator", sentence)
    subject = (
        r"(?:this|the)\s+(?:provision|clause|section|evidence|text|record|measure)"
        r"|(?:the|this)\s+mapping"
        r"|(?:the|this)\s+indicator"
        r"|(?:evidence|text)\s+is"
    )
    mapping_object = (
        r"(?:satisf(?:y|ies)|meet(?:s)?|support(?:s)?|establish(?:es)?|map(?:s)?(?:\s+to)?|mapping\s+to|"
        r"qualif(?:y|ies)\s+as|confirmed\s+as|conclude\s+that)"
    )
    indicator_or_element = (
        r"(?:the\s+)?indicator|p6-i[1-5]|p7-i[1-5]|required\s+element|mapping\s+conclusion|"
        r"mandatory\s+(?:minimum\s+)?retention\s+period|retention\s+requirement|"
        r"accountability\s+obligation|dpo|dpia|cross[- ]border|cybersecurity\s+framework|cyber\s+security\s+framework"
    )
    if indicator:
        indicator_or_element = f"(?:{indicator_or_element}|{re.escape(indicator)})"
    subject_group = f"(?:{subject})"
    mapping_group = f"(?:{mapping_object})"
    object_group = f"(?:{indicator_or_element})"
    positive_supporting_context_negation = (
        "not merely supporting context",
        "not merely a supporting context",
        "not just contextual support",
        "not just context",
        "not merely supporting",
        "not just supporting",
        "not only supporting",
    )
    if _has(sentence, positive_supporting_context_negation):
        return False
    explicit_negative_patterns = (
        rf"{subject_group}.{{0,120}}\b(?:does\s+not|do\s+not|did\s+not|cannot|can't|doesn't|is\s+not|are\s+not|not)\b.{{0,120}}\b{mapping_group}\b",
        rf"{subject_group}.{{0,120}}\b(?:does\s+not|do\s+not|did\s+not|cannot|can't|doesn't|is\s+not|are\s+not|not)\b.{{0,120}}{object_group}",
        rf"\b(?:insufficient\s+(?:evidence|text)\s+to\s+conclude|insufficient\s+to\s+conclude|unable\s+to\s+determine|cannot\s+confirm|cannot\s+be\s+confirmed|not\s+clear)\b.{{0,160}}{subject_group}.{{0,120}}(?:\b{mapping_group}\b|{object_group})",
        rf"{subject_group}.{{0,160}}\b(?:insufficient\s+(?:evidence|text)\s+to\s+conclude|insufficient\s+to\s+conclude|unable\s+to\s+determine|cannot\s+confirm|cannot\s+be\s+confirmed|not\s+clear)\b.{{0,160}}(?:\b{mapping_group}\b|{object_group})",
        rf"\b(?:this|the)\b.{{0,80}}\b(?:cannot|can't)\s+be\s+confirmed\s+as\b.{{0,80}}{object_group}",
        rf"\bthe\s+text\s+does\s+not\s+support\s+mapping\s+to\b.{{0,80}}{object_group}",
    )
    return any(re.search(pattern, sentence) for pattern in explicit_negative_patterns)


def _p6_i2_focal_storage_supported(record: dict, task: CandidateTask | None) -> bool:
    indicator = str(record.get("indicator") or "")
    if indicator and indicator != "P6-I2":
        return True
    quote = _fold(str(record.get("quote") or ""))
    focal_text = _fold(task.focal_text if task is not None else "")
    evidence_ids = set(record.get("evidence_ids") or [])
    focal_included = "S1" in evidence_ids
    storage_terms = ("keep", "kept", "store", "stored", "maintain", "maintained", "retain", "retained", "preserve", "copy", "duplicate", "sent to and kept")
    economy = task.economy if task is not None else "Singapore"
    base_location_terms = domestic_terms(economy)
    location_terms = tuple(
        dict.fromkeys(
            [
                *base_location_terms,
                *(f"at a place {term}" for term in base_location_terms),
                *(f"kept {term}" for term in base_location_terms),
                *(f"stored {term}" for term in base_location_terms),
                *(f"maintained {term}" for term in base_location_terms),
                *(f"retained {term}" for term in base_location_terms),
            ]
        )
    )
    storage = _has(quote, storage_terms)
    singapore_location = _has(quote, location_terms)
    focal_storage = _has(focal_text, storage_terms)
    focal_location = _has(focal_text, location_terms)
    information_object = _has(focal_text or quote, ("record", "records", "book", "books", "data", "information", "copy", "copies", "document", "documents", "register"))
    false_location = _has(
        focal_text or quote,
        (
            "published in",
            "accessible in",
            "made available in",
            "online location",
            "website",
            "portal",
            "address in singapore",
            "address for service in singapore",
            "resident in singapore",
            "persons in singapore",
            "person in singapore",
            "patient in singapore",
            "patients in singapore",
            "live birth in singapore",
            "birth by a patient in singapore",
            "births by patients in singapore",
            "diagnosed with",
            "treated for",
            "treated in singapore",
            "diagnosed and treated in singapore",
            "places where registers are kept",
            "places at which registers are kept",
            "places at which copies of those registers are kept",
            "records of the places at which",
            "place where the register is kept",
            "where registers are kept",
            "in singapore may inspect",
        ),
    )
    reporting_only = _has(quote, ("submit", "submitted", "furnish", "furnished", "lodge", "lodged", "deliver", "delivered", "register with", "notify", "notification", "internal control")) and not storage
    if reporting_only or false_location or not information_object:
        return False
    if focal_included and focal_storage and focal_location:
        return True
    return False


def _resolved_accepted(indicators: list[str], reviewer: ReviewerDecision) -> dict:
    return {
        "status": "accepted",
        "result_code": None,
        "accepted_indicators": indicators,
        "failed_required_elements": [],
        "uncertain_elements": [],
        "uncertain_exclusions": [],
        "triggered_exclusions": [],
        "focal_role": reviewer.focal_role,
        "record_scope_basis": reviewer.record_scope_basis,
        "reviewer_attributes": reviewer.attributes,
        "reviewer": reviewer,
    }


def _resolved_rejected(
    result_code: str,
    *,
    failed: list[str] | None = None,
    triggered: list[str] | None = None,
    reviewer: ReviewerDecision,
) -> dict:
    return {
        "status": "rejected",
        "result_code": result_code,
        "accepted_indicators": [],
        "failed_required_elements": failed or [],
        "uncertain_elements": [],
        "uncertain_exclusions": [],
        "triggered_exclusions": triggered or [],
        "focal_role": reviewer.focal_role,
        "record_scope_basis": reviewer.record_scope_basis,
        "reviewer_attributes": reviewer.attributes,
        "reviewer": reviewer,
    }


def _resolved_review(uncertain_elements: list[str], uncertain_exclusions: list[str], reviewer: ReviewerDecision) -> dict:
    focal_uncertainty = reviewer.focal_integrity.reason if reviewer.focal_integrity.status in {"uncertain", "incomplete_context"} or reviewer.focal_role == "uncertain" else None
    return {
        "status": "human_legal_review",
        "result_code": "LEGAL_UNCERTAINTY",
        "accepted_indicators": [],
        "failed_required_elements": [],
        "uncertain_elements": uncertain_elements,
        "uncertain_exclusions": uncertain_exclusions,
        "triggered_exclusions": [],
        "focal_uncertainty": focal_uncertainty,
        "focal_role": reviewer.focal_role,
        "record_scope_basis": reviewer.record_scope_basis,
        "reviewer_attributes": reviewer.attributes,
        "reviewer": reviewer,
    }


def _resolved_error(technical_detail: str, reviewer: ReviewerDecision, expected_repair_action: str) -> dict:
    evidence_ids = sorted(
        set(
            evidence_id
            for item in [*reviewer.element_assessments, *reviewer.exclusion_assessments]
            for evidence_id in item.evidence_ids
        )
    )
    return {
        "status": "technical_repair",
        "result_code": "TECHNICAL_INPUT_ERROR",
        "accepted_indicators": [],
        "failed_required_elements": [],
        "uncertain_elements": [],
        "uncertain_exclusions": [],
        "triggered_exclusions": [],
        "technical_detail": technical_detail or "technical_repair_required",
        "affected_evidence_ids": evidence_ids,
        "expected_repair_action": expected_repair_action,
        "focal_role": reviewer.focal_role,
        "record_scope_basis": reviewer.record_scope_basis,
        "reviewer_attributes": reviewer.attributes,
        "reviewer": reviewer,
    }


def _reviewer_schema_errors(
    reviewer: ReviewerDecision,
    allowed_elements: tuple[str, ...],
    allowed_exclusions: tuple[str, ...],
    task: CandidateTask | None,
) -> list[str]:
    element_codes = [item.element_code for item in reviewer.element_assessments]
    exclusion_codes = [item.exclusion_code for item in reviewer.exclusion_assessments]
    if task is not None and task.route_topic.startswith("P4_") and task.task_kind == "framework_element":
        indicator = str(task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else ""))
        candidate = _framework_task_candidate_element(task)
        candidate_code = framework_element_code(indicator, candidate)
        if not candidate_code or set(element_codes) != {candidate_code}:
            return ["REVIEWER_SCHEMA_ERROR"]
        if set(exclusion_codes) != set(allowed_exclusions):
            return ["REVIEWER_SCHEMA_ERROR"]
    elif task is not None and task.route_topic in {"P7_DATA_PROTECTION_FRAMEWORK", "P7_CYBERSECURITY_FRAMEWORK"}:
        full_allowed = FRAMEWORK_REVIEW_ELEMENTS["P7-I1"] if task.route_topic == "P7_DATA_PROTECTION_FRAMEWORK" else FRAMEWORK_REVIEW_ELEMENTS["P7-I2"]
        if any(code not in full_allowed for code in element_codes):
            return ["REVIEWER_SCHEMA_ERROR"]
        if any(code not in allowed_exclusions for code in exclusion_codes):
            return ["REVIEWER_SCHEMA_ERROR"]
    else:
        if set(element_codes) != set(allowed_elements):
            return ["REVIEWER_SCHEMA_ERROR"]
        if set(exclusion_codes) != set(allowed_exclusions):
            return ["REVIEWER_SCHEMA_ERROR"]
    for item in [*reviewer.element_assessments, *reviewer.exclusion_assessments]:
        status = getattr(item, "status", "")
        if status in {"supported", "triggered"} and not item.evidence_ids:
            return ["REVIEWER_SCHEMA_ERROR"]
    if task is not None:
        valid_ids = set(task.evidence_segments)
        for item in [*reviewer.element_assessments, *reviewer.exclusion_assessments]:
            if any(evidence_id not in valid_ids for evidence_id in item.evidence_ids):
                return ["REVIEWER_SCHEMA_ERROR"]
        if task.route_topic == "P7_RETENTION" and reviewer.record_scope_basis is None:
            return ["REVIEWER_SCHEMA_ERROR"]
    return []


def _quote_from_evidence_ids(task: CandidateTask, evidence_ids: list[str]) -> str | None:
    if not evidence_ids:
        return None
    parts: list[str] = []
    for evidence_id in evidence_ids:
        text = task.evidence_segments.get(evidence_id)
        if not text:
            return None
        if not _contains_normalized(f"{task.focal_text}\n{task.supporting_context}\n{task.parent_section_text}", text):
            return None
        parts.append(text)
    quote = "\n".join(parts).strip()
    return quote or None


def _record_text(record: dict) -> str:
    return _fold(
        " ".join(
            str(record.get(key) or "")
            for key in ("action", "regulated_object", "geographic_nexus", "quote")
        )
    )


def _focal_clause_supporting_only(result: ValidatedTaskResult, record: dict, task: CandidateTask | None) -> str | None:
    indicator = str(record.get("indicator") or result.indicator or "")
    if task is None:
        return None
    focal = _fold(task.focal_text)
    if not focal:
        return "empty focal clause"
    incomplete = _focal_incomplete_fragment(focal)
    if incomplete:
        return incomplete
    penalty_or_contravention = _has(
        focal,
        (
            "contravenes subsection",
            "contravenes paragraph",
            "contravene subsection",
            "shall be guilty of an offence",
            "guilty of an offence",
            "liable on conviction",
            "shall be liable",
            "penalty",
        ),
    )
    if penalty_or_contravention:
        return "focal clause is contravention, penalty, or enforcement consequence; operative obligation is elsewhere"
    if indicator == "P7-I4" and _has(
        focal,
        (
            "does not relieve",
            "contact information",
            "business contact information",
            "must inform the commission",
            "must notify the commission",
        ),
    ) and not _has(focal, ("designate one or more", "appoint one or more", "must designate", "shall designate", "must appoint", "shall appoint")):
        return "focal clause is supporting DPO/contact mechanics, not designation obligation"
    if indicator == "P7-I3" and _has(focal, ("minimum period means", "minimum period specified", "definition of minimum period")) and not _has(
        focal,
        ("must retain", "must keep", "must preserve", "shall retain", "shall keep", "shall preserve", "retain the", "keep the", "preserve the"),
    ):
        return "focal clause only defines a duration term and does not impose a retention obligation"
    if indicator == "P7-I3" and _looks_duration_definition_only(focal):
        return "focal clause only defines a retention period/duration term and does not impose a retention obligation"
    if indicator == "P7-I5" and _has(
        focal,
        (
            "requirements in subsection",
            "contract may",
            "contract shall contain",
            "contract must contain",
            "contract shall be in writing",
            "contract must be in writing",
            "agreement may",
            "agreement must contain",
        ),
    ) and _has(
        focal,
        ("access", "information", "records", "data"),
    ):
        return "focal clause is a contractual supporting term, not the public authority compulsory access power"
    return None


def _looks_duration_definition_only(focal: str) -> bool:
    period_definition = bool(
        re.search(
            r"\b(?:minimum|applicable|prescribed|retention|reporting|relevant)\s+(?:retention\s+)?period\b.{0,160}\bmeans\b",
            focal,
        )
        or re.search(r"\b(?:gaming day|cash[- ]?in|cash[- ]?out)\b.{0,120}\bmeans\b", focal)
    )
    if not period_definition:
        return False
    operative = _has(
        focal,
        (
            "must keep",
            "must retain",
            "must preserve",
            "must maintain",
            "shall keep",
            "shall retain",
            "shall preserve",
            "shall maintain",
            "is required to keep",
            "is required to retain",
            "is required to preserve",
            "is required to maintain",
            "must be kept",
            "must be retained",
            "must be preserved",
            "must be maintained",
        ),
    )
    return not operative


def _focal_incomplete_fragment(focal: str) -> str | None:
    text = re.sub(r"\s+", " ", focal).strip(" ;:,.")
    if not text:
        return "empty focal clause"
    if re.match(r"^\(\d+[A-Za-z]?\)\s+(may|must|shall|is|are)\b", text):
        return "focal quote starts mid-sentence after a subsection marker without an expressed subject"
    if re.search(r"\b(or|and|but)$", text):
        return "focal quote ends with a dangling conjunction"
    if re.match(r"^(or|and|but)\b", text):
        return "focal quote starts mid-sentence with a conjunction"
    if _has(text, ("notice issued under subsection", "designation under subsection", "person who receives a notice under subsection")) and re.search(r"\b(or|and)$", text):
        return "focal quote is an incomplete notice/designation fragment"
    if _has(text, ("subject to subsection", "referred to in subsection", "specified in subsection", "under subsection")) and len(text.split()) <= 12:
        return "focal quote is only a cross-reference fragment"
    return None


def _looks_storage_only(text: str) -> bool:
    storage = _has(text, ("keep", "kept", "store", "stored", "maintain", "maintained", "retain", "retained", "preserve", "copy", "duplicate"))
    processing = _has(text, ("process", "analyse", "analyze", "compute", "handle", "use", "processing"))
    return storage and not processing


def _regulated_object_type_from_text(text: str) -> str:
    if _has(text, ("security", "securities", "coupon", "coupons", "share", "shares", "debenture", "debentures", "bond", "bonds", "financial asset")):
        return "financial_asset"
    if _has(text, ("cord blood", "blood", "human tissue", "tissue", "organ", "organs", "cell", "cells", "specimen", "specimens", "biological material", "biological sample")):
        return "biological_material"
    if _has(text, ("regulated product", "electrical or electronic product", "e-waste", "ewaste", "goods", "product", "products", "commodity", "commodities", "property", "material recovered")):
        return "physical_goods"
    if _has(
        text,
        (
            "personal data",
            "data protection",
            "data export",
            "transfer data",
            "transfer of data",
            "information",
            "electronic record",
            "electronic records",
            "database",
            "dataset",
            "computer data",
            "customer data",
            "user data",
            "subscriber information",
        ),
    ):
        return "data_or_information"
    if _has(text, ("record", "records", "register", "registers", "document", "documents", "copy", "copies", "books")):
        return "record_or_document"
    if _has(text, ("service", "services")):
        return "service"
    if _has(text, ("person", "patient", "employee", "child", "live birth")):
        return "person"
    return "unknown"


def _looks_non_data_asset_transfer(text: str) -> bool:
    """Detect P6 false positives where the transferred object is not information.

    This is intentionally limited to asset-transfer contexts. It should reject
    securities/coupons/goods/tissue transfer provisions, but not record/register
    storage provisions inside financial statutes.
    """

    transfer_terms = (
        "transfer",
        "transferred",
        "transferee",
        "transferor",
        "import",
        "imported",
        "export",
        "exported",
        "deliver",
        "delivered",
        "shipment",
        "ship",
    )
    if not _has(text, transfer_terms):
        return False
    non_information_legal_objects = (
        "policy",
        "policies",
        "insurance policy",
        "licence",
        "license",
        "permit",
        "registration",
        "registered owner",
        "ownership",
        "ownership interest",
        "title",
        "right, title or interest",
        "contractual right",
    )
    information_terms = (
        "data",
        "information",
        "record",
        "records",
        "document",
        "documents",
        "electronic record",
        "electronic records",
        "database",
        "contents",
    )
    if _has(text, non_information_legal_objects) and not _has(text, information_terms):
        return True
    if _has(text, ("policy", "policies")) and _has(text, ("register", "registers")) and not _has(text, ("record", "records", "data", "information", "document", "documents")):
        return True
    object_type = _regulated_object_type_from_text(text)
    return object_type in {"financial_asset", "physical_goods", "biological_material", "service", "person", "unknown"}


def _looks_third_party_cdd_reliance(text: str) -> bool:
    return _has(text, ("third party", "third-party", "rely on")) and _has(
        text,
        (
            "customer due diligence",
            "cdd",
            "identify and verify",
            "identification and verification",
            "outsourcing",
            "outsourced",
        ),
    )


def _dedupe_p6_indicators(indicators: list[str], record: dict) -> list[str]:
    out = []
    for indicator in indicators:
        if indicator not in out:
            out.append(indicator)
    text = _record_text(record)
    if "P6-I2" in out and _looks_storage_only(text):
        out = [indicator for indicator in out if indicator not in {"P6-I1", "P6-I4"}]
    return out


def _match_record(match: IndicatorMatch, quote: str | None, failure_codes: list[str], status: str) -> dict:
    return {
        "indicator": match.indicator,
        "status": status,
        "quote": quote,
        "failure_codes": failure_codes,
        "actor": match.actor,
        "action": match.action,
        "regulated_object": match.regulated_object,
        "geographic_nexus": match.geographic_nexus,
        "duration": match.duration,
        "conditions": match.conditions,
        "rationale": match.why_included,
        "evidence_ids": match.evidence_ids,
        "required_element_codes": [item.element_code for item in match.required_element_status],
    }


def _is_true_review(decision: MappingDecision) -> bool:
    text = _fold(decision.rationale)
    return _has(text, ("cross-reference", "cross reference", "definition", "defined term", "subsidiary", "regulations", "not loaded", "external instrument", "unclear whether personal", "p6-i1", "p6-i4"))


def _has_any_code(codes: list[str], pool: tuple[str, ...]) -> bool:
    pool_set = set(pool)
    return any(code in pool_set for code in codes)


def _dedupe_text(values) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out



def _contains_normalized(haystack: str, needle: str) -> bool:
    return _normalize_text(needle) in _normalize_text(haystack)


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\u00a0", " ")
    value = value.translate(str.maketrans({
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
    }))
    return re.sub(r"\s+", " ", value).strip()


def _fold(value: str | None) -> str:
    return _normalize_text(value or "").casefold()


def _has(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _p4_remedy_limitation(text: str) -> bool:
    remedy = r"(?:injunction|damages?|account of profits|seizure|destruction|delivery up|blocking order|disable access)"
    negative_before = rf"\b(?:no|never|shall not|must not|may not|cannot|is not entitled to|not entitled to)\b.{{0,50}}\b{remedy}\b"
    negative_after = rf"\b{remedy}\b.{{0,50}}\b(?:shall not|must not|may not|cannot|is limited|are limited|not available|not be granted|not be awarded|not recoverable|is excluded|are excluded)\b"
    explicit_limitation = rf"\blimitation\s+(?:on|of)\s+{remedy}\b"
    return bool(
        re.search(negative_before, text)
        or re.search(negative_after, text)
        or re.search(explicit_limitation, text)
    )


def _result_code_for(status: str, codes: list[str]) -> str | None:
    if status == "accepted":
        return None
    if "NO_MATCH" in codes:
        return "NO_MATCH"
    if "MODEL_ERROR" in codes or "LLM_ERROR" in codes or "REVIEWER_MODEL_UNAVAILABLE" in codes:
        return "MODEL_ERROR"
    if status == "error" or any(code in TECHNICAL_DETAILS for code in codes):
        return "TECHNICAL_INPUT_ERROR"
    if "EXCLUSION_TRIGGERED" in codes or "REVIEWER_INVALID_EXCLUSION" in codes:
        return "EXCLUSION_TRIGGERED"
    if "LEGAL_UNCERTAINTY" in codes or "REVIEWER_UNCERTAIN" in codes or status == "review":
        return "LEGAL_UNCERTAINTY"
    if status == "rejected":
        return "REQUIRED_ELEMENT_MISSING"
    return "TECHNICAL_INPUT_ERROR"


def _result(
    task: CandidateTask,
    status: str,
    decision: MappingDecision | None,
    indicator: str | None,
    failure_codes: list[str],
    review_reasons: list[str],
    rationale: str,
    prompt_version: str,
    model_name: str,
    cache_key: str,
    llm_call: bool,
    cache_hit: bool,
    retries: int,
    error: str | None,
    *,
    warnings: list[str] | None = None,
    accepted_matches: list[dict] | None = None,
    review_matches: list[dict] | None = None,
    failed_required_elements: list[str] | None = None,
    uncertain_elements: list[str] | None = None,
    triggered_exclusions: list[str] | None = None,
    technical_detail: str | None = None,
    external_source_detail: str | None = None,
) -> ValidatedTaskResult:
    result_code = _result_code_for(status, failure_codes)
    failure_codes = [result_code] if result_code else []
    review_reasons = [result_code] if result_code == "LEGAL_UNCERTAINTY" else []
    queue_type = "none"
    if result_code == "LEGAL_UNCERTAINTY":
        queue_type = "human_legal_review"
        status = "human_legal_review"
    if result_code in {"TECHNICAL_INPUT_ERROR", "MODEL_ERROR"}:
        queue_type = "technical_repair"
        status = "technical_repair"
        technical_detail = technical_detail or result_code or "technical_repair_required"
    return ValidatedTaskResult(
        task_id=task.task_id,
        economy=task.economy,
        document_id=task.document_id,
        law_title=task.law_title,
        instrument_type=task.instrument_type,
        source_url=task.source_url,
        focal_provision_id=task.focal_provision_id,
        route_topic=task.route_topic,
        candidate_indicators=task.candidate_indicators,
        status=status,  # type: ignore[arg-type]
        queue_type=queue_type,  # type: ignore[arg-type]
        result_code=result_code,  # type: ignore[arg-type]
        indicator=indicator,  # type: ignore[arg-type]
        decision=decision,
        failure_codes=failure_codes,
        review_reasons=review_reasons,
        rationale=rationale,
        accepted_matches=accepted_matches or [],
        review_matches=review_matches or [],
        prompt_version=prompt_version,
        validation_version=P4_VALIDATION_VERSION if task.route_topic.startswith("P4_") else VALIDATION_VERSION,
        model_name=model_name,
        cache_key=cache_key,
        llm_call=llm_call,
        cache_hit=cache_hit,
        retries=retries,
        failed_required_elements=failed_required_elements or [],
        uncertain_elements=uncertain_elements or [],
        uncertain_exclusions=[],
        focal_uncertainty=None,
        triggered_exclusions=triggered_exclusions or [],
        technical_detail=technical_detail,
        affected_evidence_ids=[],
        expected_repair_action="inspect_source_or_model_output" if queue_type == "technical_repair" else None,
        external_source_detail=external_source_detail,
        error=error,
        warnings=warnings or [],
    )
