"""Single source of truth for RDTII P6/P7 mapping definitions."""

from __future__ import annotations

from dataclasses import dataclass


INDICATOR_SPEC_VERSION = "rdtii-p6-p7-indicator-spec-v5-full-contracts"
P4_INDICATOR_SPEC_VERSION = "rdtii-p4-indicator-spec-v2-boundary-contracts"

P4_MAPPING_BOUNDARY_RULES = (
    "P4-I1: distinguish a local-presence/address-for-service burden, first filing, substantive examination, or material cost from routine forms, deadlines, renewals, and ordinary contact details.",
    "P4-I2: positive evidence must grant or establish a patent remedy; a prohibition, limitation, defence, immunity, or unavailability of a remedy is not a positive remedy element.",
    "P4-I3: assess an actual post-grant patent-right restriction with its scope, conditions, compensation, safeguards, review, and practical effect; keywords alone never match.",
    "P4-I5: classify operative rights/protection as copyright_framework and explicit permitted uses/non-infringement rules as copyright_exceptions; scope, inspection, records, procedure, and incomplete cross-references are supporting-only.",
    "P4-I6: require an explicit copyright-related online nexus and an actual final or provisional online remedy; general copyright remedies and unrelated website blocking do not match.",
    "P4-I9: require protected commercial information and a holder/business/regulated entity compelled to provide it to a court or authority; official disclosure, internal sharing, and secrecy duties run in the wrong direction.",
    "P4-I10: distinguish general protection of private/commercial secrets against unauthorised acquisition, use, or disclosure from government-only or narrow sectoral secrecy; sectoral support alone cannot establish a complete framework.",
    "All P4 framework evidence must be an operative focal provision; definition-only, scope-only, procedure-only, cross-reference-only, and supporting-only text cannot independently satisfy an element.",
)


@dataclass(frozen=True)
class IndicatorSpec:
    indicator_id: str
    decision_group: str
    required_elements: tuple[str, ...]
    excluded_cases: tuple[str, ...]
    positive_expressions: tuple[str, ...]
    functional_expressions: tuple[str, ...]
    object_terms: tuple[str, ...]
    action_terms: tuple[str, ...]
    geographic_or_duration_terms: tuple[str, ...]
    source_families: tuple[str, ...]
    adjacent_indicators: tuple[str, ...]
    assessment_unit: str = "provision"
    optional_elements: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    evidence_rules: tuple[str, ...] = ("operative evidence must be cited by evidence_id",)
    context_rules: tuple[str, ...] = ("focal clause provides operative rule", "supporting context may provide definitions, scope, conditions, or exclusions")
    allowed_statuses: tuple[str, ...] = ("accepted", "rejected", "human_legal_review", "technical_repair")
    framework_or_provision_type: str = "provision"
    # These fields are consumed by the deterministic resolver.  Keeping them
    # beside the retrieval/prompt terms prevents the structured, direct and
    # framework paths from inventing different legal acceptance rules.
    focal_required_elements: tuple[str, ...] = ()
    required_attributes: tuple[str, ...] = ()
    allowed_object_types: tuple[str, ...] = ()
    human_review_conditions: tuple[str, ...] = ()
    title: str = ""


P6_REVIEW_ELEMENTS = (
    "OPERATIVE_RULE",
    "INFORMATION_BEARING_OBJECT",
    "CROSS_BORDER_NEXUS",
    "ABSOLUTE_TRANSFER_PROHIBITION",
    "MANDATORY_DOMESTIC_PROCESSING",
    "LOCAL_STORAGE_ACTION",
    "EXPLICIT_DOMESTIC_STORAGE_LOCATION",
    "DIGITAL_INFRASTRUCTURE_OBJECT",
    "DOMESTIC_INFRASTRUCTURE_DEPLOYMENT",
    "CONDITIONAL_TRANSFER_PATH",
    "ORDINARY_COMPLIANCE_PATH",
    "MARKET_ACTOR_OBLIGATION",
)

P6_REVIEW_EXCLUSIONS = (
    "GOVERNMENT_COOPERATION",
    "LAW_ENFORCEMENT_COOPERATION",
    "REGULATORY_COOPERATION",
    "DEVICE_DISPOSAL_OR_DATA_ERASURE",
    "NON_DATA_ASSET_TRANSFER",
    "CONFIDENTIALITY_ONLY",
    "GOVERNMENT_INTERNAL_ONLY",
)

P7_REVIEW_ELEMENTS_BY_GROUP = {
    "P7_RETENTION": (
        "OPERATIVE_RULE",
        "MANDATORY_RETENTION_ACTION",
        "INFORMATION_BEARING_OBJECT",
        "MINIMUM_RETENTION_DURATION",
        "CALCULABLE_DURATION",
        "IN_SCOPE_RECORD_TYPE",
    ),
    "P7_ACCOUNTABILITY": (
        "OPERATIVE_RULE",
        "MANDATORY_DESIGNATION_OR_ASSESSMENT",
        "PRIVACY_COMPLIANCE_FUNCTION",
        "DPO_PATH",
        "DPIA_PATH",
    ),
    "P7_GOVERNMENT_ACCESS": (
        "OPERATIVE_RULE",
        "PUBLIC_AUTHORITY",
        "COMPULSORY_ACCESS_POWER",
        "EXTERNALLY_HELD_DATA",
        "PERSONAL_OR_IDENTIFIABLE_DATA",
    ),
}

P7_REVIEW_EXCLUSIONS_BY_GROUP = {
    "P7_RETENTION": (
        "MAXIMUM_DURATION_ONLY",
        "NO_DURATION",
        "FUTURE_RULEMAKING_ONLY",
        "PHYSICAL_ITEM_ONLY",
        "FACILITY_MAINTENANCE_ONLY",
        "GOVERNMENT_DATA_ONLY",
        "PUBLIC_ADMINISTRATION_INTERNAL_ONLY",
        "PROCEDURE_OR_PENALTY_ONLY",
    ),
    "P7_ACCOUNTABILITY": (
        "GENERAL_COMPLIANCE_ROLE",
        "CYBERSECURITY_ONLY_ROLE",
        "SAFETY_OR_BUILDING_ROLE",
        "FINANCIAL_OR_QUALITY_ROLE",
        "GENERAL_RISK_ASSESSMENT",
        "VOLUNTARY_RECOMMENDATION",
    ),
    "P7_GOVERNMENT_ACCESS": (
        "OFFICIAL_SECRECY_ONLY",
        "INTERAGENCY_SHARING_ONLY",
        "PUBLIC_REGISTER_ONLY",
        "VOLUNTARY_DISCLOSURE_ONLY",
        "AGGREGATE_OR_ANONYMOUS_ONLY",
        "SUBSEQUENT_USE_ONLY",
        "FUTURE_RULEMAKING_ONLY",
    ),
}

P4_REVIEW_ELEMENTS_BY_GROUP = {
    "P4_PATENT_APPLICATION": (
        "OPERATIVE_RULE",
        "PATENT_APPLICATION_NEXUS",
        "MATERIAL_APPLICATION_BURDEN",
        "LEGAL_CONSEQUENCE",
    ),
    "P4_PATENT_ENFORCEMENT": (
        "OPERATIVE_RULE",
        "PATENT_RIGHT_RESTRICTION",
        "TRIGGERING_CONDITIONS",
        "PRACTICAL_LEGAL_EFFECT",
        "COMPENSATION",
        "PROCEDURAL_SAFEGUARDS",
        "JUDICIAL_REVIEW",
    ),
    "P4_DISCLOSURE": (
        "OPERATIVE_RULE",
        "PROTECTED_SUBJECT",
        "COMPULSORY_DISCLOSURE_ACTION",
        "LEGAL_COMPULSION",
        "LEGALLY_COMPELLED_HOLDER",
        "PUBLIC_INTEREST_BASIS",
        "DISCLOSURE_SAFEGUARDS",
    ),
}

P4_REVIEW_EXCLUSIONS_BY_GROUP = {
    "P4_PATENT_APPLICATION": (
        "ORDINARY_FORM_FORMAT_OR_DEADLINE",
        "ORDINARY_AGENT_PROCEDURE",
        "DEFINITION_OR_RULEMAKING_ONLY",
        "PATENT_ENFORCEMENT_NOT_APPLICATION",
    ),
    "P4_PATENT_ENFORCEMENT": (
        "KEYWORD_ONLY_OR_DEFINITION",
        "ORDINARY_LIMITED_PUBLIC_INTEREST_EXCEPTION",
        "APPLICATION_OR_REMEDY_ONLY",
        "NON_PATENT_RULE",
    ),
    "P4_DISCLOSURE": (
        "ORDINARY_RECORDKEEPING_OR_REPORTING",
        "VOLUNTARY_DISCLOSURE",
        "NO_PROTECTED_SUBJECT",
        "NO_LEGAL_COMPULSION",
        "WRONG_DISCLOSURE_DIRECTION",
        "PUBLIC_OFFICIAL_DISCLOSURE_OR_INTERNAL_SHARING",
        "PRIVATE_CONTRACT_DISCLOSURE",
        "GOVERNMENT_PROCUREMENT_CONDITION",
        "ORDINARY_DISCOVERY_WITH_ADEQUATE_SAFEGUARDS",
    ),
}

FRAMEWORK_REVIEW_ELEMENTS = {
    "P4-I2": (
        "ORDINARY_CIVIL_OR_ADMINISTRATIVE_REMEDIES",
        "PROVISIONAL_MEASURES",
    ),
    "P4-I5": (
        "COPYRIGHT_FRAMEWORK",
        "COPYRIGHT_EXCEPTIONS",
    ),
    "P4-I6": (
        "ONLINE_CIVIL_OR_ADMINISTRATIVE_REMEDIES",
        "ONLINE_PROVISIONAL_MEASURES",
    ),
    "P4-I10": (
        "STATUTORY_TRADE_SECRET_PROTECTION",
        "COMMON_LAW_OR_CASE_LAW_PROTECTION",
        "TRADE_SECRET_REMEDIES",
    ),
    "P7-I1": (
        "PERSONAL_DATA_SCOPE",
        "SUBSTANTIVE_DATA_PROTECTION_DUTIES_OR_RIGHTS",
        "REGULATOR_OR_ENFORCEMENT",
        "HORIZONTAL_OR_SECTORAL_SCOPE",
    ),
    "P7-I2": (
        "CYBERSECURITY_SCOPE",
        "SECURITY_RISK_INCIDENT_OR_AUDIT_OBLIGATIONS",
        "AUTHORITY_OR_ENFORCEMENT",
        "HORIZONTAL_OR_SECTORAL_SCOPE",
    ),
}

FRAMEWORK_REVIEW_EXCLUSIONS = {
    "P4-I2": ("NON_PATENT_REMEDY", "LIMITATION_DEFENCE_OR_IMMUNITY", "NON_OPERATIVE_OR_SUPPORTING_ONLY"),
    "P4-I5": ("TITLE_OR_REGISTRATION_ONLY", "SCOPE_PROCEDURE_OR_INSPECTION_ONLY", "NON_OPERATIVE_OR_SUPPORTING_ONLY"),
    "P4-I6": ("NO_ONLINE_NEXUS", "NON_COPYRIGHT_WEBSITE_BLOCKING", "INTERMEDIARY_SAFE_HARBOUR_ONLY", "NON_OPERATIVE_OR_SUPPORTING_ONLY"),
    "P4-I10": ("STATE_SECRET_OR_OFFICIAL_CONFIDENTIALITY_ONLY", "NARROW_DUTY_ONLY", "SECTORAL_OR_GOVERNMENT_ONLY", "NON_OPERATIVE_OR_SUPPORTING_ONLY"),
    "P7-I1": ("NO_FRAMEWORK_FOUND",),
    "P7-I2": ("SCATTERED_SECURITY_PROVISIONS_ONLY", "NO_FRAMEWORK_FOUND"),
}

FRAMEWORK_REVIEWER_CONTRACT_VERSION = "rdtii-framework-reviewer-contract-v2-legal-function-gate"

FRAMEWORK_ELEMENT_NAME_TO_CODE = {
    "P4-I2": {
        "ordinary_civil_or_administrative_remedies": "ORDINARY_CIVIL_OR_ADMINISTRATIVE_REMEDIES",
        "provisional_measures": "PROVISIONAL_MEASURES",
    },
    "P4-I5": {
        "copyright_framework": "COPYRIGHT_FRAMEWORK",
        "copyright_exceptions": "COPYRIGHT_EXCEPTIONS",
    },
    "P4-I6": {
        "online_civil_or_administrative_remedies": "ONLINE_CIVIL_OR_ADMINISTRATIVE_REMEDIES",
        "online_provisional_measures": "ONLINE_PROVISIONAL_MEASURES",
    },
    "P4-I10": {
        "statutory_trade_secret_protection": "STATUTORY_TRADE_SECRET_PROTECTION",
        "common_law_or_case_law_protection": "COMMON_LAW_OR_CASE_LAW_PROTECTION",
        "trade_secret_remedies": "TRADE_SECRET_REMEDIES",
    },
    "P7-I1": {
        "personal_data_scope": "PERSONAL_DATA_SCOPE",
        "substantive_duties_or_rights": "SUBSTANTIVE_DATA_PROTECTION_DUTIES_OR_RIGHTS",
        "regulator_or_enforcement": "REGULATOR_OR_ENFORCEMENT",
    },
    "P7-I2": {
        "cybersecurity_scope": "CYBERSECURITY_SCOPE",
        "substantive_cybersecurity_obligation": "SECURITY_RISK_INCIDENT_OR_AUDIT_OBLIGATIONS",
        "authority_or_enforcement": "AUTHORITY_OR_ENFORCEMENT",
    },
}

FRAMEWORK_ELEMENT_ALIASES = {
    indicator: {name: name for name in elements}
    for indicator, elements in {
        "P4-I2": ("ordinary_civil_or_administrative_remedies", "provisional_measures"),
        "P4-I5": ("copyright_framework", "copyright_exceptions"),
        "P4-I6": ("online_civil_or_administrative_remedies", "online_provisional_measures"),
        "P4-I10": ("statutory_trade_secret_protection", "common_law_or_case_law_protection", "trade_secret_remedies"),
    }.items()
}
FRAMEWORK_ELEMENT_ALIASES.update({
    "P7-I1": {
        "personal_data_scope": "personal_data_scope",
        "substantive_duties_or_rights": "substantive_duties_or_rights",
        "regulator_or_enforcement": "regulator_or_enforcement",
        "authority_or_enforcement": "regulator_or_enforcement",
    },
    "P7-I2": {
        "cybersecurity_scope": "cybersecurity_scope",
        "substantive_cybersecurity_obligation": "substantive_cybersecurity_obligation",
        "substantive_duties_or_rights": "substantive_cybersecurity_obligation",
        "authority_or_enforcement": "authority_or_enforcement",
        "regulator_or_enforcement": "authority_or_enforcement",
    },
})

FRAMEWORK_ADMINISTRATIVE_FUNCTIONS = {
    "procedural_rule",
    "appointment_or_delegation",
    "administrative_information",
    "cross_reference",
    "cross_reference_only",
    "supporting_only",
    "incomplete_fragment",
    "unrelated",
    "exception",
    "definition_only",
    "publication_only",
    "legal_status_only",
    "guide_or_outline_only",
    "procedural_only",
    "consequential_amendment",
}

FRAMEWORK_LEGAL_FUNCTION_ALLOWLIST = {
    "P4-I2": {
        "ordinary_civil_or_administrative_remedies": {"substantive_duty", "individual_right", "enforcement_power", "penalty_or_sanction"},
        "provisional_measures": {"individual_right", "enforcement_power", "procedural_rule"},
    },
    "P4-I5": {
        "copyright_framework": {"substantive_duty", "individual_right", "enforcement_power"},
        "copyright_exceptions": {"substantive_duty", "exception"},
    },
    "P4-I6": {
        "online_civil_or_administrative_remedies": {"substantive_duty", "individual_right", "enforcement_power", "penalty_or_sanction"},
        "online_provisional_measures": {"individual_right", "enforcement_power", "procedural_rule"},
    },
    "P4-I10": {
        "statutory_trade_secret_protection": {"substantive_duty", "individual_right"},
        "common_law_or_case_law_protection": {"application_rule", "individual_right", "enforcement_power"},
        "trade_secret_remedies": {"individual_right", "enforcement_power", "penalty_or_sanction"},
    },
    "P7-I1": {
        "personal_data_scope": {"scope_rule", "application_rule"},
        "substantive_duties_or_rights": {"substantive_duty", "individual_right"},
        "regulator_or_enforcement": {"regulator_power", "enforcement_power", "penalty_or_sanction"},
    },
    "P7-I2": {
        "cybersecurity_scope": {"scope_rule", "application_rule"},
        "substantive_cybersecurity_obligation": {"substantive_duty"},
        "authority_or_enforcement": {"regulator_power", "enforcement_power", "penalty_or_sanction"},
    },
}

# Resolver contract.  The prompt-facing ``INDICATOR_SPECS`` remains the source
# of retrieval terms, while this map makes the legal acceptance constraints
# explicit and available to every processing mode.
INDICATOR_FOCAL_CONTRACT: dict[str, dict[str, tuple[str, ...]]] = {
    "P4-I1": {
        "focal_required_elements": ("OPERATIVE_RULE", "PATENT_APPLICATION_NEXUS", "MATERIAL_APPLICATION_BURDEN", "LEGAL_CONSEQUENCE"),
        "required_attributes": ("affected_applicant", "operative_requirement", "legal_effect"),
        "human_review_conditions": ("application_burden_uncertain",),
    },
    "P4-I2": {"focal_required_elements": ("FRAMEWORK_ELEMENT",)},
    "P4-I3": {
        "focal_required_elements": ("OPERATIVE_RULE", "PATENT_RIGHT_RESTRICTION", "TRIGGERING_CONDITIONS", "PRACTICAL_LEGAL_EFFECT"),
        "required_attributes": ("restriction_type", "triggering_conditions", "practical_legal_effect"),
        "human_review_conditions": ("restriction_effect_uncertain",),
    },
    "P4-I5": {"focal_required_elements": ("FRAMEWORK_ELEMENT",)},
    "P4-I6": {"focal_required_elements": ("FRAMEWORK_ELEMENT", "ONLINE_NEXUS")},
    "P4-I9": {
        "focal_required_elements": ("OPERATIVE_RULE", "PROTECTED_SUBJECT", "COMPULSORY_DISCLOSURE_ACTION", "LEGAL_COMPULSION", "LEGALLY_COMPELLED_HOLDER"),
        "required_attributes": ("protected_subject", "information_holder", "compelled_actor", "disclosure_action", "non_compliance_consequence"),
        "human_review_conditions": ("protected_subject_uncertain", "legal_compulsion_uncertain"),
    },
    "P4-I10": {"focal_required_elements": ("FRAMEWORK_ELEMENT",)},
    "P6-I1": {
        "focal_required_elements": ("OPERATIVE_RULE", "INFORMATION_BEARING_OBJECT", "CROSS_BORDER_NEXUS", "ABSOLUTE_TRANSFER_PROHIBITION"),
        "allowed_object_types": ("data", "personal data", "information", "records", "electronic records", "database contents"),
        "human_review_conditions": ("object_type_uncertain", "operative_prohibition_uncertain"),
    },
    "P6-I2": {
        "focal_required_elements": ("OPERATIVE_RULE", "INFORMATION_BEARING_OBJECT", "LOCAL_STORAGE_ACTION", "EXPLICIT_DOMESTIC_STORAGE_LOCATION"),
        "allowed_object_types": ("data", "information", "records", "electronic records", "database contents"),
    },
    "P6-I3": {
        "focal_required_elements": ("OPERATIVE_RULE", "DIGITAL_INFRASTRUCTURE_OBJECT", "DOMESTIC_INFRASTRUCTURE_DEPLOYMENT"),
        "allowed_object_types": ("server", "data centre", "cloud infrastructure", "digital infrastructure"),
    },
    "P6-I4": {
        "focal_required_elements": ("OPERATIVE_RULE", "INFORMATION_BEARING_OBJECT", "CROSS_BORDER_NEXUS", "CONDITIONAL_TRANSFER_PATH"),
        "allowed_object_types": ("data", "personal data", "information", "records", "electronic records", "database contents"),
        "human_review_conditions": ("transfer_condition_uncertain", "object_type_uncertain"),
    },
    "P7-I3": {
        "focal_required_elements": ("OPERATIVE_RULE", "MANDATORY_RETENTION_ACTION", "MINIMUM_RETENTION_DURATION"),
        "required_attributes": ("retention_periods", "trigger_event"),
        "human_review_conditions": ("retention_duration_uncertain",),
    },
    "P7-I4": {
        "focal_required_elements": ("OPERATIVE_RULE", "MANDATORY_DESIGNATION_OR_ASSESSMENT"),
        "required_attributes": ("accountability_path",),
        "human_review_conditions": ("accountability_path_uncertain",),
    },
    "P7-I5": {
        "focal_required_elements": ("OPERATIVE_RULE", "PUBLIC_AUTHORITY", "COMPULSORY_ACCESS_POWER", "EXTERNALLY_HELD_DATA", "PERSONAL_OR_IDENTIFIABLE_DATA"),
        "required_attributes": ("judicial_authorization",),
        "human_review_conditions": ("personal_data_object_uncertain", "judicial_authorization_uncertain"),
    },
    "P7-I1": {"focal_required_elements": ("FRAMEWORK_ELEMENT",)},
    "P7-I2": {"focal_required_elements": ("FRAMEWORK_ELEMENT",)},
    "P6-I5": {"focal_required_elements": ("SINGAPORE_PARTY", "AGREEMENT_IN_FORCE", "BINDING_DATA_TRANSFER_COMMITMENT")},
}

# Reviewer element codes are deliberately centralised here as well.  These are
# the exact codes the deterministic resolver consumes, not a second natural
# language interpretation of the contract.
INDICATOR_REQUIRED_REVIEW_ELEMENTS: dict[str, tuple[str, ...]] = {
    "P4-I1": ("OPERATIVE_RULE", "PATENT_APPLICATION_NEXUS", "MATERIAL_APPLICATION_BURDEN", "LEGAL_CONSEQUENCE"),
    "P4-I3": ("OPERATIVE_RULE", "PATENT_RIGHT_RESTRICTION", "TRIGGERING_CONDITIONS", "PRACTICAL_LEGAL_EFFECT"),
    "P4-I9": ("OPERATIVE_RULE", "PROTECTED_SUBJECT", "COMPULSORY_DISCLOSURE_ACTION", "LEGAL_COMPULSION", "LEGALLY_COMPELLED_HOLDER"),
    "P4-I2": FRAMEWORK_REVIEW_ELEMENTS["P4-I2"],
    "P4-I5": FRAMEWORK_REVIEW_ELEMENTS["P4-I5"],
    "P4-I6": FRAMEWORK_REVIEW_ELEMENTS["P4-I6"],
    "P4-I10": FRAMEWORK_REVIEW_ELEMENTS["P4-I10"],
    "P6-I1": ("OPERATIVE_RULE", "INFORMATION_BEARING_OBJECT"),
    "P6-I2": ("OPERATIVE_RULE", "INFORMATION_BEARING_OBJECT", "LOCAL_STORAGE_ACTION", "EXPLICIT_DOMESTIC_STORAGE_LOCATION"),
    "P6-I3": ("OPERATIVE_RULE", "DIGITAL_INFRASTRUCTURE_OBJECT", "DOMESTIC_INFRASTRUCTURE_DEPLOYMENT", "MARKET_ACTOR_OBLIGATION"),
    "P6-I4": ("OPERATIVE_RULE", "INFORMATION_BEARING_OBJECT", "CROSS_BORDER_NEXUS", "CONDITIONAL_TRANSFER_PATH"),
    "P7-I3": P7_REVIEW_ELEMENTS_BY_GROUP["P7_RETENTION"],
    "P7-I4": ("OPERATIVE_RULE", "MANDATORY_DESIGNATION_OR_ASSESSMENT", "PRIVACY_COMPLIANCE_FUNCTION"),
    "P7-I5": P7_REVIEW_ELEMENTS_BY_GROUP["P7_GOVERNMENT_ACCESS"],
    "P7-I1": FRAMEWORK_REVIEW_ELEMENTS["P7-I1"],
    "P7-I2": FRAMEWORK_REVIEW_ELEMENTS["P7-I2"],
}


def focal_required_elements(indicator: str) -> tuple[str, ...]:
    return INDICATOR_FOCAL_CONTRACT.get(indicator, {}).get("focal_required_elements", ())


def required_attributes(indicator: str) -> tuple[str, ...]:
    return INDICATOR_FOCAL_CONTRACT.get(indicator, {}).get("required_attributes", ())


def required_review_elements(indicator: str) -> tuple[str, ...]:
    return INDICATOR_REQUIRED_REVIEW_ELEMENTS.get(indicator, ())


def canonical_framework_element(indicator: str, value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    canonical = FRAMEWORK_ELEMENT_ALIASES.get(indicator, {}).get(text)
    if canonical:
        return canonical
    return text if not text.isupper() else ""


def framework_element_code(indicator: str, value: str | None) -> str:
    canonical = canonical_framework_element(indicator, value)
    return FRAMEWORK_ELEMENT_NAME_TO_CODE.get(indicator, {}).get(canonical, "")


def framework_legal_function_supported(indicator: str, element: str | None, legal_function: str | None) -> tuple[bool, str]:
    canonical = canonical_framework_element(indicator, element)
    function = str(legal_function or "").strip()
    if not canonical or not function:
        return False, "FRAMEWORK_ELEMENT_REVIEW_FIELDS"
    if function in FRAMEWORK_ADMINISTRATIVE_FUNCTIONS and function not in FRAMEWORK_LEGAL_FUNCTION_ALLOWLIST.get(indicator, {}).get(canonical, set()):
        return False, f"FRAMEWORK_LEGAL_FUNCTION_{function.upper()}"
    allowed = FRAMEWORK_LEGAL_FUNCTION_ALLOWLIST.get(indicator, {}).get(canonical, set())
    if function not in allowed:
        return False, "FRAMEWORK_ELEMENT_LEGAL_FUNCTION_MISMATCH"
    return True, ""


P6_SOURCE_FAMILIES = (
    "data protection",
    "corporate and accounting",
    "financial services",
    "securities",
    "payment services",
    "telecommunications",
    "health",
    "digital services",
)

INDICATOR_SPECS: dict[str, IndicatorSpec] = {
    "P4-I1": IndicatorSpec(
        indicator_id="P4-I1",
        decision_group="P4_PATENT_APPLICATION",
        title="Patent application issues",
        required_elements=P4_REVIEW_ELEMENTS_BY_GROUP["P4_PATENT_APPLICATION"],
        excluded_cases=("ordinary forms, formats or deadlines", "ordinary patent-agent procedure", "definition or rule-making power only", "enforcement rather than application"),
        positive_expressions=("foreign applicant", "address for service", "local address", "resident agent", "local representative", "first application", "filing abroad", "substantive examination", "application fee"),
        functional_expressions=("must provide a local address for service", "must appoint", "must first apply", "shall not file abroad", "must undergo substantive examination", "additional application burden"),
        object_terms=("patent application", "applicant", "foreign applicant", "inventor"),
        action_terms=("apply", "file", "appoint", "designate", "examine", "pay"),
        geographic_or_duration_terms=("foreign", "resident", "local", "abroad", "outside"),
        source_families=("patent", "patent procedure"),
        adjacent_indicators=("P4-I2", "P4-I3"),
        evidence_rules=("cite an operative application requirement", "identify the material burden and legal consequence"),
    ),
    "P4-I2": IndicatorSpec(
        indicator_id="P4-I2",
        decision_group="P4_PATENT_ENFORCEMENT",
        title="Patent enforcement procedures, remedies and provisional measures",
        required_elements=FRAMEWORK_REVIEW_ELEMENTS["P4-I2"],
        excluded_cases=("definition of infringement only", "general jurisdiction only", "criminal penalty only", "non-patent remedy", "limitation, defence or unavailability of a remedy"),
        positive_expressions=("patent infringement proceedings", "injunction", "damages", "account of profits", "interim injunction", "evidence preservation"),
        functional_expressions=("bring proceedings", "grant an injunction", "award damages", "order seizure", "interim relief"),
        object_terms=("patent", "patentee", "patent infringement", "infringing product"),
        action_terms=("enjoin", "restrain", "award", "seize", "destroy", "preserve"),
        geographic_or_duration_terms=("interim", "preliminary", "interlocutory", "ex parte", "before final judgment"),
        source_families=("patent", "civil procedure", "court rules"),
        adjacent_indicators=("P4-I3",),
        assessment_unit="framework",
        evidence_rules=("map operative evidence at framework-element level", "do not infer completeness from one provision"),
        framework_or_provision_type="framework",
    ),
    "P4-I3": IndicatorSpec(
        indicator_id="P4-I3",
        decision_group="P4_PATENT_ENFORCEMENT",
        title="Other patent enforcement issues",
        required_elements=P4_REVIEW_ELEMENTS_BY_GROUP["P4_PATENT_ENFORCEMENT"],
        excluded_cases=("keyword only", "ordinary limited public-interest exception", "application procedure", "ordinary infringement remedy"),
        positive_expressions=("working requirement", "compulsory licence", "government use", "crown use", "public interest", "national emergency"),
        functional_expressions=("must work", "may grant a compulsory licence", "government may use", "restrict exercise of patent"),
        object_terms=("patent", "patentee", "patented invention", "patent right"),
        action_terms=("work", "use", "license", "restrict", "authorize", "revoke"),
        geographic_or_duration_terms=("local", "domestic", "sector", "industry", "emergency"),
        source_families=("patent", "competition", "government use"),
        adjacent_indicators=("P4-I1", "P4-I2"),
        evidence_rules=("cite the complete operative restriction and triggering conditions", "extract compensation, safeguards and review where stated"),
    ),
    "P4-I4": IndicatorSpec(
        indicator_id="P4-I4",
        decision_group="P4_TREATY_STATUS",
        title="Patent Cooperation Treaty status",
        required_elements=("OFFICIAL_STATUS", "EFFECTIVE_DATE_OR_LAST_CHECKED", "OFFICIAL_SOURCE"),
        excluded_cases=("domestic-law reference only", "non-official source"),
        positive_expressions=("Patent Cooperation Treaty", "PCT"),
        functional_expressions=("contracting state", "in force", "not party"),
        object_terms=("Patent Cooperation Treaty",),
        action_terms=("accede", "ratify", "enter into force"),
        geographic_or_duration_terms=("effective date", "last checked"),
        source_families=("official WIPO treaty status",),
        adjacent_indicators=(),
        assessment_unit="economy_status",
        evidence_rules=("use only the local audited official-status registry",),
        framework_or_provision_type="external_status",
    ),
    "P4-I5": IndicatorSpec(
        indicator_id="P4-I5",
        decision_group="P4_COPYRIGHT_FRAMEWORK",
        title="Copyright framework and exceptions",
        required_elements=FRAMEWORK_REVIEW_ELEMENTS["P4-I5"],
        excluded_cases=("act title only", "registration only", "criminal penalty only", "scope, procedure, inspection or recordkeeping only", "abstract three-step test without an operative exception"),
        positive_expressions=("copyright subsists", "exclusive right", "copyright infringement", "fair dealing", "fair use", "permitted use", "research or study", "criticism or review", "accessibility"),
        functional_expressions=("protects copyright", "exclusive right", "does not infringe", "permitted use"),
        object_terms=("copyright", "work", "author", "owner", "fair dealing", "exception"),
        action_terms=("copy", "communicate", "perform", "make available", "permit", "infringe"),
        geographic_or_duration_terms=(),
        source_families=("copyright",),
        adjacent_indicators=("P4-I6",),
        assessment_unit="framework",
        evidence_rules=("map protection and exception elements separately", "do not infer completeness from one exception"),
        framework_or_provision_type="framework",
    ),
    "P4-I6": IndicatorSpec(
        indicator_id="P4-I6",
        decision_group="P4_ONLINE_COPYRIGHT",
        title="Online copyright enforcement",
        required_elements=FRAMEWORK_REVIEW_ELEMENTS["P4-I6"],
        excluded_cases=("no online nexus", "ordinary offline copyright remedy", "intermediary safe harbour only", "non-copyright website blocking"),
        positive_expressions=("flagrantly infringing online location", "online location", "website blocking", "network connection provider", "internet service provider", "communication to the public", "making available", "streaming", "electronic transmission"),
        functional_expressions=("access disabling order", "disable access", "block access", "blocking injunction", "online injunction", "stop online infringement", "temporary blocking"),
        object_terms=("copyright", "online location", "website", "digital copy", "network service"),
        action_terms=("block", "disable", "remove", "restrain", "communicate", "stream", "upload"),
        geographic_or_duration_terms=("online", "internet", "website", "digital", "electronic", "network"),
        source_families=("copyright", "civil procedure", "court rules"),
        adjacent_indicators=("P4-I5", "P8-I1"),
        assessment_unit="framework",
        evidence_rules=("require an explicit online nexus", "map ordinary and provisional online remedies separately"),
        framework_or_provision_type="framework",
    ),
    "P4-I7": IndicatorSpec(
        indicator_id="P4-I7",
        decision_group="P4_TREATY_STATUS",
        title="WIPO Copyright Treaty status",
        required_elements=("OFFICIAL_STATUS", "EFFECTIVE_DATE_OR_LAST_CHECKED", "OFFICIAL_SOURCE"),
        excluded_cases=("domestic-law reference only", "non-official source"),
        positive_expressions=("WIPO Copyright Treaty", "WCT"),
        functional_expressions=("contracting party", "in force", "not party"),
        object_terms=("WIPO Copyright Treaty",),
        action_terms=("accede", "ratify", "enter into force"),
        geographic_or_duration_terms=("effective date", "last checked"),
        source_families=("official WIPO treaty status",),
        adjacent_indicators=(),
        assessment_unit="economy_status",
        evidence_rules=("use only the local audited official-status registry",),
        framework_or_provision_type="external_status",
    ),
    "P4-I8": IndicatorSpec(
        indicator_id="P4-I8",
        decision_group="P4_TREATY_STATUS",
        title="WIPO Performances and Phonograms Treaty status",
        required_elements=("OFFICIAL_STATUS", "EFFECTIVE_DATE_OR_LAST_CHECKED", "OFFICIAL_SOURCE"),
        excluded_cases=("domestic-law reference only", "non-official source"),
        positive_expressions=("WIPO Performances and Phonograms Treaty", "WPPT"),
        functional_expressions=("contracting party", "in force", "not party"),
        object_terms=("WIPO Performances and Phonograms Treaty",),
        action_terms=("accede", "ratify", "enter into force"),
        geographic_or_duration_terms=("effective date", "last checked"),
        source_families=("official WIPO treaty status",),
        adjacent_indicators=(),
        assessment_unit="economy_status",
        evidence_rules=("use only the local audited official-status registry",),
        framework_or_provision_type="external_status",
    ),
    "P4-I9": IndicatorSpec(
        indicator_id="P4-I9",
        decision_group="P4_DISCLOSURE",
        title="Mandatory disclosure of trade secrets, source code or algorithms",
        required_elements=P4_REVIEW_ELEMENTS_BY_GROUP["P4_DISCLOSURE"],
        excluded_cases=("ordinary records or reports", "voluntary disclosure", "no protected subject", "no legal compulsion", "private contract", "government procurement condition", "ordinary discovery with adequate safeguards"),
        positive_expressions=("trade secret", "confidential business information", "source code", "algorithm", "proprietary information", "technical specification"),
        functional_expressions=("must disclose", "must provide", "must submit", "must produce", "grant access", "court may order production"),
        object_terms=("trade secret", "source code", "algorithm", "confidential business information", "proprietary information", "technical information"),
        action_terms=("disclose", "provide", "submit", "produce", "surrender", "grant access"),
        geographic_or_duration_terms=("penalty", "refuse licence", "market access", "court order", "inspection"),
        source_families=("trade secret", "civil procedure", "court rules", "regulation", "national security"),
        adjacent_indicators=("P4-I10", "P2-I2"),
        evidence_rules=("identify protected subject, information holder, compelled actor, receiving authority, disclosure action and legal compulsion", "confirm disclosure direction runs from holder to authority", "extract safeguards"),
    ),
    "P4-I10": IndicatorSpec(
        indicator_id="P4-I10",
        decision_group="P4_TRADE_SECRET_FRAMEWORK",
        title="Effective trade secrets legal framework",
        required_elements=FRAMEWORK_REVIEW_ELEMENTS["P4-I10"],
        excluded_cases=("official secrecy only", "single contractual confidentiality duty only", "state secrets only", "definition only", "narrow sectoral duty only"),
        positive_expressions=("trade secret", "breach of confidence", "confidential information", "misappropriation", "unauthorised disclosure", "account of profits"),
        functional_expressions=("must not disclose", "liable for breach of confidence", "grant injunction", "award damages", "protect confidential information"),
        object_terms=("trade secret", "confidential information", "proprietary information", "commercially valuable information"),
        action_terms=("acquire", "use", "disclose", "restrain", "enjoin", "award", "destroy"),
        geographic_or_duration_terms=(),
        source_families=("trade secret", "confidentiality", "equity", "civil procedure", "court rules"),
        adjacent_indicators=("P4-I9",),
        assessment_unit="framework",
        evidence_rules=("map statutory, common-law/case-law and remedy elements separately", "absence requires sufficient source coverage"),
        framework_or_provision_type="framework",
    ),
    "P6-I5": IndicatorSpec(
        indicator_id="P6-I5",
        decision_group="P6_EXTERNAL_STATUS",
        required_elements=("SINGAPORE_PARTY", "AGREEMENT_IN_FORCE", "BINDING_DATA_TRANSFER_COMMITMENT"),
        optional_elements=("effective date", "official treaty text"),
        excluded_cases=("aspirational cooperation only", "signed not in force", "non-binding language"),
        positive_expressions=("cross-border transfer of information by electronic means", "shall allow", "shall not prevent"),
        functional_expressions=("agreement in force", "binding commitment", "shall"),
        object_terms=("information by electronic means", "data transfer", "data flow"),
        action_terms=("allow", "not prevent", "permit"),
        geographic_or_duration_terms=("in force", "effective date"),
        source_families=("official treaty source", "official government status source"),
        adjacent_indicators=(),
        assessment_unit="agreement",
        evidence_rules=("official participation source", "official agreement text", "binding article text"),
        framework_or_provision_type="external_status",
    ),
    "P7-I1": IndicatorSpec(
        indicator_id="P7-I1",
        decision_group="P7_DATA_PROTECTION_FRAMEWORK",
        required_elements=("PERSONAL_DATA_SCOPE", "SUBSTANTIVE_DATA_PROTECTION_RULES", "REGULATOR_OR_ENFORCEMENT"),
        optional_elements=("complaints", "investigation powers", "penalties", "remedies"),
        excluded_cases=("confidentiality only", "non-binding policy", "cybersecurity only", "data offence only"),
        positive_expressions=("personal data means", "organisation must", "access request", "correction request", "commission may", "financial penalty"),
        functional_expressions=("data protection framework", "data subject rights", "privacy obligations", "enforcement"),
        object_terms=("personal data", "individual", "organisation", "commission"),
        action_terms=("collect", "use", "disclose", "protect", "access", "correct", "enforce"),
        geographic_or_duration_terms=(),
        source_families=("data protection", "privacy"),
        adjacent_indicators=("P7-I2",),
        assessment_unit="framework",
        evidence_rules=("principal law", "supporting instruments", "framework evidence bundle"),
        framework_or_provision_type="framework",
    ),
    "P7-I2": IndicatorSpec(
        indicator_id="P7-I2",
        decision_group="P7_CYBERSECURITY_FRAMEWORK",
        required_elements=("CYBERSECURITY_SCOPE", "SECURITY_OR_INCIDENT_OBLIGATIONS", "AUTHORITY_OR_ENFORCEMENT"),
        optional_elements=("licensing", "directions", "penalties", "subsidiary regulations"),
        excluded_cases=("single security duty only", "ordinary confidentiality", "non-binding strategy", "data protection security only"),
        positive_expressions=("critical information infrastructure", "cybersecurity incident", "commissioner of cybersecurity", "risk assessment", "audit"),
        functional_expressions=("cybersecurity framework", "incident reporting", "security duties", "CII obligations"),
        object_terms=("cybersecurity", "critical information infrastructure", "computer system", "network"),
        action_terms=("protect", "report", "audit", "assess", "direct", "investigate"),
        geographic_or_duration_terms=(),
        source_families=("cybersecurity", "digital services"),
        adjacent_indicators=("P7-I1",),
        assessment_unit="framework",
        evidence_rules=("principal law", "supporting instruments", "framework evidence bundle"),
        framework_or_provision_type="framework",
    ),
    "P6-I1": IndicatorSpec(
        indicator_id="P6-I1",
        decision_group="P6_LOCATION",
        required_elements=("OPERATIVE_RULE", "INFORMATION_BEARING_OBJECT", "CROSS_BORDER_OR_PROCESSING_LOCATION_NEXUS", "TRANSFER_PROHIBITION_OR_DOMESTIC_PROCESSING_REQUIREMENT", "NO_ORDINARY_COMPLIANCE_PATH"),
        excluded_cases=("ordinary conditional transfer path", "local copy only", "local infrastructure only", "government information exchange", "non-data transfer"),
        positive_expressions=("must not transfer data outside", "shall not be transferred abroad", "processing must take place in", "data must be processed domestically", "may only be processed within"),
        functional_expressions=("prohibit transfer", "local processing", "domestic processing", "offshore processing prohibited"),
        object_terms=("data", "personal data", "customer data", "user data", "communications data", "commercial information", "digital information"),
        action_terms=("transfer", "process", "disclose", "send", "provide", "make available"),
        geographic_or_duration_terms=("outside singapore", "overseas", "foreign country", "within singapore", "in singapore", "domestically"),
        source_families=P6_SOURCE_FAMILIES,
        adjacent_indicators=("P6-I2", "P6-I3", "P6-I4"),
    ),
    "P6-I2": IndicatorSpec(
        indicator_id="P6-I2",
        decision_group="P6_LOCATION",
        required_elements=("OPERATIVE_RULE", "INFORMATION_BEARING_OBJECT", "LOCAL_STORAGE_ACTION", "EXPLICIT_DOMESTIC_STORAGE_LOCATION"),
        excluded_cases=("retention period only", "submission to regulator only", "inspection availability only", "local server requirement", "government internal data"),
        positive_expressions=("copy shall be kept in singapore", "records must be maintained in singapore", "data shall be stored within singapore", "register is kept in singapore", "sent to and kept at a place in singapore", "duplicate register kept in singapore"),
        functional_expressions=("keep in singapore", "maintain in singapore", "store within singapore", "retain locally", "local copy", "send and keep"),
        object_terms=("data", "information", "records", "books", "books and records", "accounting records", "register", "statements", "returns", "accounts", "transactions", "copy", "duplicate", "backup", "database"),
        action_terms=("keep", "maintain", "store", "retain", "preserve", "hold", "send and keep", "lodge and maintain"),
        geographic_or_duration_terms=("in singapore", "within singapore", "at a place in singapore", "at its registered office in singapore", "locally", "local copy"),
        source_families=P6_SOURCE_FAMILIES,
        adjacent_indicators=("P6-I1", "P6-I3", "P6-I4"),
    ),
    "P6-I3": IndicatorSpec(
        indicator_id="P6-I3",
        decision_group="P6_LOCATION",
        required_elements=("OPERATIVE_RULE", "DIGITAL_INFRASTRUCTURE_OBJECT", "DOMESTIC_INFRASTRUCTURE_DEPLOYMENT"),
        excluded_cases=("ordinary facility", "registered office", "paper register", "repository", "government system only", "network security control only"),
        positive_expressions=("locate its servers within singapore", "maintain at least one server in singapore", "establish a data centre in singapore", "operate domestic cloud infrastructure", "host the service on infrastructure located in singapore"),
        functional_expressions=("locate server", "maintain server", "establish data centre", "operate cloud infrastructure", "host locally"),
        object_terms=("server", "local server", "data centre", "data center", "cloud infrastructure", "computing infrastructure", "hosting infrastructure", "database infrastructure", "data processing facility", "network service platform infrastructure"),
        action_terms=("locate", "establish", "maintain", "operate", "host", "deploy"),
        geographic_or_duration_terms=("in singapore", "within singapore", "locally", "domestic", "located in singapore"),
        source_families=("telecommunications", "digital services", "financial services", "cybersecurity", "payment services"),
        adjacent_indicators=("P6-I1", "P6-I2", "P6-I4"),
    ),
    "P6-I4": IndicatorSpec(
        indicator_id="P6-I4",
        decision_group="P6_LOCATION",
        required_elements=("OPERATIVE_RULE", "INFORMATION_BEARING_OBJECT", "CROSS_BORDER_NEXUS", "CONDITIONAL_TRANSFER_PATH"),
        excluded_cases=("absolute ban", "local processing requirement", "local copy only", "government information sharing", "non-data transfer"),
        positive_expressions=("must not transfer unless", "transfer is permitted where", "country providing comparable protection", "legally enforceable safeguards", "prescribed requirements are met"),
        functional_expressions=("conditional transfer", "comparable protection", "contractual safeguards", "consent", "approval", "prescribed requirements"),
        object_terms=("data", "personal data", "customer information", "user information", "commercial information", "digital data"),
        action_terms=("transfer", "send", "disclose", "provide", "make available"),
        geographic_or_duration_terms=("outside singapore", "overseas", "foreign country", "recipient in another country", "country or territory outside singapore"),
        source_families=P6_SOURCE_FAMILIES,
        adjacent_indicators=("P6-I1", "P6-I2", "P6-I3"),
    ),
    "P7-I3": IndicatorSpec(
        indicator_id="P7-I3",
        decision_group="P7_RETENTION",
        required_elements=("OPERATIVE_RULE", "INFORMATION_BEARING_OBJECT", "MINIMUM_RETENTION_DURATION"),
        excluded_cases=("maximum retention only", "no duration", "future regulation-making power only", "physical facility logs only", "procedure or penalty only"),
        positive_expressions=("at least", "not less than", "for a period of", "until", "years after", "months after"),
        functional_expressions=("retain", "keep", "preserve", "maintain"),
        object_terms=("personal data", "customer data", "user data", "subscriber data", "communications data", "account records", "payment records", "loan records", "transaction records", "accounting records", "tax records", "invoice records", "business transaction records", "financial records", "cybersecurity incident records", "electronic commercial records", "statutory operational records", "sectoral operational records"),
        action_terms=("retain", "keep", "preserve", "maintain"),
        geographic_or_duration_terms=("at least", "not less than", "for a period of", "years after", "months after", "days after", "minimum period"),
        source_families=("financial services", "payment services", "telecommunications", "data protection", "tax", "corporate and accounting", "digital services", "cybersecurity", "transport", "energy", "environmental", "health", "sector regulation"),
        adjacent_indicators=(),
    ),
    "P7-I4": IndicatorSpec(
        indicator_id="P7-I4",
        decision_group="P7_ACCOUNTABILITY",
        required_elements=("OPERATIVE_RULE", "DPO_OR_DPIA_OBLIGATION", "PRIVACY_COMPLIANCE_FUNCTION"),
        excluded_cases=("ordinary officer", "ordinary manager", "general compliance only", "cybersecurity-only officer", "voluntary recommendation"),
        positive_expressions=("designate one or more individuals", "responsible for ensuring compliance with this act", "person responsible for compliance", "accountable for protection of personal data", "officer responsible for privacy matters", "privacy impact assessment", "data protection impact assessment"),
        functional_expressions=("designate", "appoint", "nominate", "responsible for", "conduct impact assessment", "carry out impact assessment"),
        object_terms=("personal data", "data protection", "privacy", "compliance with this act", "protection of personal data"),
        action_terms=("designate", "appoint", "nominate", "conduct", "carry out", "ensure"),
        geographic_or_duration_terms=(),
        source_families=("data protection", "privacy", "digital services"),
        adjacent_indicators=(),
    ),
    "P7-I5": IndicatorSpec(
        indicator_id="P7-I5",
        decision_group="P7_GOVERNMENT_ACCESS",
        required_elements=("PUBLIC_AUTHORITY", "COMPULSORY_ACCESS_POWER", "EXTERNALLY_HELD_DATA", "PERSONAL_OR_IDENTIFIABLE_DATA"),
        excluded_cases=("official secrecy", "government internal sharing", "public register", "voluntary disclosure", "aggregate statistics", "future rule-making only"),
        positive_expressions=("require production", "obtain computer data", "access subscriber information", "inspect and copy", "produce customer information", "intercept communications"),
        functional_expressions=("require", "compel", "obtain", "access", "inspect", "retrieve", "query", "copy", "produce", "seize", "intercept", "furnish"),
        object_terms=("personal data", "customer information", "user data", "subscriber information", "communications data", "traffic data", "location data", "computer data", "account information", "transaction information", "identification records"),
        action_terms=("require", "compel", "obtain", "access", "inspect", "retrieve", "query", "copy", "produce", "seize", "intercept", "furnish"),
        geographic_or_duration_terms=(),
        source_families=("criminal procedure", "telecommunications", "cybercrime", "financial services", "tax", "digital services", "data protection"),
        adjacent_indicators=(),
    ),
}

DECISION_GROUPS: dict[str, tuple[str, ...]] = {
    "P4_PATENT_APPLICATION": ("P4-I1",),
    "P4_PATENT_ENFORCEMENT": ("P4-I3",),
    "P4_COPYRIGHT_FRAMEWORK": ("P4-I5",),
    "P4_ONLINE_COPYRIGHT": ("P4-I6",),
    "P4_DISCLOSURE": ("P4-I9",),
    "P4_TRADE_SECRET_FRAMEWORK": ("P4-I10",),
    "P4_TREATY_STATUS": ("P4-I4", "P4-I7", "P4-I8"),
    "P6_LOCATION": ("P6-I1", "P6-I2", "P6-I3", "P6-I4"),
    "P6_TREATY": ("P6-I5",),
    "P7_DATA_PROTECTION_FRAMEWORK": ("P7-I1",),
    "P7_CYBERSECURITY_FRAMEWORK": ("P7-I2",),
    "P7_RETENTION": ("P7-I3",),
    "P7_ACCOUNTABILITY": ("P7-I4",),
    "P7_GOVERNMENT_ACCESS": ("P7-I5",),
}

P4_GROUP_PROMPT_ORDER = (
    "P4_PATENT_APPLICATION",
    "P4_PATENT_ENFORCEMENT",
    "P4_COPYRIGHT_FRAMEWORK",
    "P4_ONLINE_COPYRIGHT",
    "P4_DISCLOSURE",
    "P4_TRADE_SECRET_FRAMEWORK",
    "P4_TREATY_STATUS",
)

GROUP_PROMPT_ORDER = ("P6_LOCATION", "P6_TREATY", "P7_DATA_PROTECTION_FRAMEWORK", "P7_CYBERSECURITY_FRAMEWORK", "P7_RETENTION", "P7_ACCOUNTABILITY", "P7_GOVERNMENT_ACCESS")


def specs_for_group(decision_group: str) -> tuple[IndicatorSpec, ...]:
    return tuple(INDICATOR_SPECS[indicator] for indicator in DECISION_GROUPS[decision_group])


def source_family_hit(title_text: str, families: tuple[str, ...]) -> bool:
    text = title_text.casefold()
    aliases = {
        "data protection": ("personal data", "data protection", "privacy"),
        "corporate and accounting": ("companies", "accounting", "corporate", "acra", "business names", "limited liability partnerships"),
        "financial services": ("bank", "financial", "finance", "insurance", "securities", "futures", "capital markets", "moneylenders"),
        "securities": ("securities", "futures", "capital markets"),
        "payment services": ("payment services", "payment", "money-changing", "remittance"),
        "telecommunications": ("telecommunication", "telecommunications", "telecom", "media development"),
        "health": ("health", "medical", "healthcare"),
        "digital services": ("electronic", "online", "digital", "platform", "computer"),
        "cybersecurity": ("cybersecurity", "computer misuse", "critical information infrastructure"),
        "tax": ("income tax", "goods and services tax", "tax", "customs"),
        "criminal procedure": ("criminal procedure", "corruption", "police", "evidence", "misuse of computers"),
        "cybercrime": ("computer misuse", "cybercrime", "misuse of computers"),
        "privacy": ("privacy", "personal data"),
        "patent": ("patent", "patents"),
        "patent procedure": ("patent", "patents", "patent agent"),
        "copyright": ("copyright",),
        "trade secret": ("trade secret", "confidential information", "confidentiality"),
        "confidentiality": ("confidence", "confidential", "secrecy"),
        "civil procedure": ("civil procedure", "rules of court", "court rules"),
        "court rules": ("rules of court", "court rules", "federal court rules", "high court rules"),
        "competition": ("competition", "patent"),
        "government use": ("government use", "crown use", "patent"),
        "equity": ("equity", "breach of confidence"),
        "regulation": ("regulation", "regulations"),
        "national security": ("national security", "security"),
    }
    return any(any(alias in text for alias in aliases.get(family, (family,))) for family in families)
