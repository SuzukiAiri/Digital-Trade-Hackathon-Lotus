"""Aggregation and export helpers for validated mapping tasks."""

from __future__ import annotations

import json
import re
from collections import defaultdict

from .models import MeasureRecord, ValidatedTaskResult


AGGREGATION_VERSION = "rdtii-aggregation-v5-match-aware"
P4_AGGREGATION_VERSION = "rdtii-p4-framework-aggregation-v3-element-statuses"

P4_FRAMEWORK_ELEMENTS: dict[str, tuple[str, ...]] = {
    "P4-I2": ("ordinary_civil_or_administrative_remedies", "provisional_measures"),
    "P4-I5": ("copyright_framework", "copyright_exceptions"),
    "P4-I6": ("online_civil_or_administrative_remedies", "online_provisional_measures"),
    "P4-I10": (
        "statutory_trade_secret_protection",
        "common_law_or_case_law_protection",
        "trade_secret_remedies",
    ),
}


def aggregate_provision_measures(results: list[ValidatedTaskResult]) -> list[MeasureRecord]:
    groups: dict[tuple[str, str, str], list[tuple[ValidatedTaskResult, dict]]] = defaultdict(list)
    supporting: dict[tuple[str, str, str], list[ValidatedTaskResult]] = defaultdict(list)
    for result in results:
        if result.status == "rejected" and result.focal_role == "supporting_only":
            indicator = result.indicator
            if indicator:
                supporting[(indicator, result.document_id, _section_family(result.focal_provision_id))].append(result)
            continue
        if result.status != "accepted":
            continue
        for match in result.accepted_matches:
            indicator = match.get("indicator")
            if not indicator:
                continue
            signature = _measure_signature(result, match)
            groups[(indicator, result.document_id, signature)].append((result, match))

    measures: list[MeasureRecord] = []
    for (indicator, document_id, _signature), pairs in sorted(groups.items()):
        first = pairs[0][0]
        refs = _dedupe(item.focal_provision_id for item, _match in pairs)
        support_key = (indicator, document_id, _section_family(first.focal_provision_id))
        support_items = supporting.get(support_key, [])
        refs = _ordered_section_refs(refs, [item.focal_provision_id for item in support_items])
        snippets = _dedupe((match.get("quote") or "") for _item, match in pairs)
        rationales = _dedupe((match.get("rationale") or item.rationale) for item, match in pairs)
        support_task_ids = [item.task_id for item in support_items]
        measures.append(
            MeasureRecord(
                economy=first.economy,
                indicator_id=indicator,  # type: ignore[arg-type]
                document_id=document_id,
                official_title=first.law_title,
                instrument_type=first.instrument_type,
                source_url=first.source_url,
                section_references=refs,
                verbatim_snippets=snippets,
                mapping_rationale=" ".join(rationales)[:2000],
                evidence_task_ids=_dedupe([*(item.task_id for item, _match in pairs), *support_task_ids]),
                measure_type="framework" if indicator in {*P4_FRAMEWORK_ELEMENTS, "P7-I1", "P7-I2"} else "provision",
                review_required=False,
                coverage=_coverage(first.law_title),
                confidence=_confidence([item for item, _match in pairs]),
            )
        )
    return measures


def aggregate_p4_framework_conclusions(
    results: list[ValidatedTaskResult],
    *,
    coverage_sufficient: dict[str, bool] | None = None,
) -> list[dict]:
    """Aggregate accepted element evidence without inferring absence from no hits."""

    coverage_sufficient = coverage_sufficient or {}
    accepted_elements: dict[str, set[str]] = defaultdict(set)
    complete_eligible_elements: dict[str, set[str]] = defaultdict(set)
    supporting_ids: dict[str, list[str]] = defaultdict(list)
    uncertain_elements: dict[str, set[str]] = defaultdict(set)
    uncertain_indicators: set[str] = set()
    for result in results:
        indicator = str(result.indicator or "")
        if indicator not in P4_FRAMEWORK_ELEMENTS:
            continue
        if result.status == "human_legal_review":
            uncertain_indicators.add(indicator)
            for match in result.review_matches:
                element = _p4_framework_element_from_match(match)
                if element in P4_FRAMEWORK_ELEMENTS[indicator]:
                    uncertain_elements[indicator].add(element)
            continue
        if result.status != "accepted":
            continue
        elements: set[str] = set()
        for match in result.accepted_matches:
            if not isinstance(match, dict):
                continue
            element = str(match.get("p4_framework_element") or "")
            if not element:
                continue
            elements.add(element)
            if indicator != "P4-I10" or _p4_i10_complete_eligible(match):
                complete_eligible_elements[indicator].add(element)
        if result.human_validated_attributes:
            human_element = str(result.human_validated_attributes.get("framework_element") or "")
            elements.add(human_element)
            if indicator != "P4-I10" or _p4_i10_human_complete_eligible(result.human_validated_attributes):
                complete_eligible_elements[indicator].add(human_element)
        for element in elements:
            if element in P4_FRAMEWORK_ELEMENTS[indicator]:
                accepted_elements[indicator].add(element)
                supporting_ids[indicator].append(result.task_id)

    conclusions = []
    for indicator, required in P4_FRAMEWORK_ELEMENTS.items():
        present = accepted_elements[indicator]
        if indicator == "P4-I10":
            eligible = complete_eligible_elements[indicator]
            protection = bool(
                eligible
                & {
                    "statutory_trade_secret_protection",
                    "common_law_or_case_law_protection",
                }
            )
            complete = protection and "trade_secret_remedies" in eligible
        else:
            complete = set(required).issubset(present)
        if complete:
            status = "complete"
        elif present:
            status = "partial"
        elif coverage_sufficient.get(indicator, False) and indicator not in uncertain_indicators:
            status = "absent"
        else:
            status = "uncertain"
        score = {"complete": 0, "partial": 0.5, "absent": 1}.get(status)
        element_statuses = {
            element: (
                "present"
                if element in present
                else "uncertain"
                if element in uncertain_elements[indicator]
                else "not_found"
            )
            for element in required
        }
        conclusions.append(
            {
                "indicator_id": indicator,
                "framework_status": status,
                "rdtii_score": score,
                "element_statuses": element_statuses,
                "present_elements": sorted(present),
                "uncertain_elements": sorted(uncertain_elements[indicator]),
                "complete_eligible_elements": sorted(complete_eligible_elements[indicator]),
                "missing_elements": sorted(set(required) - present),
                "supporting_task_ids": sorted(set(supporting_ids[indicator])),
                "coverage_sufficient_for_absence": bool(coverage_sufficient.get(indicator, False)),
                "aggregation_version": P4_AGGREGATION_VERSION,
            }
        )
    return conclusions


def _p4_framework_element_from_match(match: dict) -> str:
    if not isinstance(match, dict):
        return ""
    direct = str(match.get("p4_framework_element") or "")
    if direct:
        return direct
    reviewer = match.get("reviewer")
    if not isinstance(reviewer, dict):
        return ""
    for check in reviewer.get("optional_checks") or []:
        if not isinstance(check, dict) or check.get("check_code") != "P4_FRAMEWORK_ELEMENT":
            continue
        try:
            payload = json.loads(str(check.get("reason") or "{}"))
        except json.JSONDecodeError:
            return ""
        if isinstance(payload, dict):
            return str(payload.get("framework_element") or payload.get("candidate_element") or "")
    return ""


def _p4_i10_complete_eligible(match: dict) -> bool:
    facts = match.get("p4_framework_facts")
    if not isinstance(facts, dict):
        facts = {}
    attrs = match.get("reviewer_attributes")
    if not isinstance(attrs, dict):
        attrs = {}
    coverage = str(facts.get("coverage") or attrs.get("coverage") or "")
    if coverage != "horizontal":
        return False
    if facts.get("government_or_official_only") is True:
        return False
    element = str(match.get("p4_framework_element") or "")
    if element == "statutory_trade_secret_protection":
        return (
            facts.get("protected_private_or_commercial_information") is True
            and facts.get("unauthorised_acquisition_use_or_disclosure") is True
        )
    return element in {"common_law_or_case_law_protection", "trade_secret_remedies"}


def _p4_i10_human_complete_eligible(attributes: dict) -> bool:
    if str(attributes.get("coverage") or "") != "horizontal":
        return False
    if attributes.get("government_or_official_only") is True:
        return False
    element = str(attributes.get("framework_element") or "")
    if element == "statutory_trade_secret_protection":
        return (
            attributes.get("protected_private_or_commercial_information") is True
            and attributes.get("unauthorised_acquisition_use_or_disclosure") is True
        )
    return element in {"common_law_or_case_law_protection", "trade_secret_remedies"}


def _measure_signature(result: ValidatedTaskResult, match: dict) -> str:
    return _section_family(result.focal_provision_id)


def _normalise_section_reference(value: str) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"^\s*(s\.|sec\.|section|reg\.|regulation)\s*", "", text)
    text = re.sub(r"\s+", "", text)
    text = text.replace("–", "-").replace("—", "-")
    return text or str(value or "")


def _section_family(value: str) -> str:
    text = _normalise_section_reference(value)
    match = re.match(r"([a-z]*\d+[a-z]*)", text)
    return match.group(1) if match else text


def _ordered_section_refs(primary_refs: list[str], supporting_refs: list[str]) -> list[str]:
    out = _dedupe(primary_refs)
    for ref in supporting_refs:
        if ref not in out:
            out.append(ref)
    return out


def _coverage(title: str) -> str:
    text = title.casefold()
    if "personal data protection" in text:
        return "Horizontal"
    if "cybersecurity" in text:
        return "Sectoral - Cybersecurity"
    if any(term in text for term in ("bank", "securities", "financial", "payment", "moneylender", "insurance", "credit")):
        return "Sectoral - Financial services"
    if "telecommunication" in text or "telecom" in text:
        return "Sectoral - Telecommunications"
    if any(term in text for term in ("income tax", "goods and services tax", "customs", "tax")):
        return "Sectoral - Taxation"
    if any(term in text for term in ("companies", "accounting", "acra", "corporate")):
        return "Sectoral - Corporate and accounting"
    if any(term in text for term in ("electronic", "online", "digital", "platform")):
        return "Sectoral - Digital services"
    return "Sectoral - Other"


def _confidence(items: list[ValidatedTaskResult]) -> str:
    if all(not item.warnings and not item.result_code for item in items):
        return "high"
    return "medium"


def _dedupe(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out
