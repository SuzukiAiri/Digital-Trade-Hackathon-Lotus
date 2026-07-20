"""Core models for RDTII legal-text mapping."""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


IndicatorId = Literal[
    "P4-I1",
    "P4-I2",
    "P4-I3",
    "P4-I4",
    "P4-I5",
    "P4-I6",
    "P4-I7",
    "P4-I8",
    "P4-I9",
    "P4-I10",
    "P6-I1",
    "P6-I2",
    "P6-I3",
    "P6-I4",
    "P6-I5",
    "P7-I1",
    "P7-I2",
    "P7-I3",
    "P7-I4",
    "P7-I5",
]
P67IndicatorId = Literal[
    "P6-I1",
    "P6-I2",
    "P6-I3",
    "P6-I4",
    "P6-I5",
    "P7-I1",
    "P7-I2",
    "P7-I3",
    "P7-I4",
    "P7-I5",
]

ElementStatus = Literal["present", "supported_by_context", "absent", "uncertain", "not_applicable"]
ReviewerElementStatus = Literal["supported", "not_supported", "uncertain"]
ReviewerExclusionStatus = Literal["triggered", "not_triggered", "uncertain"]
FocalIntegrityStatus = Literal["ok", "source_mismatch", "incomplete_context", "technical_mismatch", "uncertain"]
FocalRole = Literal["operative", "supporting_only", "uncertain"]
RecordScopeBasis = Literal[
    "PERSONAL_CUSTOMER_USER",
    "ACCOUNT_PAYMENT_TRANSACTION",
    "AML_KYC",
    "ACCOUNTING_TAX_FINANCIAL",
    "COMMUNICATIONS_PLATFORM_DIGITAL_SERVICE",
    "AUTHENTICATION_CYBERSECURITY_SYSTEM_EVENT",
    "PERSON_OR_TRANSACTION_TRACEABILITY",
    "OPERATIONAL_SECTOR_RECORD",
    "PHYSICAL_OPERATIONAL_ONLY",
    "UNCERTAIN",
    "NONE",
]
JudicialAuthorization = Literal["required", "not_required", "emergency_exception", "mixed", "uncertain"]
AccountabilityPath = Literal["dpo", "dpia", "dpo_and_dpia", "uncertain"]
QueueType = Literal["none", "model_review_pending", "human_legal_review", "technical_repair", "external_source"]
ResultCode = Literal[
    "NO_MATCH",
    "REQUIRED_ELEMENT_MISSING",
    "EXCLUSION_TRIGGERED",
    "FRAMEWORK_FOCAL_NOT_INDEPENDENT",
    "LEGAL_UNCERTAINTY",
    "TECHNICAL_INPUT_ERROR",
    "MODEL_ERROR",
    "EXTERNAL_SOURCE_UNAVAILABLE",
]

RouteTopic = Literal[
    "P4_PATENT_APPLICATION",
    "P4_PATENT_ENFORCEMENT",
    "P4_COPYRIGHT_FRAMEWORK",
    "P4_ONLINE_COPYRIGHT",
    "P4_DISCLOSURE",
    "P4_TRADE_SECRET_FRAMEWORK",
    "P4_TREATY_STATUS",
    "P6_LOCATION",
    "P6_TREATY",
    "P7_DATA_PROTECTION_FRAMEWORK",
    "P7_CYBERSECURITY_FRAMEWORK",
    "P7_RETENTION",
    "P7_ACCOUNTABILITY",
    "P7_GOVERNMENT_ACCESS",
]

RecallSource = Literal["primary_router", "audit_promoted", "primary_and_audit"]


class ProvisionContext(Model):
    economy: str = "Singapore"
    document_id: str
    law_title: str
    instrument_type: str
    source_url: str | None
    provision_id: str
    processing_mode: str = "structured_provisions"
    citation_mode: str = "structured_provision"
    source_locator: str = ""
    focal_text_hash: str = ""
    document_content_hash: str = ""
    canonical_schema_version: str = ""
    provision_metadata_snapshot: dict = Field(default_factory=dict)
    section_reference: str
    text: str
    part_heading: str = ""
    division_heading: str = ""


class CandidateTask(Model):
    task_id: str
    economy: str
    indicator_id: IndicatorId | None = None
    task_kind: Literal["provision", "framework_element", "treaty_provision", "external_status", "document_claim"] = "provision"
    source_type: Literal["domestic_legislation", "subsidiary_legislation", "treaty", "external_registry", "pdf_document"] = "domestic_legislation"
    document_id: str
    source_record_id: str = ""
    law_title: str
    instrument_type: str
    parent_instrument: str | None = None
    focal_provision_id: str
    focal_quote: str = ""
    processing_mode: str = "structured_provisions"
    citation_mode: str = "structured_provision"
    source_locator: str = ""
    focal_text_hash: str = ""
    document_content_hash: str = ""
    canonical_schema_version: str = ""
    provision_metadata_snapshot: dict = Field(default_factory=dict)
    normalized_provision_id: str
    section_heading: str | None = None
    focal_text: str
    supporting_provision_ids: list[str] = Field(default_factory=list)
    parent_section_text: str
    supporting_context: str
    route_topic: RouteTopic
    candidate_indicators: list[IndicatorId]
    source_url: str | None
    contract_version: str = ""
    evidence_segments: dict[str, str] = Field(default_factory=dict)
    recall_source: RecallSource
    matched_patterns: list[str] = Field(default_factory=list)
    audit_confidence: Literal["high", "medium", "low"] | None = None


EvidenceTask = CandidateTask


class PDFDocumentTask(Model):
    task_id: str
    economy: str
    document_id: str
    collection: str
    title: str
    official_number: str = ""
    year: str = ""
    language: str = ""
    source_url: str
    raw_path: str
    pdf_text_path: str = ""
    document_text_hash: str = ""
    candidate_indicators: list[IndicatorId] = Field(default_factory=list)
    matched_pages: list[int] = Field(default_factory=list)
    matched_context: str = ""
    prefilter_status: Literal["candidate", "reject", "uncertain", "pass", "relevant", "review"] = "uncertain"
    source_sha256: str
    page_count: int | None = None


class PDFClaimAttributes(Model):
    coverage: str | None = None
    sector: str | None = None
    framework_candidate_element: str | None = None
    framework_element: str | None = None
    framework_legal_function: str | None = None
    record_scope_basis: str | None = None
    judicial_authorization: str | None = None
    accountability_path: str | None = None
    minimum_duration_value: str | None = None
    minimum_duration_unit: str | None = None
    trigger_event: str | None = None


class PDFEvidenceClaim(Model):
    indicator_id: IndicatorId
    article: str
    page_number: int
    verbatim_snippet: str
    mapping_rationale: str
    coverage: Literal["horizontal", "sectoral", "uncertain"] = "uncertain"
    sector: str | None = None
    focal_role: FocalRole = "operative"
    confidence: float = 0.0
    elements: list[ReviewerElementAssessment] = Field(default_factory=list)
    exclusions: list[ReviewerExclusionAssessment] = Field(default_factory=list)
    attributes: PDFClaimAttributes = Field(default_factory=PDFClaimAttributes)


class PDFMappingDecision(Model):
    document_id: str
    document_decision: Literal["no_match", "claims_found", "uncertain", "technical_failure"]
    claims: list[PDFEvidenceClaim] = Field(default_factory=list)
    document_notes: str = ""


class P67PDFEvidenceClaim(PDFEvidenceClaim):
    model_config = ConfigDict(extra="forbid", title="PDFEvidenceClaim")
    indicator_id: P67IndicatorId


class P67PDFMappingDecision(Model):
    model_config = ConfigDict(extra="forbid", title="PDFMappingDecision")
    document_id: str
    document_decision: Literal["no_match", "claims_found", "uncertain", "technical_failure"]
    claims: list[P67PDFEvidenceClaim] = Field(default_factory=list)
    document_notes: str = ""


class PDFCitationVerification(Model):
    status: Literal["verified", "failed", "unverifiable"]
    matched_text: str = ""
    article_confirmed: bool = False
    reason: str = ""


class AdjacentIndicatorReason(Model):
    indicator: IndicatorId
    reason: str


class RequiredElementReview(Model):
    element_code: str = Field(...)
    status: ElementStatus = Field(...)
    evidence_ids: list[str] = Field(...)


class FocalIntegrityAssessment(Model):
    status: FocalIntegrityStatus = Field(...)
    reason: str = Field(...)


class ReviewerElementAssessment(Model):
    element_id: str = Field(validation_alias=AliasChoices("element_id", "element_code"))
    status: ReviewerElementStatus = Field(...)
    evidence_ids: list[str] = Field(...)
    reason: str = Field(...)

    @property
    def element_code(self) -> str:
        return self.element_id


class ReviewerExclusionAssessment(Model):
    exclusion_id: str = Field(validation_alias=AliasChoices("exclusion_id", "exclusion_code"))
    status: ReviewerExclusionStatus = Field(...)
    evidence_ids: list[str] = Field(...)
    reason: str = Field(...)

    @property
    def exclusion_code(self) -> str:
        return self.exclusion_id


class ReviewerOptionalCheck(Model):
    check_code: str = Field(...)
    status: str = Field(...)
    evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class EvidenceCatalogEntry(Model):
    evidence_id: str
    role: Literal["focal", "parent", "heading", "supporting"]
    provision_id: str
    text: str


class EvidenceSpan(Model):
    evidence_id: str
    text: str = ""


RegulatedObjectType = Literal[
    "data_or_information",
    "record_or_document",
    "financial_asset",
    "physical_goods",
    "biological_material",
    "service",
    "person",
    "unknown",
]

StorageLocationRelation = Literal[
    "storage_object",
    "storage_facility",
    "person",
    "activity",
    "treatment_or_birth",
    "business_scope",
    "address_only",
    "supporting_context_only",
    "unknown",
]

InfrastructureType = Literal["server", "data_centre", "cloud", "hosting", "computing_facility", "network_facility", "unknown"]
AccountabilityReviewPath = Literal["dpo", "dpia", "both", "none", "uncertain"]
FrameworkElement = Literal[
    "personal_data_scope",
    "substantive_duties_or_rights",
    "regulator_or_enforcement",
    "cybersecurity_scope",
    "substantive_cybersecurity_obligation",
    "authority_or_enforcement",
]
P4FrameworkElement = Literal[
    "ordinary_civil_or_administrative_remedies",
    "provisional_measures",
    "copyright_framework",
    "copyright_exceptions",
    "online_civil_or_administrative_remedies",
    "online_provisional_measures",
    "statutory_trade_secret_protection",
    "common_law_or_case_law_protection",
    "trade_secret_remedies",
]
FrameworkLegalFunction = Literal[
    "scope_rule",
    "substantive_duty",
    "individual_right",
    "regulator_power",
    "enforcement_power",
    "penalty_or_sanction",
    "definition",
    "application_rule",
    "exception",
    "procedural_rule",
    "appointment_or_delegation",
    "administrative_information",
    "cross_reference",
    "supporting_only",
    "incomplete_fragment",
    "unrelated",
]


class RetentionPeriod(Model):
    value: str
    unit: str
    condition: str | None = None
    trigger_event: str | None = None


class BaseTaskReview(Model):
    focal_role: FocalRole = "operative"
    required_elements: list[ReviewerElementAssessment] = Field(default_factory=list)
    exclusions: list[ReviewerExclusionAssessment] = Field(default_factory=list)
    decision: Literal["match", "no_match", "supporting_only", "uncertain"] = "uncertain"
    rationale: str = ""
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)


class P6TransferReview(BaseTaskReview):
    candidate_indicator: Literal["P6-I1", "P6-I4"]
    regulated_object_text: str = ""
    regulated_object_type: RegulatedObjectType = "unknown"
    information_bearing_object_evidence: str = ""
    operative_action: str = ""
    cross_border_direction: str = ""
    condition_or_exception: str | None = None


class P6StorageReview(BaseTaskReview):
    information_object_text: str = ""
    storage_action: str = ""
    location_text: str = ""
    location_relation: StorageLocationRelation = "unknown"


class P6InfrastructureReview(BaseTaskReview):
    infrastructure_object: str = ""
    infrastructure_type: InfrastructureType = "unknown"
    domestic_deployment_requirement: str = ""
    business_or_service_condition: str = ""


class P7RetentionReview(BaseTaskReview):
    record_object: str = ""
    retention_action: str = ""
    retention_periods: list[RetentionPeriod] = Field(default_factory=list)
    trigger_event: str | None = None
    government_data_only: bool | None = None


class P7AccountabilityReview(BaseTaskReview):
    accountability_path: AccountabilityReviewPath = "uncertain"
    mandatory_action: str = ""
    privacy_or_data_protection_function: str = ""


class P7GovernmentAccessReview(BaseTaskReview):
    public_authority: str = ""
    compulsory_power: str = ""
    external_holder: str = ""
    personal_or_identifiable_data: str = ""
    judicial_authorization: JudicialAuthorization = "uncertain"


class FrameworkElementReview(BaseTaskReview):
    indicator_id: Literal["P7-I1", "P7-I2"]
    candidate_element: FrameworkElement
    framework_element: FrameworkElement
    legal_function: FrameworkLegalFunction = "unrelated"
    operative_subject: str = ""
    operative_action: str = ""
    regulated_scope: str = ""
    coverage: Literal["horizontal", "sectoral", "uncertain"] = "uncertain"
    substantive_obligation: str = ""
    authority_or_enforcement_power: str = ""
    focal_supports_element: bool | None = None
    missing_elements: list[str] = Field(default_factory=list)
    uncertainty: str = ""


class P4PatentApplicationReview(BaseTaskReview):
    restriction_type: str = ""
    affected_applicant: str = ""
    foreign_or_local_distinction: str = ""
    operative_requirement: str = ""
    local_presence_or_service_requirement: str = ""
    first_filing_requirement: bool | None = None
    substantive_examination_requirement: bool | None = None
    fee_or_cost_burden: str = ""
    discriminatory_effect: str = ""
    legal_effect: str = ""


class P4PatentEnforcementOtherReview(BaseTaskReview):
    restriction_type: str = ""
    affected_patent_right: str = ""
    affected_sector_or_product: str = ""
    scope_horizontal_or_sectoral: str = ""
    triggering_conditions: str = ""
    compensation_available: str = ""
    procedural_safeguards: str = ""
    judicial_review: str = ""
    practical_legal_effect: str = ""


class P4MandatoryDisclosureReview(BaseTaskReview):
    protected_subject: str = ""
    information_holder: str = ""
    compelled_actor: str = ""
    receiving_authority: str = ""
    disclosure_action: str = ""
    triggering_condition: str = ""
    non_compliance_consequence: str = ""
    sector_or_product_scope: str = ""
    public_interest_basis: str = ""
    safeguards: list[str] = Field(default_factory=list)
    unfair_commercial_use_protection: str = ""


class P4FrameworkElementReview(BaseTaskReview):
    indicator_id: Literal["P4-I2", "P4-I5", "P4-I6", "P4-I10"]
    candidate_element: P4FrameworkElement
    framework_element: P4FrameworkElement
    legal_function: FrameworkLegalFunction = "unrelated"
    operative_subject: str = ""
    operative_action: str = ""
    regulated_scope: str = ""
    coverage: Literal["horizontal", "sectoral", "uncertain"] = "uncertain"
    online_nexus: str = ""
    evidence_character: Literal[
        "core_operative",
        "supporting_or_sectoral",
        "definition_only",
        "scope_only",
        "procedure_only",
        "cross_reference_only",
        "uncertain",
    ] = "uncertain"
    remedy_direction: Literal[
        "grants_remedy",
        "limits_remedy",
        "defence_or_immunity",
        "not_applicable",
        "uncertain",
    ] = "not_applicable"
    protected_private_or_commercial_information: bool | None = None
    unauthorised_acquisition_use_or_disclosure: bool | None = None
    government_or_official_only: bool | None = None
    focal_supports_element: bool | None = None
    missing_elements: list[str] = Field(default_factory=list)
    uncertainty: str = ""


class P4TreatyStatus(Model):
    economy: str
    indicator_id: Literal["P4-I4", "P4-I7", "P4-I8"]
    instrument: str
    status: Literal["party", "not_party", "uncertain"]
    accession_or_ratification_date: str = ""
    effective_date: str = ""
    official_source_url: str = ""
    last_checked: str = ""
    source_note: str = ""
    status_text: str = ""


class TreatyProvisionReview(BaseTaskReview):
    agreement_name: str = ""
    article: str = ""
    binding_commitment: str = ""
    data_flow_commitment: str = ""
    in_force_status: str = ""
    official_source: str = ""


class ReviewerAttributes(Model):
    coverage: str | None = None
    sector: str | None = None
    framework_candidate_element: FrameworkElement | None = None
    framework_element: FrameworkElement | None = None
    framework_legal_function: FrameworkLegalFunction | None = None
    record_scope_basis: RecordScopeBasis | None = None
    judicial_authorization: JudicialAuthorization | None = None
    accountability_path: AccountabilityPath | None = None
    minimum_duration_value: str | None = None
    minimum_duration_unit: str | None = None
    trigger_event: str | None = None


class IndicatorMatch(Model):
    indicator: IndicatorId | None = Field(...)
    legal_function: Literal[
        "operative_rule",
        "definition",
        "procedure",
        "penalty",
        "exception",
        "regulation_making_power",
        "other",
    ] = Field(...)
    actor: str | None = Field(...)
    modality: str | None = Field(...)
    action: str | None = Field(...)
    regulated_object: str | None = Field(...)
    object_type: str | None = Field(...)
    geographic_nexus: str | None = Field(...)
    duration: str | None = Field(...)
    conditions: list[str] = Field(...)
    operative_evidence_ids: list[str] = Field(...)
    supporting_evidence_ids: list[str] = Field(...)
    evidence_ids: list[str] = Field(...)
    required_element_status: list[RequiredElementReview] = Field(...)
    triggered_exclusions: list[str] = Field(...)
    why_included: str = Field(...)
    adjacent_indicator_analysis: list[AdjacentIndicatorReason] = Field(...)


class MappingDecision(Model):
    decision: Literal["match", "no_match", "uncertain"] = Field(...)
    matches: list[IndicatorMatch] = Field(...)
    rationale: str = Field(...)


class P67AdjacentIndicatorReason(Model):
    model_config = ConfigDict(extra="forbid", title="AdjacentIndicatorReason")
    indicator: P67IndicatorId
    reason: str


class P67IndicatorMatch(IndicatorMatch):
    model_config = ConfigDict(extra="forbid", title="IndicatorMatch")
    indicator: P67IndicatorId | None = Field(...)
    adjacent_indicator_analysis: list[P67AdjacentIndicatorReason] = Field(...)


class P67MappingDecision(Model):
    model_config = ConfigDict(extra="forbid", title="MappingDecision")
    decision: Literal["match", "no_match", "uncertain"] = Field(...)
    matches: list[P67IndicatorMatch] = Field(...)
    rationale: str = Field(...)


class ReviewerDecision(Model):
    focal_integrity: FocalIntegrityAssessment = Field(...)
    focal_role: FocalRole = "operative"
    elements: list[ReviewerElementAssessment] = Field(validation_alias=AliasChoices("elements", "element_assessments"))
    exclusions: list[ReviewerExclusionAssessment] = Field(validation_alias=AliasChoices("exclusions", "exclusion_assessments"))
    attributes: ReviewerAttributes = Field(default_factory=ReviewerAttributes)
    optional_checks: list[ReviewerOptionalCheck] = Field(default_factory=list)
    decision: Literal["match", "no_match", "supporting_only", "uncertain"] = "uncertain"
    review_reason: str = Field(...)

    @property
    def element_assessments(self) -> list[ReviewerElementAssessment]:
        return self.elements

    @property
    def exclusion_assessments(self) -> list[ReviewerExclusionAssessment]:
        return self.exclusions

    @property
    def record_scope_basis(self) -> RecordScopeBasis | None:
        return self.attributes.record_scope_basis


class ValidatedTaskResult(Model):
    task_id: str
    economy: str
    document_id: str
    law_title: str
    instrument_type: str
    source_url: str | None
    focal_provision_id: str
    route_topic: RouteTopic
    candidate_indicators: list[IndicatorId]
    status: Literal["accepted", "rejected", "supporting_only", "model_review_pending", "human_legal_review", "technical_repair", "review", "error"]
    queue_type: QueueType = "none"
    result_code: ResultCode | None = None
    indicator: IndicatorId | None
    decision: MappingDecision | None
    failure_codes: list[str] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    rationale: str
    accepted_matches: list[dict] = Field(default_factory=list)
    review_matches: list[dict] = Field(default_factory=list)
    prompt_version: str
    validation_version: str
    model_name: str
    cache_key: str
    llm_call: bool
    cache_hit: bool
    retries: int
    reviewer_model_name: str | None = None
    reviewer_llm_call: bool = False
    reviewer_cache_hit: bool = False
    reviewer_cache_key: str | None = None
    reviewer_decision: ReviewerDecision | None = None
    review_resolution_attempted: bool = False
    review_resolution_completed: bool = False
    review_resolution_notes: list[str] = Field(default_factory=list)
    failed_required_elements: list[str] = Field(default_factory=list)
    uncertain_elements: list[str] = Field(default_factory=list)
    uncertain_exclusions: list[str] = Field(default_factory=list)
    focal_uncertainty: str | None = None
    triggered_exclusions: list[str] = Field(default_factory=list)
    technical_detail: str | None = None
    affected_evidence_ids: list[str] = Field(default_factory=list)
    expected_repair_action: str | None = None
    external_source_detail: str | None = None
    focal_role: FocalRole | None = None
    record_scope_basis: RecordScopeBasis | None = None
    reviewer_attributes: ReviewerAttributes | None = None
    error: str | None
    warnings: list[str] = Field(default_factory=list)
    decision_source: Literal["model", "human_review", "deterministic"] = "model"
    human_review_id: str | None = None
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    human_validated_attributes: dict = Field(default_factory=dict)


class MeasureRecord(Model):
    economy: str
    indicator_id: IndicatorId
    document_id: str
    official_title: str
    instrument_type: str
    source_url: str | None
    section_references: list[str]
    verbatim_snippets: list[str]
    mapping_rationale: str
    evidence_task_ids: list[str]
    measure_type: Literal["provision", "framework", "external_status"]
    review_required: bool
    coverage: str
    confidence: Literal["high", "medium", "review"] = "medium"
    supporting_instruments: list[str] = Field(default_factory=list)


class AtomicEvidenceRecord(Model):
    evidence_id: str
    economy: str
    indicator_id: IndicatorId
    document_id: str
    law_name: str
    law_number_ref: str = ""
    last_amended: str = ""
    instrument_role: str = "unknown"
    principal_instrument_id: str = ""
    amends_instrument_id: str = ""
    consolidated_target: str = ""
    article: str
    location_reference: str
    focal_quote: str
    supporting_refs: list[str] = Field(default_factory=list)
    mapping_rationale: str
    source_url: str
    coverage: str
    sector: str
    discovery_tag: Literal["KNOWN", "NEW"]
    baseline_match_key: str = ""
    baseline_match_basis: str = ""
    baseline_row_id: str = ""
    baseline_file_hash: str = ""
    confidence: float = 0.7
    focal_role: str = ""
    decision: Literal["accepted", "rejected", "supporting_only", "human_legal_review", "technical_repair"]
    decision_reason: str = ""
    mapper_task_id: str
    reviewer_task_id: str | None = None
    citation_status: Literal["verified", "failed", "unverifiable"]
    citation_error: str = ""
    citation_mode: str = "structured_provision"
    canonical_provision_id: str | None = None
    source_locator: str = ""
    canonical_schema_version: str = ""
    source_text_hash: str = ""
    document_content_hash: str = ""
    citation_provenance: str = ""
    page_number: int | None = None
    printed_article: str | None = None
    notes: str = ""
    validated_attributes: dict = Field(default_factory=dict)
    decision_source: Literal["model", "human_review", "deterministic"] = "model"
    human_review_id: str | None = None
    reviewed_by: str | None = None
    reviewed_at: str | None = None
