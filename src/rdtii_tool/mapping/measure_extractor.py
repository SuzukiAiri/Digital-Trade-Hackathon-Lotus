"""LLM-backed measure decision for one CandidateTask."""

from __future__ import annotations

import json
import os
import time

from .indicator_specs import (
    FRAMEWORK_REVIEW_ELEMENTS,
    FRAMEWORK_REVIEW_EXCLUSIONS,
    canonical_framework_element,
    framework_element_code,
    INDICATOR_SPECS,
    P6_REVIEW_ELEMENTS,
    P6_REVIEW_EXCLUSIONS,
    P7_REVIEW_ELEMENTS_BY_GROUP,
    P7_REVIEW_EXCLUSIONS_BY_GROUP,
    P4_REVIEW_ELEMENTS_BY_GROUP,
    P4_REVIEW_EXCLUSIONS_BY_GROUP,
    P4_MAPPING_BOUNDARY_RULES,
    specs_for_group,
)
from .models import (
    CandidateTask,
    EvidenceCatalogEntry,
    FocalIntegrityAssessment,
    FrameworkElementReview,
    IndicatorMatch,
    MappingDecision,
    P6InfrastructureReview,
    P6StorageReview,
    P6TransferReview,
    P7AccountabilityReview,
    P7GovernmentAccessReview,
    P7RetentionReview,
    P4FrameworkElementReview,
    P4MandatoryDisclosureReview,
    P4PatentApplicationReview,
    P4PatentEnforcementOtherReview,
    P67MappingDecision,
    TreatyProvisionReview,
    ReviewerDecision,
    ReviewerElementAssessment,
    ReviewerExclusionAssessment,
    ReviewerAttributes,
    ReviewerOptionalCheck,
)
from .economy_profiles import economy_profile
from .model_config import (
    MAPPER_MODEL as DEFAULT_MAPPER_MODEL,
    REVIEWER_MODEL as DEFAULT_REVIEWER_MODEL,
    api_key_available,
    openai_client,
    mapper_model_name,
    reviewer_model_name,
)


DEFAULT_PROMPT_VERSION = "rdtii-measure-v8-executable-contracts"
CROSS_BORDER_PROMPT_VERSION = "rdtii-measure-v8-executable-contracts"
RETENTION_PROMPT_VERSION = "rdtii-measure-v8-executable-contracts"
FRAMEWORK_PROMPT_VERSION = "rdtii-framework-v3-status-only"
REVIEWER_PROMPT_VERSION = "rdtii-reviewer-v7-independent-evidence"
REVIEWER_SCHEMA_VERSION = "rdtii-reviewer-schema-v5-full-contract-elements"
REVIEWER_CACHE_CONTRACT_VERSION = "rdtii-reviewer-cache-contract-v2-stable-inputs"
REVIEWER_PROMPT_VERSION_EVIDENCE_CATALOG = "rdtii-reviewer-v7-evidence-catalog-independent"
P4_PROMPT_VERSION = "rdtii-p4-measure-v2-boundary-contracts"
P4_FRAMEWORK_PROMPT_VERSION = "rdtii-p4-framework-v2-element-quality"
P4_REVIEWER_PROMPT_VERSION = "rdtii-p4-reviewer-v2-direction-and-elements"
P4_REVIEWER_SCHEMA_VERSION = "rdtii-p4-reviewer-schema-v2-direction-and-elements"
REVIEWER_PROMPT_VERSIONS_BY_GROUP = {
    "P6_LOCATION": "rdtii-reviewer-v7-p6-situs-contract",
    "P7_RETENTION": "rdtii-reviewer-v7-retention-attributes",
    "P7_ACCOUNTABILITY": "rdtii-reviewer-v7-accountability-fanout",
    "P7_GOVERNMENT_ACCESS": REVIEWER_PROMPT_VERSION,
    "P4_PATENT_APPLICATION": P4_REVIEWER_PROMPT_VERSION,
    "P4_PATENT_ENFORCEMENT": P4_REVIEWER_PROMPT_VERSION,
    "P4_COPYRIGHT_FRAMEWORK": P4_REVIEWER_PROMPT_VERSION,
    "P4_ONLINE_COPYRIGHT": P4_REVIEWER_PROMPT_VERSION,
    "P4_DISCLOSURE": P4_REVIEWER_PROMPT_VERSION,
    "P4_TRADE_SECRET_FRAMEWORK": P4_REVIEWER_PROMPT_VERSION,
}
def prompt_version_for_task(task: CandidateTask) -> str:
    if task.route_topic in {
        "P4_PATENT_ENFORCEMENT",
        "P4_COPYRIGHT_FRAMEWORK",
        "P4_ONLINE_COPYRIGHT",
        "P4_TRADE_SECRET_FRAMEWORK",
    } and task.task_kind == "framework_element":
        return P4_FRAMEWORK_PROMPT_VERSION
    if task.route_topic.startswith("P4_"):
        return P4_PROMPT_VERSION
    if task.route_topic == "P6_LOCATION":
        return CROSS_BORDER_PROMPT_VERSION
    if task.route_topic == "P6_TREATY":
        return DEFAULT_PROMPT_VERSION
    if task.route_topic in {"P7_DATA_PROTECTION_FRAMEWORK", "P7_CYBERSECURITY_FRAMEWORK"}:
        return FRAMEWORK_PROMPT_VERSION
    if task.route_topic == "P7_RETENTION":
        return RETENTION_PROMPT_VERSION
    return DEFAULT_PROMPT_VERSION


def all_prompt_versions() -> dict[str, str]:
    return {
        "default": DEFAULT_PROMPT_VERSION,
        "P6_LOCATION": CROSS_BORDER_PROMPT_VERSION,
        "P7_RETENTION": RETENTION_PROMPT_VERSION,
        "P7_ACCOUNTABILITY": DEFAULT_PROMPT_VERSION,
        "P7_GOVERNMENT_ACCESS": DEFAULT_PROMPT_VERSION,
        "framework": FRAMEWORK_PROMPT_VERSION,
        "reviewer": REVIEWER_PROMPT_VERSION,
        "reviewer_schema": REVIEWER_SCHEMA_VERSION,
        "reviewer_cache_contract": REVIEWER_CACHE_CONTRACT_VERSION,
        "reviewer_evidence_catalog": REVIEWER_PROMPT_VERSION_EVIDENCE_CATALOG,
        "reviewer_by_group": dict(REVIEWER_PROMPT_VERSIONS_BY_GROUP),
        "p4": P4_PROMPT_VERSION,
        "p4_framework": P4_FRAMEWORK_PROMPT_VERSION,
        "p4_reviewer": P4_REVIEWER_PROMPT_VERSION,
        "p4_reviewer_schema": P4_REVIEWER_SCHEMA_VERSION,
    }


def reviewer_prompt_version_for_task(task: CandidateTask, *, evidence_catalog: bool = False) -> str:
    if evidence_catalog:
        return REVIEWER_PROMPT_VERSION_EVIDENCE_CATALOG
    return REVIEWER_PROMPT_VERSIONS_BY_GROUP.get(task.route_topic, REVIEWER_PROMPT_VERSION)

def build_prompt(task: CandidateTask) -> str:
    if task.route_topic.startswith("P4_"):
        return _build_p4_prompt(task)
    indicators = ", ".join(task.candidate_indicators)
    specs = _spec_text(task.route_topic, task.economy)
    segments = "\n".join(f"[{sid}] {text}" for sid, text in task.evidence_segments.items())
    return f"""You are mapping legal text to RDTII indicators.

Use only the provided legal text. Do not infer facts not written in the focal clause or supporting context.

Task:
1. Determine the focal clause's legal function.
2. Determine whether the focal clause creates an operative legal measure.
3. Check whether the regulated object and conduct fit decision_group={task.route_topic}.
4. Check all substantive requirements for candidate indicators: {indicators}.
5. Check exclusion conditions.
6. For P6_LOCATION, compare P6-I1, P6-I2, P6-I3 and P6-I4 in one decision.
7. Return zero, one, or multiple true matches. Do not force a match.
8. Use evidence_ids such as ["S1", "S2"]; do not invent quotes or section text.
9. For each proposed match, provide operative_evidence_ids, supporting_evidence_ids, evidence_ids, required_element_status, triggered_exclusions, and adjacent_indicator_analysis.
10. required_element_status must use exactly these statuses: present, supported_by_context, absent, uncertain, not_applicable.
11. adjacent_indicator_analysis must be a list of objects: {{"indicator": "P6-I1", "reason": "..."}}.
12. For no_match, return decision="no_match" and matches=[]; free-text reason codes are not part of the schema.

General examples:
- Transfer of property, shares, money, goods, oil, land, licences or radio station location is not a data cross-border measure.
- Appointing an ordinary officer is not a data protection officer requirement.
- Records do not automatically mean personal, user, subscriber, communications or digital trade data.
- Facility does not automatically mean server, cloud, data centre or computing infrastructure.
- Official secrecy or confidentiality duties are not government data access powers.
- A definition, procedure, penalty, exception, or regulation-making power is not an accepted measure by itself.
- Conditional cross-border transfer with comparable protection, consent, approval or safeguards maps to P6-I4 rather than P6-I1.

Indicator definitions for this task:
{specs}

Use triggered_exclusions only for true exclusions that defeat the selected indicator. Put comparisons against other candidate indicators in adjacent_indicator_analysis or rationale, not triggered_exclusions.

Return no_match for clearly unrelated clauses. Return uncertain only for genuinely close cases where a loaded definition, cross-reference, or missing instrument is needed.

Economy: {task.economy}
Law title: {task.law_title}
Document ID: {task.document_id}
Focal provision: {task.focal_provision_id}
Decision group: {task.route_topic}
Candidate indicators: {indicators}

NUMBERED TEXT SEGMENTS:
{segments}
"""


def _build_p4_prompt(task: CandidateTask) -> str:
    indicators = ", ".join(task.candidate_indicators)
    specs = "\n\n".join(_spec_text_for_indicator(indicator) for indicator in task.candidate_indicators)
    boundaries = "\n".join(f"- {rule}" for rule in P4_MAPPING_BOUNDARY_RULES)
    segments = "\n".join(f"[{sid}] {text}" for sid, text in task.evidence_segments.items())
    return f"""You are mapping legal text to RDTII Pillar 4 indicators.

Use only the supplied legal text. The Router recalled a candidate but did not decide a match.
Identify an operative legal provision, test every required fact and exclusion, and return strict structured evidence.
Mapper match is not final acceptance; a separate Reviewer and deterministic resolver decide status.

Rules:
1. Return no_match for definitions, headings, directories, pure cross-references, rule-making powers, or supporting text that does not itself create the required legal effect.
2. Return uncertain only for genuine ambiguity, incomplete cross-references, or conflicting evidence. Do not default high-risk indicators to uncertain.
3. Evidence IDs must come from the numbered segments. Include S1 when the focal clause is operative.
4. For framework tasks, map only the candidate element named in matched_patterns; never infer the whole framework from one provision.
5. P4-I1 concerns application burdens; P4-I3 concerns restrictions on exercise/use of patent rights; P4-I2 concerns availability of remedies.
6. P4-I5 concerns the general copyright framework and exceptions; P4-I6 requires an explicit online nexus and an enforcement remedy.
7. P4-I9 requires protected subject + compelled disclosure action + legal compulsion. Extract safeguards but do not treat protection duties as disclosure duties.
8. P4-I10 may be statutory, common-law/equitable, or remedial. Lack of a statute title is not evidence of absence.
9. Use string indicator IDs exactly as supplied. Never convert them to numbers.

Canonical P4 boundary rules:
{boundaries}

Indicator definitions:
{specs}

Economy: {task.economy}
Law title: {task.law_title}
Document ID: {task.document_id}
Focal provision: {task.focal_provision_id}
Decision group: {task.route_topic}
Candidate indicators: {indicators}
Matched patterns: {'; '.join(task.matched_patterns)}

NUMBERED TEXT SEGMENTS:
{segments}
"""


def _spec_text_for_indicator(indicator: str) -> str:
    spec = INDICATOR_SPECS[indicator]
    return "\n".join(
        [
            f"{spec.indicator_id} - {spec.title}:",
            f"- Required element codes: {'; '.join(spec.required_elements)}",
            f"- Excluded cases: {'; '.join(spec.excluded_cases)}",
            f"- Evidence rules: {'; '.join(spec.evidence_rules)}",
            f"- Positive conditions: {'; '.join(spec.positive_expressions)}",
            f"- Objects: {'; '.join(spec.object_terms)}",
            f"- Actions: {'; '.join(spec.action_terms)}",
            f"- Adjacent indicators: {'; '.join(spec.adjacent_indicators)}",
        ]
    )


def _spec_text(decision_group: str, economy: str = "Singapore") -> str:
    chunks = []
    localised = economy.casefold() != "singapore"
    for spec in specs_for_group(decision_group):
        geographic_terms = spec.geographic_or_duration_terms
        positive_expressions = spec.positive_expressions
        functional_expressions = spec.functional_expressions
        if localised:
            geographic_terms = _localise_terms(geographic_terms, economy)
            positive_expressions = _localise_terms(positive_expressions, economy)
            functional_expressions = _localise_terms(functional_expressions, economy)
        chunks.append(
            "\n".join(
                [
                    f"{spec.indicator_id}:",
                    f"- Required element codes: {'; '.join(spec.required_elements)}",
                    f"- Excluded cases: {'; '.join(spec.excluded_cases)}",
                    f"- Evidence rules: {'; '.join(spec.evidence_rules)}",
                    f"- Context rules: {'; '.join(spec.context_rules)}",
                    f"- Positive expressions: {'; '.join(positive_expressions[:8])}",
                    f"- Objects: {'; '.join(spec.object_terms[:15])}",
                    f"- Actions: {'; '.join(spec.action_terms[:12])}",
                    f"- Geographic/duration elements: {'; '.join(geographic_terms[:12])}",
                    f"- Adjacent indicators to distinguish: {'; '.join(spec.adjacent_indicators)}",
                ]
            )
        )
    return "\n\n".join(chunks)


def _localise_terms(terms: tuple[str, ...], economy: str) -> tuple[str, ...]:
    profile = economy_profile(economy)
    local = profile.name.casefold()
    return tuple(
        term.replace("singapore", local).replace("Singapore", economy)
        for term in terms
    )


def extract_decision(task: CandidateTask, model_name: str, *, max_retries: int = 1) -> tuple[MappingDecision, int]:
    client = openai_client()
    prompt = build_prompt(task)
    attempt = 0
    while True:
        try:
            output_schema = MappingDecision if task.route_topic.startswith("P4_") else P67MappingDecision
            response = client.responses.parse(
                model=model_name,
                input=prompt,
                text_format=output_schema,
            )
            return MappingDecision.model_validate(response.output_parsed.model_dump()), attempt
        except Exception:
            if attempt >= max_retries:
                raise
            time.sleep(min(30, 2 ** attempt * 3))
            attempt += 1


def build_reviewer_prompt(task: CandidateTask, match: IndicatorMatch, *, retry_instructions: str = "") -> str:
    allowed_elements, allowed_exclusions = _review_spec_for_task(task, match)
    catalog = evidence_catalog_for_task(task)
    allowed_evidence_ids = [item.evidence_id for item in catalog]
    framework_candidate = _framework_task_candidate_element(task) if task.task_kind == "framework_element" else ""
    segments = "\n".join(
        f"[{item.evidence_id}] role={item.role}; provision_id={item.provision_id}; text={item.text}"
        for item in catalog
    )
    guidance = _reviewer_guidance_for_group(task.route_topic, task.economy)
    retry_block = f"\nTargeted schema correction required:\n{retry_instructions}\n" if retry_instructions else ""
    return f"""You are an adversarial legal evidence reviewer for RDTII mapping.

Use only the provided text segments and IndicatorSpec. Do not use outside legal knowledge.
The router only recalled a possible candidate. It did not decide a match. Your default posture is independent verification, and no_match is expected when the focal clause lacks any required element.
Do not decide final accepted/rejected/review status. The program will do that deterministically.
Do not redo full mapping. Verify the proposed facts, element claims, exclusions, and evidence use; reject the premise when the candidate actually belongs to a different indicator or only appears in supporting context.

Reviewer tasks:
1. Verify whether each allowed element is supported by valid evidence IDs.
2. Verify whether each allowed exclusion is triggered by valid evidence IDs.
3. Verify focal clause integrity and that supporting context is not replacing a missing focal operative rule.
4. Return exactly the task-specific structured review schema selected by the caller.
   Do not include irrelevant fields from other indicators. Use only:
   - focal_role, decision, rationale, evidence_spans;
   - required_elements[] with allowed element codes;
   - exclusions[] with allowed exclusion codes;
   - the task-specific factual fields for this indicator group.
	5. decision is match only if all required elements are supported by explicit evidence spans and no exclusion is triggered; supporting_only if the focal clause is only supporting context; no_match if any required element is not supported or an exclusion is triggered; uncertain only for genuine legal ambiguity.
6. Return only the allowed element codes and allowed exclusion codes listed below.
7. Do not invent element codes, exclusion codes, evidence IDs, final failure codes, or final statuses.
8. supported requires explicit evidence_ids. Missing evidence is not_supported, not uncertain.
9. Use uncertain only when evidence exists but has two reasonable legal interpretations.
10. focal_role=operative means the focal clause contains the operative rule; supporting_only means it only defines, modifies, limits, explains, provides contact/notification, or preserves responsibility for another provision's obligation; uncertain means the focal role cannot be determined from the evidence.
11. Cite only allowed_evidence_ids. Do not cite S1, H1, R_PARENT, R_SCOPE, or any ID not listed below.
12. elements must contain every allowed element code exactly once. Do not omit, duplicate, or add element codes.
13. exclusions must contain every allowed exclusion code exactly once, including not_triggered exclusions. Do not omit, duplicate, or add exclusion codes.
14. Put non-required analysis in optional_checks, not elements.

Allowed element codes:
{'; '.join(allowed_elements)}

Allowed exclusion codes:
{'; '.join(allowed_exclusions)}

Allowed evidence IDs:
{'; '.join(allowed_evidence_ids)}

Evidence role rules:
- role=focal is the focal clause and may support OPERATIVE_RULE.
- role=parent may supply parent-section context, but cannot replace a missing focal operative rule.
- role=heading may support statute title, "this Act", regulated scope, and industry scope.
- role=supporting may support definitions, object/scope, conditions, exceptions, and cross-references.
- A separate obligation in another subsection cannot replace a missing focal operative rule.

Decision-group-specific review rules:
{guidance}
{retry_block}

Router-proposed structured facts to verify independently:
- indicator: {match.indicator}
- decision_group: {task.route_topic}
- candidate_element: {framework_candidate}
- legal_function: {match.legal_function}
- actor: {match.actor}
- modality: {match.modality}
- action: {match.action}
- regulated_object: {match.regulated_object}
- geographic_nexus: {match.geographic_nexus}
- duration: {match.duration}
- conditions: {'; '.join(match.conditions)}
- evidence_ids: {'; '.join(match.evidence_ids)}
- review_trigger_codes: {'; '.join(match.triggered_exclusions)}
- unresolved_required_elements: {'; '.join(item.element_code for item in match.required_element_status if item.status == 'uncertain')}

Evidence catalog:
{segments}
"""


def evidence_catalog_for_task(task: CandidateTask) -> list[EvidenceCatalogEntry]:
    catalog: list[EvidenceCatalogEntry] = []
    for index, (source_id, text) in enumerate(task.evidence_segments.items(), start=1):
        if source_id == "S1" or not catalog:
            role = "focal"
            provision_id = task.focal_provision_id
        elif source_id == "R_PARENT" or str(text).startswith("Complete parent section:"):
            role = "parent"
            provision_id = _parent_provision_id(task.focal_provision_id)
        elif source_id in {"H1", "R_SCOPE"} or str(text).startswith("Title and headings:") or str(text).startswith("Headings:"):
            role = "heading"
            provision_id = task.focal_provision_id
        else:
            role = "supporting"
            provision_id = _segment_provision_id(text, task.focal_provision_id)
        catalog.append(EvidenceCatalogEntry(evidence_id=f"E{index}", role=role, provision_id=provision_id, text=text))
    return catalog


def _segment_provision_id(text: str, fallback: str) -> str:
    head = str(text).split(":", 1)[0].strip()
    return head if head and len(head) <= 80 else fallback


def _parent_provision_id(value: str) -> str:
    return str(value).split("(", 1)[0] or value


def _reviewer_guidance_for_group(route_topic: str, economy: str = "Singapore") -> str:
    local_name = economy if economy else "Singapore"
    if route_topic == "P4_PATENT_APPLICATION":
        return """P4-I1 fact matrix:
- Confirm an operative patent-application requirement and its legal consequence.
- Foreign/local differentiation, a local representative requirement, a domestic address for service that creates a localisation burden, first domestic filing, substantive examination, or a material fee/cost burden may qualify under the RDTII definition.
- Distinguish a required local address for service from an ordinary administrative contact address that applies without an additional local-presence burden.
- Ordinary forms, file formats, routine deadlines, renewals, contact details, and ordinary agent procedure do not qualify by themselves.
- Do not move patent remedies, compulsory licensing, government use, or post-grant exercise restrictions into P4-I1."""
    if route_topic == "P4_PATENT_ENFORCEMENT":
        return """P4 patent enforcement fact matrix:
- For P4-I3, a keyword such as compulsory licence, government use, working, public interest, or emergency is never sufficient. Confirm the restriction, affected patent right, trigger, scope, legal effect, compensation, safeguards, and judicial review where stated.
- For P4-I2 framework tasks, review only the named element. Ordinary remedies and provisional measures are separate elements.
- P4-I2 positive evidence must establish or grant a remedy. A prohibition on injunctions, damages limitation, defence, immunity, or statement that a remedy is unavailable is not a positive remedy element; consider P4-I3 only through its full restriction matrix.
- A definition of infringement, general jurisdiction, criminal penalty alone, heading, or cross-reference is not operative framework evidence."""
    if route_topic == "P4_COPYRIGHT_FRAMEWORK":
        return """P4-I5 framework rules:
- Review only the named element: operative copyright protection or an explicit, usable copyright exception.
- Classify fair use, fair dealing, permitted use, accessibility, education, library/archive, quotation, parody, temporary copying, and similar non-infringement provisions as copyright_exceptions, not copyright_framework.
- Scope/application clauses, definitions, record inspection, tribunal procedure, groundless-threat procedure, rule-making powers, incomplete cross-references, a statute title, registration procedure, or abstract test are supporting-only and cannot independently satisfy an element.
- One isolated exception cannot establish the whole framework.
- Element evidence may be accepted normally; framework completeness is determined later by aggregation."""
    if route_topic == "P4_ONLINE_COPYRIGHT":
        return """P4-I6 framework rules:
- Require an explicit online nexus in the focal evidence: online, internet, website, online location, digital copy, digital/electronic transmission, streaming, uploading, communication to the public, making available, network service, network connection provider, or internet service provider.
- General copyright remedies are not enough merely because they could theoretically apply online.
- Website blocking must be linked to copyright infringement. Content-control or unrelated blocking powers do not qualify.
- Distinguish a final blocking/access-disabling remedy from an interim, temporary, interlocutory, or urgent online measure.
- Intermediary safe-harbour rules without an enforcement remedy do not qualify.
- Review ordinary online remedies and online provisional measures as separate elements."""
    if route_topic == "P4_DISCLOSURE":
        return """P4-I9 fact matrix:
- Match requires all three facts: protected commercial subject, compelled disclosure action, and legal compulsion or consequence.
- Identify the information holder and compelled actor. The required direction is a business, right holder, applicant, licensee, provider, or regulated entity being compelled to provide protected information to a court or public authority.
- A public officer being permitted or required to disclose information, internal government sharing, an official secrecy duty, or confidentiality obligations after receipt run in the wrong direction.
- General investigation powers, ordinary records/reporting, voluntary disclosure, private contractual disclosure, and discovery not shown to involve protected material do not qualify.
- Extract confidentiality, access/use limits, court supervision, public-interest basis, return/destruction, security controls, and protection against unfair commercial use.
- A government-procurement condition belongs to P2-I2 rather than P4-I9."""
    if route_topic == "P4_TRADE_SECRET_FRAMEWORK":
        return """P4-I10 framework rules:
- Review only the named element: statutory protection, express common-law/case-law protection, or remedies.
- Do not require a statute named Trade Secrets Act.
- Statutory protection must protect private/commercial secret information against unauthorised acquisition, use, or disclosure.
- Official secrecy, state secrets, one departmental or employee confidentiality duty, government-internal confidentiality, court-file confidentiality, a definition, or one narrow sector rule is supporting/sectoral evidence and not a complete framework.
- Remedies must be enforceable and linked to protected trade secrets, confidential commercial information, or breach of confidence.
- No evidence is uncertainty, not proof of absence. Aggregation decides completeness after element review."""
    if route_topic == "P6_LOCATION":
        return f"""P6 fact matrix:
- LOCAL_STORAGE_ACTION is only keep/store/maintain/retain/preserve/keep a copy/send and keep.
- For P6-I2, the focal clause itself must contain the local storage obligation, or must contain an explicit cross-reference chain to the provision that supplies the storage obligation. Adjacent text may explain object/scope/conditions, but may not replace a missing focal storage duty.
- Reporting, filing, registration location, inspection availability, or general internal-control duties do not support LOCAL_STORAGE_ACTION or EXPLICIT_DOMESTIC_STORAGE_LOCATION unless the focal clause expressly requires data/records/copies to be kept/stored/maintained/retained in {local_name}.
- MANDATORY_DOMESTIC_PROCESSING requires process/analyse/compute/handle/use or equivalent data processing in {local_name}. Storage/copy retention in {local_name} does not support it.
- CONDITIONAL_TRANSFER_PATH must regulate cross-border transfer itself through consent, adequacy, comparable protection, safeguards, BCRs, certification, transfer assessment, prescribed data-protection requirements, or approval for transfer.
- Overseas books/records plus a {local_name} copy is local storage, not a conditional transfer regime.
- ORDINARY_COMPLIANCE_PATH exists only when ordinary market actors may transfer data after satisfying general legal protection conditions.
- ABSOLUTE_TRANSFER_PROHIBITION requires an express data export prohibition. Local storage does not imply it.
- Government, regulatory, law-enforcement, judicial, and intelligence cooperation is excluded from commercial cross-border data-flow measures.
- Device/media disposal, data erasure, deletion, or destruction before transfer triggers DEVICE_DISPOSAL_OR_DATA_ERASURE."""
    if route_topic == "P7_RETENTION":
        return """P7-I3 retention taxonomy:
- IN_SCOPE_RECORD_TYPE is supported for personal/customer/user/subscriber data; account/payment/loan/transaction records; AML/KYC records; accounting/tax/invoice/financial records; communications/traffic/location records; platform/e-commerce/digital-service records; electronic authentication records; cybersecurity/system-event records.
- You must set record_scope_basis to exactly one of: PERSONAL_CUSTOMER_USER, ACCOUNT_PAYMENT_TRANSACTION, AML_KYC, ACCOUNTING_TAX_FINANCIAL, COMMUNICATIONS_PLATFORM_DIGITAL_SERVICE, AUTHENTICATION_CYBERSECURITY_SYSTEM_EVENT, PERSON_OR_TRANSACTION_TRACEABILITY, OPERATIONAL_SECTOR_RECORD, PHYSICAL_OPERATIONAL_ONLY, UNCERTAIN, NONE.
- IN_SCOPE_RECORD_TYPE may be supported when the record is an information-bearing legal record and record_scope_basis is one of: PERSONAL_CUSTOMER_USER, ACCOUNT_PAYMENT_TRANSACTION, AML_KYC, ACCOUNTING_TAX_FINANCIAL, COMMUNICATIONS_PLATFORM_DIGITAL_SERVICE, AUTHENTICATION_CYBERSECURITY_SYSTEM_EVENT, PERSON_OR_TRANSACTION_TRACEABILITY, OPERATIONAL_SECTOR_RECORD.
- PERSON_OR_TRANSACTION_TRACEABILITY requires evidence of names, addresses, identity information, customer/supplier/recipient/user information, purchase/sale/supply/import/export or other transaction information, or traceability tied to a person or concrete business transaction.
- OPERATIONAL_SECTOR_RECORD is for industry operational information-bearing records such as aviation, energy, environmental, safety, product, training, or equipment records. Do not relabel those records as transaction, personal, or cybersecurity records unless the text actually says so.
- Building inspection, fire safety, equipment/mechanical/facility maintenance, workplace physical safety, weapons/explosives operations, pure manufacturing process, physical inventory, product testing, energy/environment/safety operations, or facility logs are PHYSICAL_OPERATIONAL_ONLY or NONE unless evidence shows an allowed basis.
- Do not support IN_SCOPE_RECORD_TYPE merely because the text says record, register, report, information, regulatory record, business record, or traceability record.
- Edge sector records such as medical/clinical, product traceability, manufacturing/import, energy/environmental, transport, or other operational records are supported only when evidence shows an allowed basis. If definitions/cross-references may show such basis but are missing, use record_scope_basis=UNCERTAIN and element status uncertain.
- Minimum duration includes at least, not less than, for a period of X years/months/days, or until X after an event. Maximum limits such as not exceeding/no longer than/up to trigger exclusions.
- For every match, attributes.minimum_duration_value, attributes.minimum_duration_unit, and attributes.trigger_event must be filled from text evidence. If any is not available, mark the corresponding element uncertain or not_supported rather than match."""
    if route_topic == "P7_ACCOUNTABILITY":
        return """P7-I4 DPO/DPIA rules:
- DPO path requires an operative focal clause requiring designate/appoint/nominate/ensure a responsible person, and the function must concern data protection, privacy, personal data processing, or compliance with a data-protection law.
- A focal clause in a personal-data-protection law that requires an organisation to designate one or more individuals responsible for ensuring compliance with that Act supports the functional DPO path, even if it does not use the title Data Protection Officer.
- Do not trigger GENERAL_COMPLIANCE_ROLE when the focal clause's compliance responsibility is explicitly for personal data protection, privacy, or a personal-data-protection statute.
- A generic 'person responsible for compliance with this Act', principal person, compliance officer, operations person, or research-compliance person is not enough unless title/scope/Part proves the law is a personal data protection law.
- Supporting context may prove the law is data-protection law or define the responsibility, but cannot replace a missing focal designation obligation.
- A focal clause that only states the designation does not relieve the organisation of responsibility, publishes contact details, states notification mechanics, or explains the designated person's responsibilities is supporting_only unless it also contains the designation obligation.
- DPIA path requires a mandatory data-protection impact assessment, privacy impact assessment, or assessment of personal-data processing risks to individuals. General assessment, risk, financial, safety, research, organisational, or cybersecurity assessment language is not_supported."""
    if route_topic == "P7_GOVERNMENT_ACCESS":
        return """P7-I5 government access rules:
- EXTERNALLY_HELD_DATA is supported when data/records are held by a company, bank, organisation, service provider, employer, individual, or other non-government holder and a public authority compels production/access. This does not prove personal data.
- PERSONAL_OR_IDENTIFIABLE_DATA is supported only for personal data/information, named or natural persons, subscriber/user/customer information tied to natural persons, employee/patient records, natural-person account/tax/identification records, or communications/traffic/location data tied to a person.
- General books, documents, information, records, or material without definition/scope proving natural-person data is not_supported.
- If a loaded definition exists but natural person vs legal person scope remains genuinely ambiguous, use uncertain.
- Official secrecy or confidentiality alone triggers OFFICIAL_SECRECY_ONLY. Compulsory production notwithstanding secrecy means OFFICIAL_SECRECY_ONLY is not_triggered."""
    if route_topic in {"P7_DATA_PROTECTION_FRAMEWORK", "P7_CYBERSECURITY_FRAMEWORK"}:
        return """Framework element review rules:
- Review exactly one candidate framework element for exactly one focal provision.
- Do not decide whether the whole framework is complete. Framework completeness is decided only by aggregation.
- candidate_element and framework_element must identify the same single element under review.
- decision=match only when the focal clause E1 independently supports that candidate element.
- decision=supporting_only when supporting text explains the element but E1 does not independently establish it.
- decision=no_match when the focal clause does not establish the candidate element.
- legal_function must describe the focal provision's own legal role, not a surrounding provision's role.
- definition/application/scope provisions can support only scope elements when the focal text itself is complete and directly relevant.
- procedure, appointment, delegation, notice mechanics, administrative information, and pure cross-reference provisions are not independent framework evidence."""
    return "Verify only the allowed framework elements and exclusions against the evidence bundle."

def extract_reviewer_decision(
    task: CandidateTask,
    match: IndicatorMatch,
    model_name: str,
    *,
    max_retries: int = 1,
    retry_instructions: str = "",
) -> tuple[ReviewerDecision, int]:
    client = openai_client()
    prompt = build_reviewer_prompt(task, match, retry_instructions=retry_instructions)
    attempt = 0
    while True:
        try:
            response = client.responses.parse(
                model=model_name,
                input=prompt,
                text_format=_review_schema_for_task(task, match),
            )
            return _normalize_task_review(task, match, response.output_parsed), attempt
        except Exception:
            if attempt >= max_retries:
                raise
            time.sleep(min(20, 2 ** attempt * 3))
            attempt += 1

def require_llm_environment() -> str:
    model = mapper_model_name()
    if not model or not api_key_available():
        raise RuntimeError("RDTII live mapping requires OPENAI_API_KEY and a configured mapper model")
    return model


def review_model_name(default_model: str) -> str:
    return reviewer_model_name(default_model)


def _review_schema_for_task(task: CandidateTask, match: IndicatorMatch):
    indicator = match.indicator or (task.candidate_indicators[0] if task.candidate_indicators else None)
    if indicator == "P4-I1":
        return P4PatentApplicationReview
    if indicator == "P4-I3":
        return P4PatentEnforcementOtherReview
    if indicator == "P4-I9":
        return P4MandatoryDisclosureReview
    if indicator in {"P4-I2", "P4-I5", "P4-I6", "P4-I10"}:
        return P4FrameworkElementReview
    if indicator in {"P6-I1", "P6-I4"}:
        return P6TransferReview
    if indicator == "P6-I2":
        return P6StorageReview
    if indicator == "P6-I3":
        return P6InfrastructureReview
    if indicator == "P7-I3":
        return P7RetentionReview
    if indicator == "P7-I4":
        return P7AccountabilityReview
    if indicator == "P7-I5":
        return P7GovernmentAccessReview
    if indicator in {"P7-I1", "P7-I2"}:
        return FrameworkElementReview
    if indicator == "P6-I5":
        return TreatyProvisionReview
    return ReviewerDecision


def _normalize_task_review(task: CandidateTask, match: IndicatorMatch, parsed) -> ReviewerDecision:
    if isinstance(parsed, ReviewerDecision):
        return parsed
    attrs = ReviewerAttributes()
    elements = list(parsed.required_elements)
    exclusions = list(parsed.exclusions)
    optional_checks: list[ReviewerOptionalCheck] = []
    if isinstance(parsed, P7RetentionReview):
        period = parsed.retention_periods[0] if parsed.retention_periods else None
        attrs = attrs.model_copy(update={
            "minimum_duration_value": period.value if period else None,
            "minimum_duration_unit": period.unit if period else None,
            "trigger_event": (period.trigger_event if period and period.trigger_event else parsed.trigger_event),
            "record_scope_basis": "OPERATIONAL_SECTOR_RECORD",
        })
    elif isinstance(parsed, P7AccountabilityReview):
        path = {"both": "dpo_and_dpia", "none": "uncertain"}.get(parsed.accountability_path, parsed.accountability_path)
        attrs = attrs.model_copy(update={"accountability_path": path})
    elif isinstance(parsed, P7GovernmentAccessReview):
        attrs = attrs.model_copy(update={"judicial_authorization": parsed.judicial_authorization})
    elif isinstance(parsed, FrameworkElementReview):
        attrs = attrs.model_copy(update={
            "coverage": parsed.coverage,
            "framework_candidate_element": parsed.candidate_element,
            "framework_element": parsed.framework_element,
            "framework_legal_function": parsed.legal_function,
        })
    elif isinstance(parsed, P4FrameworkElementReview):
        attrs = attrs.model_copy(update={
            "coverage": parsed.coverage,
            "framework_legal_function": parsed.legal_function,
        })
        optional_checks = [
            ReviewerOptionalCheck(
                check_code="P4_FRAMEWORK_ELEMENT",
                status="captured",
                evidence_ids=["E1"],
                reason=json.dumps(
                    {
                        "candidate_element": parsed.candidate_element,
                        "framework_element": parsed.framework_element,
                        "evidence_character": parsed.evidence_character,
                        "remedy_direction": parsed.remedy_direction,
                        "coverage": parsed.coverage,
                        "protected_private_or_commercial_information": parsed.protected_private_or_commercial_information,
                        "unauthorised_acquisition_use_or_disclosure": parsed.unauthorised_acquisition_use_or_disclosure,
                        "government_or_official_only": parsed.government_or_official_only,
                    },
                    sort_keys=True,
                ),
            ),
            ReviewerOptionalCheck(
                check_code="P4_ONLINE_NEXUS",
                status="supported" if getattr(parsed, "online_nexus", "") else "not_supported",
                evidence_ids=["E1"] if getattr(parsed, "online_nexus", "") else [],
                reason=getattr(parsed, "online_nexus", ""),
            )
        ]
        if parsed.indicator_id != "P4-I6":
            optional_checks.pop()
    elif isinstance(
        parsed,
        (P4PatentApplicationReview, P4PatentEnforcementOtherReview, P4MandatoryDisclosureReview),
    ):
        facts = parsed.model_dump()
        for key in (
            "focal_role",
            "required_elements",
            "exclusions",
            "decision",
            "rationale",
            "evidence_spans",
        ):
            facts.pop(key, None)
        optional_checks.append(
            ReviewerOptionalCheck(
                check_code="P4_STRUCTURED_FACTS",
                status="captured",
                evidence_ids=["E1"] if facts else [],
                reason=json.dumps(facts, ensure_ascii=False, sort_keys=True),
            )
        )
    elif isinstance(parsed, TreatyProvisionReview):
        attrs = attrs.model_copy(update={"coverage": "horizontal", "sector": "International agreement"})
    elif isinstance(parsed, P6TransferReview):
        if parsed.regulated_object_type not in {"data_or_information", "record_or_document"}:
            elements = _force_element(
                elements,
                "INFORMATION_BEARING_OBJECT",
                "not_supported",
                "regulated_object_type is not data_or_information or record_or_document",
            )
            exclusions = _force_exclusion(
                exclusions,
                "NON_DATA_ASSET_TRANSFER",
                "triggered",
                f"regulated_object_type={parsed.regulated_object_type}; object={parsed.regulated_object_text}",
            )
    elif isinstance(parsed, P6StorageReview):
        if parsed.location_relation not in {"storage_object", "storage_facility"}:
            elements = _force_element(
                elements,
                "EXPLICIT_DOMESTIC_STORAGE_LOCATION",
                "not_supported",
                f"location_relation={parsed.location_relation}; location does not modify storage object/facility",
            )
    focal_status = "ok"
    focal_reason = ""
    if parsed.focal_role == "supporting_only":
        focal_reason = "task-specific reviewer marked focal clause supporting_only"
    return ReviewerDecision(
        focal_integrity=FocalIntegrityAssessment(status=focal_status, reason=focal_reason),
        focal_role=parsed.focal_role,
        elements=elements,
        exclusions=exclusions,
        attributes=attrs,
        optional_checks=optional_checks,
        decision=parsed.decision,
        review_reason=parsed.rationale,
    )


def _force_element(elements: list[ReviewerElementAssessment], code: str, status: str, reason: str) -> list[ReviewerElementAssessment]:
    out: list[ReviewerElementAssessment] = []
    seen = False
    for item in elements:
        if item.element_code == code:
            out.append(ReviewerElementAssessment(element_id=code, status=status, evidence_ids=[], reason=reason))
            seen = True
        else:
            out.append(item)
    if not seen:
        out.append(ReviewerElementAssessment(element_id=code, status=status, evidence_ids=[], reason=reason))
    return out


def _force_exclusion(exclusions: list[ReviewerExclusionAssessment], code: str, status: str, reason: str) -> list[ReviewerExclusionAssessment]:
    out: list[ReviewerExclusionAssessment] = []
    seen = False
    evidence_ids = ["E1"] if status == "triggered" else []
    for item in exclusions:
        if item.exclusion_code == code:
            out.append(ReviewerExclusionAssessment(exclusion_id=code, status=status, evidence_ids=evidence_ids, reason=reason))
            seen = True
        else:
            out.append(item)
    if not seen:
        out.append(ReviewerExclusionAssessment(exclusion_id=code, status=status, evidence_ids=evidence_ids, reason=reason))
    return out


def _review_spec_for_task(task: CandidateTask, match: IndicatorMatch) -> tuple[tuple[str, ...], tuple[str, ...]]:
    indicator = match.indicator or task.indicator_id or task.candidate_indicators[0]
    if task.route_topic.startswith("P4_") and task.task_kind == "framework_element":
        candidate = _framework_task_candidate_element(task)
        code = framework_element_code(indicator, candidate)
        base_elements = (code,) if code else tuple()
        base_exclusions = FRAMEWORK_REVIEW_EXCLUSIONS[indicator]
    elif task.route_topic in P4_REVIEW_ELEMENTS_BY_GROUP:
        base_elements = P4_REVIEW_ELEMENTS_BY_GROUP[task.route_topic]
        base_exclusions = P4_REVIEW_EXCLUSIONS_BY_GROUP[task.route_topic]
    elif task.route_topic == "P6_LOCATION":
        base_elements = P6_REVIEW_ELEMENTS
        base_exclusions = P6_REVIEW_EXCLUSIONS
    elif task.route_topic == "P6_TREATY":
        base_elements = ("BINDING_AGREEMENT_IN_FORCE", "DATA_FLOW_COMMITMENT", "OFFICIAL_SOURCE")
        base_exclusions = ("NON_BINDING_OR_NOT_IN_FORCE", "NO_DATA_FLOW_COMMITMENT", "SOURCE_UNVERIFIABLE")
    elif task.route_topic == "P7_DATA_PROTECTION_FRAMEWORK":
        candidate = _framework_task_candidate_element(task)
        base_elements = ((framework_element_code("P7-I1", candidate),) if framework_element_code("P7-I1", candidate) else tuple())
        base_exclusions = FRAMEWORK_REVIEW_EXCLUSIONS["P7-I1"]
    elif task.route_topic == "P7_CYBERSECURITY_FRAMEWORK":
        candidate = _framework_task_candidate_element(task)
        base_elements = ((framework_element_code("P7-I2", candidate),) if framework_element_code("P7-I2", candidate) else tuple())
        base_exclusions = FRAMEWORK_REVIEW_EXCLUSIONS["P7-I2"]
    else:
        fallback_indicator = match.indicator or task.candidate_indicators[0]
        base_elements = P7_REVIEW_ELEMENTS_BY_GROUP.get(task.route_topic, tuple(INDICATOR_SPECS[fallback_indicator].required_elements))
        base_exclusions = P7_REVIEW_EXCLUSIONS_BY_GROUP.get(task.route_topic, INDICATOR_SPECS[fallback_indicator].exclusions)
    return base_elements, base_exclusions


def _claimed_review_elements(match: IndicatorMatch, allowed_elements: tuple[str, ...]) -> tuple[str, ...]:
    allowed = set(allowed_elements)
    out: list[str] = []
    for item in match.required_element_status:
        code = item.element_code
        if code in allowed and code not in out:
            out.append(code)
    return tuple(out)


def _framework_task_candidate_element(task: CandidateTask) -> str:
    indicator = task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else "")
    for pattern in task.matched_patterns:
        if pattern.startswith("framework_element:"):
            return canonical_framework_element(indicator, pattern.split(":", 1)[1].strip())
    parts = task.task_id.split(":")
    value = parts[-2].strip() if len(parts) >= 2 else ""
    return canonical_framework_element(indicator, value)
