"""Authoritative registry for Zone 2 P6/P7 indicator execution.

The registry is intentionally small: it points every indicator to the task
kind, route topic, typed reviewer, resolver family, and aggregation family used
by the single production chain. Detailed indicator text remains in
``indicator_specs`` and is referenced here instead of copied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .indicator_specs import INDICATOR_SPEC_VERSION, INDICATOR_SPECS, P4_INDICATOR_SPEC_VERSION


@dataclass(frozen=True)
class IndicatorDefinition:
    indicator_id: str
    task_kind: str
    source_type: str
    route_topic: str
    contract_version: str
    reviewer_schema: str
    resolver_rule: str
    aggregation_rule: str
    required_attributes: tuple[str, ...] = ()
    hard_exclusions: tuple[str, ...] = ()


INDICATOR_REGISTRY: dict[str, IndicatorDefinition] = {
    "P4-I1": IndicatorDefinition(
        "P4-I1", "provision", "domestic_legislation", "P4_PATENT_APPLICATION",
        P4_INDICATOR_SPEC_VERSION, "P4PatentApplicationReview", "resolve_p4_patent_application", "provision",
        required_attributes=("affected_applicant", "operative_requirement", "legal_effect"),
        hard_exclusions=("ordinary_procedure_only", "definition_only"),
    ),
    "P4-I2": IndicatorDefinition(
        "P4-I2", "framework_element", "domestic_legislation", "P4_PATENT_ENFORCEMENT",
        P4_INDICATOR_SPEC_VERSION, "P4FrameworkElementReview", "resolve_p4_framework_element", "framework",
        required_attributes=("framework_element", "coverage", "evidence_character", "remedy_direction"),
    ),
    "P4-I3": IndicatorDefinition(
        "P4-I3", "provision", "domestic_legislation", "P4_PATENT_ENFORCEMENT",
        P4_INDICATOR_SPEC_VERSION, "P4PatentEnforcementOtherReview", "resolve_p4_patent_enforcement_other", "provision",
        required_attributes=("restriction_type", "triggering_conditions", "practical_legal_effect"),
        hard_exclusions=("keyword_only", "ordinary_limited_exception"),
    ),
    "P4-I4": IndicatorDefinition(
        "P4-I4", "external_status", "external_registry", "P4_TREATY_STATUS",
        P4_INDICATOR_SPEC_VERSION, "P4TreatyStatus", "resolve_p4_treaty_status", "external_status",
    ),
    "P4-I5": IndicatorDefinition(
        "P4-I5", "framework_element", "domestic_legislation", "P4_COPYRIGHT_FRAMEWORK",
        P4_INDICATOR_SPEC_VERSION, "P4FrameworkElementReview", "resolve_p4_framework_element", "framework",
        required_attributes=("framework_element", "coverage", "evidence_character"),
    ),
    "P4-I6": IndicatorDefinition(
        "P4-I6", "framework_element", "domestic_legislation", "P4_ONLINE_COPYRIGHT",
        P4_INDICATOR_SPEC_VERSION, "P4FrameworkElementReview", "resolve_p4_framework_element", "framework",
        required_attributes=("framework_element", "coverage", "evidence_character", "remedy_direction", "online_nexus"),
        hard_exclusions=("no_online_nexus", "safe_harbour_only"),
    ),
    "P4-I7": IndicatorDefinition(
        "P4-I7", "external_status", "external_registry", "P4_TREATY_STATUS",
        P4_INDICATOR_SPEC_VERSION, "P4TreatyStatus", "resolve_p4_treaty_status", "external_status",
    ),
    "P4-I8": IndicatorDefinition(
        "P4-I8", "external_status", "external_registry", "P4_TREATY_STATUS",
        P4_INDICATOR_SPEC_VERSION, "P4TreatyStatus", "resolve_p4_treaty_status", "external_status",
    ),
    "P4-I9": IndicatorDefinition(
        "P4-I9", "provision", "domestic_legislation", "P4_DISCLOSURE",
        P4_INDICATOR_SPEC_VERSION, "P4MandatoryDisclosureReview", "resolve_p4_mandatory_disclosure", "provision",
        required_attributes=("protected_subject", "information_holder", "compelled_actor", "disclosure_action", "non_compliance_consequence"),
        hard_exclusions=("ordinary_reporting", "voluntary_disclosure", "ordinary_discovery_with_safeguards"),
    ),
    "P4-I10": IndicatorDefinition(
        "P4-I10", "framework_element", "domestic_legislation", "P4_TRADE_SECRET_FRAMEWORK",
        P4_INDICATOR_SPEC_VERSION, "P4FrameworkElementReview", "resolve_p4_framework_element", "framework",
        required_attributes=("framework_element", "coverage", "evidence_character"),
    ),
    "P6-I1": IndicatorDefinition("P6-I1", "provision", "domestic_legislation", "P6_LOCATION", INDICATOR_SPEC_VERSION, "P6TransferReview", "resolve_p6_transfer", "provision"),
    "P6-I2": IndicatorDefinition("P6-I2", "provision", "domestic_legislation", "P6_LOCATION", INDICATOR_SPEC_VERSION, "P6StorageReview", "resolve_p6_storage", "provision"),
    "P6-I3": IndicatorDefinition("P6-I3", "provision", "domestic_legislation", "P6_LOCATION", INDICATOR_SPEC_VERSION, "P6InfrastructureReview", "resolve_p6_infrastructure", "provision"),
    "P6-I4": IndicatorDefinition("P6-I4", "provision", "domestic_legislation", "P6_LOCATION", INDICATOR_SPEC_VERSION, "P6TransferReview", "resolve_p6_transfer", "provision"),
    "P6-I5": IndicatorDefinition(
        "P6-I5",
        "treaty_provision",
        "treaty",
        "P6_TREATY",
        INDICATOR_SPEC_VERSION,
        "TreatyProvisionReview",
        "resolve_p6_treaty",
        "external_status",
    ),
    "P7-I1": IndicatorDefinition(
        "P7-I1",
        "framework_element",
        "domestic_legislation",
        "P7_DATA_PROTECTION_FRAMEWORK",
        INDICATOR_SPEC_VERSION,
        "FrameworkElementReview",
        "resolve_framework_element",
        "framework",
        required_attributes=("framework_element", "coverage"),
    ),
    "P7-I2": IndicatorDefinition(
        "P7-I2",
        "framework_element",
        "domestic_legislation",
        "P7_CYBERSECURITY_FRAMEWORK",
        INDICATOR_SPEC_VERSION,
        "FrameworkElementReview",
        "resolve_framework_element",
        "framework",
        required_attributes=("framework_element", "coverage"),
    ),
    "P7-I3": IndicatorDefinition(
        "P7-I3",
        "provision",
        "domestic_legislation",
        "P7_RETENTION",
        INDICATOR_SPEC_VERSION,
        "P7RetentionReview",
        "resolve_p7_retention",
        "provision",
        required_attributes=("retention_periods", "trigger_event"),
    ),
    "P7-I4": IndicatorDefinition(
        "P7-I4",
        "provision",
        "domestic_legislation",
        "P7_ACCOUNTABILITY",
        INDICATOR_SPEC_VERSION,
        "P7AccountabilityReview",
        "resolve_p7_accountability",
        "provision",
        required_attributes=("accountability_path",),
    ),
    "P7-I5": IndicatorDefinition(
        "P7-I5",
        "provision",
        "domestic_legislation",
        "P7_GOVERNMENT_ACCESS",
        INDICATOR_SPEC_VERSION,
        "P7GovernmentAccessReview",
        "resolve_p7_government_access",
        "provision",
        required_attributes=("judicial_authorization",),
    ),
}


def indicator_definition(indicator_id: str) -> IndicatorDefinition:
    return INDICATOR_REGISTRY[indicator_id]


def indicator_contract(indicator_id: str):
    return INDICATOR_SPECS[indicator_id]


def all_indicator_ids() -> tuple[str, ...]:
    return tuple(INDICATOR_REGISTRY)


def indicator_sort_key(indicator_id: str) -> tuple[int, int, str]:
    match = re.fullmatch(r"P(\d+)-I(\d+)", str(indicator_id or "").strip())
    if not match:
        return (10_000, 10_000, str(indicator_id or ""))
    return (int(match.group(1)), int(match.group(2)), "")
