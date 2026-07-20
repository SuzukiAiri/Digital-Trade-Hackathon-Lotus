"""Candidate discovery for RDTII P6/P7 mapping.

This module deliberately separates recall from classification. It scans the
full provision corpus and creates high-recall CandidateTasks by unioning
source-family, lexical/phrase, semantic-threshold and limited context-expansion
channels. It never accepts a mapping.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from .indicator_specs import DECISION_GROUPS, INDICATOR_SPECS, source_family_hit, specs_for_group
from .indicator_registry import indicator_definition
from .models import CandidateTask, ProvisionContext
from .economy_profiles import domestic_terms, foreign_terms


ROUTING_VERSION = "rdtii-routing-v5-high-recall"
P4_ROUTING_VERSION = "rdtii-p4-routing-v2-concept-pairs"


@dataclass(frozen=True)
class DiscoveryHit:
    decision_group: str
    matched_patterns: tuple[str, ...]
    channel: str
    confidence: str = "medium"
    promote: bool = True


@dataclass
class RoutingPackage:
    provision_tasks: list[CandidateTask]
    audit_rows: list[dict]
    stats: dict[str, int | dict[str, int]]


def build_routing_package(
    contexts: Iterable[ProvisionContext],
    economy: str = "Singapore",
    pillars: set[int] | None = None,
) -> RoutingPackage:
    if pillars == {4}:
        return _build_p4_routing_package(contexts, economy)
    tasks: dict[str, CandidateTask] = {}
    audit_rows: list[dict] = []
    primary_count = 0
    audit_hits = 0
    audit_promoted = 0
    audit_only_by_group: Counter[str] = Counter()
    provisions_scanned = 0
    framework_contexts: list[ProvisionContext] = []

    for context in contexts:
        provisions_scanned += 1
        if _framework_title_candidate(context.law_title):
            framework_contexts.append(context)
        clauses = split_focal_clauses(context)
        full_text = _fold(_context_text(context, context.text))
        for clause_id, focal_text in clauses:
            text = _fold(_context_text(context, focal_text))
            primary_hits = _primary_discovery(context, text, full_text, economy)
            audit_hits_list = _recall_audit(context, text, full_text, economy)
            audit_hits += len(audit_hits_list)

            by_group: dict[str, dict[str, DiscoveryHit]] = {}
            for hit in primary_hits:
                by_group.setdefault(hit.decision_group, {})["primary"] = hit
            for hit in audit_hits_list:
                by_group.setdefault(hit.decision_group, {})["audit"] = hit

            for group, hits in by_group.items():
                primary = hits.get("primary")
                audit = hits.get("audit")
                if not primary and audit and not audit.promote:
                    audit_rows.append(_audit_row(context, clause_id, audit, "not_promoted"))
                    continue
                if primary:
                    primary_count += 1
                    recall_source = "primary_and_audit" if audit else "primary_router"
                else:
                    audit_promoted += 1
                    audit_only_by_group[group] += 1
                    recall_source = "audit_promoted"
                    audit_rows.append(_audit_row(context, clause_id, audit, "promoted"))  # type: ignore[arg-type]

                matched = []
                confidence = None
                for hit in (primary, audit):
                    if hit:
                        matched.extend(hit.matched_patterns)
                        confidence = hit.confidence if hit.channel == "audit" else confidence

                task = _candidate_task(
                    context=context,
                    clause_id=clause_id,
                    focal_text=focal_text,
                    clauses=clauses,
                    decision_group=group,
                    economy=economy,
                    recall_source=recall_source,
                    matched_patterns=sorted(set(matched)),
                    audit_confidence=confidence,
                )
                tasks[_dedupe_key(task)] = task

    framework_element_tasks = route_framework_element_tasks(framework_contexts, economy)
    for task in framework_element_tasks:
        tasks[_dedupe_key(task)] = task
    stats: dict[str, int | dict[str, int]] = {
        "provisions_scanned": provisions_scanned,
        "primary_router_tasks": primary_count,
        "audit_hits": audit_hits,
        "audit_only_tasks_promoted": audit_promoted,
        "audit_only_by_topic": dict(audit_only_by_group),
        "framework_instruments_identified": len({task.document_id for task in framework_element_tasks}),
        "framework_element_tasks": len(framework_element_tasks),
        "candidate_tasks_after_deduplication": len(tasks),
    }
    return RoutingPackage(
        provision_tasks=sorted(tasks.values(), key=lambda item: (item.document_id, item.focal_provision_id, item.route_topic, item.task_id)),
        audit_rows=audit_rows,
        stats=stats,
    )


def route_tasks(
    contexts: list[ProvisionContext],
    economy: str = "Singapore",
    pillars: set[int] | None = None,
) -> list[CandidateTask]:
    return build_routing_package(contexts, economy, pillars).provision_tasks


def _build_p4_routing_package(contexts: Iterable[ProvisionContext], economy: str) -> RoutingPackage:
    tasks: dict[str, CandidateTask] = {}
    scanned = 0
    ordinary_count = 0
    framework_count = 0
    framework_documents: set[str] = set()
    for context in contexts:
        scanned += 1
        clauses = split_focal_clauses(context)
        for clause_id, focal_text in clauses:
            text = _fold(_context_text(context, focal_text))
            for group, patterns in _p4_ordinary_hits(context, text):
                task = _candidate_task(
                    context=context,
                    clause_id=clause_id,
                    focal_text=focal_text,
                    clauses=clauses,
                    decision_group=group,
                    economy=economy,
                    recall_source="primary_router",
                    matched_patterns=patterns,
                    audit_confidence=None,
                )
                tasks[_dedupe_key(task)] = task
                ordinary_count += 1
            for indicator, route_topic, element, patterns in _p4_framework_element_hits(context, text):
                definition = indicator_definition(indicator)
                base = _candidate_task(
                    context=context,
                    clause_id=clause_id,
                    focal_text=focal_text,
                    clauses=clauses,
                    decision_group=route_topic,
                    economy=economy,
                    recall_source="primary_router",
                    matched_patterns=[f"framework_element:{element}", *patterns],
                    audit_confidence=None,
                )
                task = base.model_copy(
                    update={
                        "task_id": _task_id(context, clause_id, f"{route_topic}:{element}", focal_text),
                        "indicator_id": indicator,
                        "task_kind": "framework_element",
                        "source_record_id": context.provision_id,
                        "focal_quote": focal_text,
                        "contract_version": definition.contract_version,
                        "candidate_indicators": [indicator],
                    }
                )
                tasks[_dedupe_key(task)] = task
                framework_count += 1
                framework_documents.add(context.document_id)
    return RoutingPackage(
        provision_tasks=sorted(
            tasks.values(),
            key=lambda item: (item.document_id, item.focal_provision_id, item.route_topic, item.task_id),
        ),
        audit_rows=[],
        stats={
            "provisions_scanned": scanned,
            "primary_router_tasks": ordinary_count,
            "audit_hits": 0,
            "audit_only_tasks_promoted": 0,
            "audit_only_by_topic": {},
            "framework_instruments_identified": len(framework_documents),
            "framework_element_tasks": framework_count,
            "candidate_tasks_after_deduplication": len(tasks),
        },
    )


def _p4_ordinary_hits(context: ProvisionContext, text: str) -> list[tuple[str, list[str]]]:
    title = _fold(context.law_title)
    patent_nexus = "patent" in title or _has_any(text, ("patent application", "patentee", "patented invention", "patent right"))
    hits: list[tuple[str, list[str]]] = []
    application_subject = _has_any(
        text,
        (
            "patent application",
            "applicant",
            "application for a patent",
            "filing abroad",
            "file abroad",
            "application abroad",
            "first application",
            "foreign filing",
            "substantive examination",
            "filing fee",
            "application fee",
        ),
    )
    application_burden = _has_any(
        text,
        (
            "foreign applicant",
            "non-resident applicant",
            "applicant outside",
            "address for service",
            "local address",
            "resident agent",
            "local agent",
            "local representative",
            "foreign filing permission",
            "permission to file abroad",
            "first apply",
            "first file",
            "first filing",
            "substantive examination",
            "examination as to substance",
            "filing fee",
            "application fee",
            "registration cost",
        ),
    )
    application_operation = _has_any(
        text,
        (
            "must ",
            "shall ",
            "is required to",
            "may not",
            "must not",
            "shall not",
            "required before",
            "application shall",
            "application must",
            "request for examination",
            "fee payable",
        ),
    )
    if patent_nexus and application_subject and application_burden and application_operation:
        hits.append(("P4_PATENT_APPLICATION", ["p4:patent-application"]))
    if patent_nexus and (
        _p4_remedy_limitation(text)
        or _has_any(
            text,
            (
                "compulsory licence",
                "compulsory license",
                "government use",
                "crown use",
                "working requirement",
                "work the patent",
                "national emergency",
                "public interest",
            ),
        )
    ):
        hits.append(("P4_PATENT_ENFORCEMENT", ["p4:patent-right-restriction"]))
    protected = _has_any(
        text,
        ("trade secret", "source code", "algorithm", "confidential business information", "proprietary information", "technical specification", "commercially sensitive"),
    )
    disclosure = _has_any(
        text,
        (
            "must disclose",
            "shall disclose",
            "must provide",
            "shall provide",
            "must submit",
            "shall submit",
            "must produce",
            "shall produce",
            "compelled production",
            "may require disclosure",
            "may require the holder",
            "may order production",
            "grant access",
            "require access",
        ),
    ) or (
        _has_any(text, ("may require", "is required to", "by notice require", "may order", "court orders"))
        and _has_any(text, ("disclose", "provide", "submit", "produce", "surrender", "grant access"))
    )
    holder_or_authority = _has_any(
        text,
        (
            "company",
            "business",
            "undertaking",
            "applicant",
            "licensee",
            "licence holder",
            "regulated entity",
            "information holder",
            "owner",
            "provider",
            "regulator",
            "authority may require",
            "court may order",
        ),
    )
    if protected and disclosure and holder_or_authority:
        hits.append(("P4_DISCLOSURE", ["p4:protected-disclosure"]))
    return hits


def _p4_framework_element_hits(
    context: ProvisionContext,
    text: str,
) -> list[tuple[str, str, str, list[str]]]:
    title = _fold(context.law_title)
    if _framework_non_evidence_clause(title, _fold(context.section_reference)):
        return []
    hits: list[tuple[str, str, str, list[str]]] = []
    patent_nexus = "patent" in title or _has_any(text, ("patent infringement", "patentee", "patented invention"))
    positive_patent_remedy = _has_any(
        text,
        (
            "may grant an injunction",
            "may grant injunction",
            "grant an injunction",
            "award damages",
            "recover damages",
            "entitled to damages",
            "account of profits",
            "order seizure",
            "order the seizure",
            "order destruction",
            "order the destruction",
            "order delivery up",
            "administrative stop order",
        ),
    ) and not _p4_remedy_limitation(text)
    if patent_nexus and positive_patent_remedy:
        hits.append(("P4-I2", "P4_PATENT_ENFORCEMENT", "ordinary_civil_or_administrative_remedies", ["p4:patent-remedy"]))
    if patent_nexus and not _p4_remedy_limitation(text) and _has_any(
        text,
        (
            "may grant an interim injunction",
            "may grant an interlocutory injunction",
            "may grant a preliminary injunction",
            "interim injunction",
            "interlocutory injunction",
            "preliminary injunction",
            "ex parte relief",
            "order evidence preservation",
            "order property preservation",
            "before final judgment",
        ),
    ):
        hits.append(("P4-I2", "P4_PATENT_ENFORCEMENT", "provisional_measures", ["p4:patent-provisional"]))

    copyright_nexus = "copyright" in title or "copyright" in text
    copyright_exception = _has_any(
        text,
        (
            "fair dealing",
            "fair use",
            "research or study",
            "criticism or review",
            "reporting current events",
            "news reporting",
            "education",
            "educational purpose",
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
            "permitted use",
            "does not infringe",
            "is not an infringement",
        ),
    )
    if copyright_nexus and not copyright_exception and _has_any(
        text,
        (
            "copyright subsists",
            "exclusive right",
            "right to reproduce",
            "right to distribute",
            "right to communicate",
            "copyright is infringed",
            "infringes copyright",
            "communication to the public",
            "making available right",
        ),
    ):
        hits.append(("P4-I5", "P4_COPYRIGHT_FRAMEWORK", "copyright_framework", ["p4:copyright-framework"]))
    if copyright_nexus and copyright_exception:
        hits.append(("P4-I5", "P4_COPYRIGHT_FRAMEWORK", "copyright_exceptions", ["p4:copyright-exception"]))

    online = _has_any(
        text,
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
    )
    online_copyright_nexus = copyright_nexus or _has_any(
        text,
        ("flagrantly infringing online location", "online copyright infringement", "copyright material"),
    )
    if online_copyright_nexus and online and not _p4_remedy_limitation(text) and _has_any(
        text,
        (
            "grant an injunction",
            "blocking injunction",
            "disable access",
            "disabling access",
            "access disabling",
            "block access",
            "order blocking",
            "award damages",
            "remove the infringing",
            "stop order",
            "restrain online",
        ),
    ):
        hits.append(("P4-I6", "P4_ONLINE_COPYRIGHT", "online_civil_or_administrative_remedies", ["p4:online-remedy"]))
    if online_copyright_nexus and online and not _p4_remedy_limitation(text) and _has_any(
        text,
        (
            "interim blocking order",
            "interim injunction",
            "interlocutory injunction",
            "preliminary injunction",
            "temporary blocking",
            "temporary block",
            "temporarily disable",
            "interim disable",
            "urgent order",
            "digital evidence preservation",
        ),
    ):
        hits.append(("P4-I6", "P4_ONLINE_COPYRIGHT", "online_provisional_measures", ["p4:online-provisional"]))

    secret_nexus = _has_any(text, ("trade secret", "confidential business information", "proprietary information", "commercially valuable information", "breach of confidence"))
    if secret_nexus and _has_any(text, ("must not disclose", "shall not disclose", "unauthorised disclosure", "unauthorized disclosure", "misappropriation", "must keep confidential", "prohibit use")):
        hits.append(("P4-I10", "P4_TRADE_SECRET_FRAMEWORK", "statutory_trade_secret_protection", ["p4:statutory-secret-protection"]))
    if secret_nexus and _has_any(text, ("breach of confidence", "common law", "equity", "equitable")):
        hits.append(("P4-I10", "P4_TRADE_SECRET_FRAMEWORK", "common_law_or_case_law_protection", ["p4:common-law-secret-protection"]))
    if secret_nexus and _has_any(text, ("injunction", "damages", "account of profits", "delivery up", "destruction", "penalty", "offence")):
        hits.append(("P4-I10", "P4_TRADE_SECRET_FRAMEWORK", "trade_secret_remedies", ["p4:trade-secret-remedy"]))
    return hits


def route_framework_element_tasks(contexts: list[ProvisionContext], economy: str = "Singapore") -> list[CandidateTask]:
    tasks: dict[str, CandidateTask] = {}
    for context in contexts:
        clauses = split_focal_clauses(context)
        for clause_id, focal_text in clauses:
            folded = _fold(_context_text(context, focal_text))
            for indicator, route_topic, element in _framework_element_hits(context, folded):
                definition = indicator_definition(indicator)
                task = _candidate_task(
                    context=context,
                    clause_id=clause_id,
                    focal_text=focal_text,
                    clauses=clauses,
                    decision_group=route_topic,
                    economy=economy,
                    recall_source="primary_router",
                    matched_patterns=[f"framework_element:{element}"],
                    audit_confidence=None,
                ).model_copy(
                    update={
                        "task_id": _task_id(context, clause_id, f"{route_topic}:{element}", focal_text),
                        "indicator_id": indicator,
                        "task_kind": "framework_element",
                        "source_type": "domestic_legislation",
                        "source_record_id": context.provision_id,
                        "focal_quote": focal_text,
                        "contract_version": definition.contract_version,
                        "candidate_indicators": [indicator],
                    }
                )
                tasks[_dedupe_key(task)] = task
    return sorted(tasks.values(), key=lambda item: (item.document_id, item.focal_provision_id, item.route_topic, item.task_id))


def _framework_element_hits(context: ProvisionContext, text: str) -> list[tuple[str, str, str]]:
    title = _fold(context.law_title)
    heading = _fold(" ".join([context.part_heading, context.division_heading, context.section_reference]))
    if _framework_non_evidence_clause(title, heading):
        return []
    scope_only = _framework_scope_only_clause(heading)
    hits: list[tuple[str, str, str]] = []
    if _data_protection_framework_family(title):
        if _has_any(text, ("personal data means", "personal information means", "data about an individual", "information or an opinion about an identified individual", "individual who can be identified")):
            hits.append(("P7-I1", "P7_DATA_PROTECTION_FRAMEWORK", "personal_data_scope"))
        if not scope_only and _has_any(text, ("organisation must not collect", "organisation must not use", "organisation must not disclose", "app entity must", "australian privacy principle", "protect personal data", "personal information", "access request", "correction request", "data breach", "eligible data breach", "privacy impact assessment")):
            hits.append(("P7-I1", "P7_DATA_PROTECTION_FRAMEWORK", "substantive_duties_or_rights"))
        if not scope_only and _has_any(text, ("financial penalty", "civil penalty", "give such directions", "enforce", "enforceable undertaking", "investigation", "determination", "commissioner may", "require any organisation", "power to review", "power to investigate")):
            hits.append(("P7-I1", "P7_DATA_PROTECTION_FRAMEWORK", "regulator_or_enforcement"))
    if _cybersecurity_framework_family(title):
        if _has_any(text, ("cybersecurity means", "cyber security means", "critical information infrastructure", "critical infrastructure asset", "critical infrastructure sector", "computer or computer system", "cybersecurity incident", "cyber security incident")):
            hits.append(("P7-I2", "P7_CYBERSECURITY_FRAMEWORK", "cybersecurity_scope"))
        if not scope_only and _has_any(text, ("owner of a critical information infrastructure must", "responsible entity", "must report", "must notify", "cybersecurity incident", "cyber security incident", "audit", "risk assessment", "risk management program", "critical infrastructure risk management", "asset register", "cybersecurity exercise", "cyber security exercise", "comply with")):
            hits.append(("P7-I2", "P7_CYBERSECURITY_FRAMEWORK", "substantive_cybersecurity_obligation"))
        if not scope_only and _has_any(text, ("commissioner may issue", "secretary may", "minister may", "issue directions", "give a direction", "information-gathering direction", "investigate", "enforcement", "civil penalty", "require any person", "direct the owner")):
            hits.append(("P7-I2", "P7_CYBERSECURITY_FRAMEWORK", "authority_or_enforcement"))
    return hits


def _data_protection_framework_family(title: str) -> bool:
    return _has_any(title, ("personal data protection", "data protection", "privacy", "personal information"))


def _cybersecurity_framework_family(title: str) -> bool:
    if _has_any(title, ("cybersecurity", "cyber security")):
        return True
    return "critical infrastructure" in title and "security" in title


def _framework_scope_only_clause(heading: str) -> bool:
    return _has_any(
        heading,
        (
            "preliminary",
            "interpretation",
            "application of act",
            "application of this act",
            "definitions",
        ),
    )


def _framework_non_evidence_clause(title: str, heading: str) -> bool:
    text = f"{title} {heading}"
    return _has_any(
        text,
        (
            "appeal",
            "delegation",
            "advisory guideline",
            "general advisory guidelines",
            "power to exempt",
            "power to make regulations",
            "composition of offences",
            "citation",
            "commencement",
            "fees",
            "forms",
        ),
    )


def split_focal_clauses(context: ProvisionContext) -> list[tuple[str, str]]:
    text = context.text.strip()
    matches = [match for match in re.finditer(r"\((\d{1,3}[A-Z]?)\)\s+", text) if _is_structural_marker(text, match.start())]
    if len(matches) < 2:
        return [(context.provision_id, text)]
    clauses: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.start() : end].strip()
        if len(body) >= 20:
            clauses.append((f"{context.provision_id}({match.group(1)})", body))
    return clauses or [(context.provision_id, text)]


def _is_structural_marker(text: str, start: int) -> bool:
    prefix = text[max(0, start - 60) : start].casefold()
    if _is_inline_legal_reference(prefix):
        return False
    if start == 0:
        return True
    previous = text[start - 1]
    if previous.isalnum() or previous == ")":
        return False
    return previous.isspace() or previous in ".;:—–-�"


INLINE_REFERENCE_TERMS = (
    "paragraph",
    "paragraphs",
    "subsection",
    "subsections",
    "section",
    "sections",
    "regulation",
    "regulations",
    "subparagraph",
    "subparagraphs",
    "sub-paragraph",
    "sub-paragraphs",
    "clause",
    "clauses",
    "item",
    "items",
    "rule",
    "rules",
    "mentioned in",
    "referred to",
    "under",
    "subject to",
    "pursuant to",
)


INLINE_REFERENCE_RE = re.compile(
    r"(?:\b(?:" + "|".join(re.escape(term) for term in INLINE_REFERENCE_TERMS) + r")\b)\s*$",
    flags=re.I,
)


def _is_inline_legal_reference(prefix: str) -> bool:
    return bool(INLINE_REFERENCE_RE.search(prefix))


def _primary_discovery(context: ProvisionContext, text: str, full_text: str, economy: str) -> list[DiscoveryHit]:
    hits: list[DiscoveryHit] = []
    title_context = _fold(" ".join([context.law_title, context.part_heading, context.division_heading, context.section_reference]))
    for group in ("P6_LOCATION", "P7_RETENTION", "P7_ACCOUNTABILITY", "P7_GOVERNMENT_ACCESS"):
        specs = specs_for_group(group)
        source_family = any(source_family_hit(title_context, spec.source_families) for spec in specs)
        lexical = _lexical_group_hit(group, text, full_text, economy)
        phrase = _phrase_hit(text, specs)
        semantic = _semantic_threshold(text, specs)
        if group == "P6_LOCATION":
            phrase_candidate = phrase
            source_family_candidate = source_family and semantic >= _source_family_threshold(group)
            semantic_candidate = semantic >= _semantic_threshold_required(group)
        else:
            phrase_candidate = phrase and _minimal_group_signal(group, text, economy)
            source_family_candidate = source_family and semantic >= _source_family_threshold(group) and _minimal_group_signal(group, text, economy)
            semantic_candidate = semantic >= _semantic_threshold_required(group) and _minimal_group_signal(group, text, economy)
        if lexical or phrase_candidate or source_family_candidate or semantic_candidate:
            patterns = []
            if source_family:
                patterns.append("source_family")
            if lexical:
                patterns.append(f"lexical:{lexical}")
            if phrase_candidate:
                patterns.append("exact_phrase")
            if semantic_candidate or source_family_candidate:
                patterns.append(f"semantic:{semantic:.2f}")
            hits.append(DiscoveryHit(group, tuple(patterns), "primary", "high"))
    return hits


def _recall_audit(context: ProvisionContext, text: str, full_text: str, economy: str) -> list[DiscoveryHit]:
    hits: list[DiscoveryHit] = []
    local_terms = _local_terms(economy)
    cross_border_terms = _cross_border_terms(economy)
    if _has_any(text, DATA_OBJECTS) and _has_any(text, P6_ACTIONS) and _has_any(text, cross_border_terms):
        hits.append(DiscoveryHit("P6_LOCATION", ("audit:p6-cross-border",), "audit", "medium"))
    if _has_any(text, P6_I2_OBJECTS) and _has_any(text, P6_I2_ACTIONS) and _has_any(text, local_terms):
        hits.append(DiscoveryHit("P6_LOCATION", ("audit:p6-local-storage",), "audit", "medium"))
    if _has_any(text, P6_I3_OBJECTS) and _has_any(text, P6_I3_ACTIONS) and _has_any(text, local_terms):
        hits.append(DiscoveryHit("P6_LOCATION", ("audit:p6-local-infra",), "audit", "medium"))
    if _has_any(text, ("retain", "keep", "preserve", "maintain")) and _has_any(text, RETENTION_OBJECTS) and _minimum_duration(text):
        hits.append(DiscoveryHit("P7_RETENTION", ("audit:p7-retention",), "audit", "low" if _has_any(text, PHYSICAL_LOG_TERMS) else "medium"))
    if (
        (_has_any(text, ("designate", "appoint", "nominate")) and _has_any(text, ("responsible for data protection", "responsible for privacy", "responsible for ensuring compliance with this act", "compliance with data")))
        or _has_any(text, ("privacy impact assessment", "data protection impact assessment", "dpia"))
    ):
        hits.append(DiscoveryHit("P7_ACCOUNTABILITY", ("audit:p7-dpo-dpia",), "audit", "medium"))
    if _has_any(text, PUBLIC_AUTHORITY_TERMS) and _has_any(text, ACCESS_POWER_TERMS) and _has_any(text, SENSITIVE_ACCESS_OBJECTS + EXTERNAL_HOLDER_TERMS):
        if _has_any(text, SENSITIVE_ACCESS_OBJECTS):
            hits.append(DiscoveryHit("P7_GOVERNMENT_ACCESS", ("audit:p7-access-sensitive",), "audit", "medium"))
        elif _has_any(text, ("records", "information")):
            hits.append(DiscoveryHit("P7_GOVERNMENT_ACCESS", ("audit:p7-access-general-records",), "audit", "low", False))
    return hits


def _lexical_group_hit(group: str, text: str, full_text: str, economy: str) -> str | None:
    if group == "P6_LOCATION":
        local_terms = _local_terms(economy)
        cross_border_terms = _cross_border_terms(economy)
        if _has_any(text, DATA_OBJECTS) and _has_any(text, P6_ACTIONS) and (_has_any(text, cross_border_terms) or _has_any(text, local_terms)):
            return "data-location-action"
        if _has_any(text, P6_I2_OBJECTS) and _has_any(text, P6_I2_ACTIONS) and _has_any(text, local_terms):
            return "local-storage"
        if _has_any(text, P6_I3_OBJECTS) and _has_any(text, P6_I3_ACTIONS) and _has_any(text, local_terms):
            return "local-infra"
    if group == "P7_RETENTION":
        if _has_any(text, ("retain", "keep", "preserve", "maintain")) and _has_any(text, RETENTION_OBJECTS) and _minimum_duration(text):
            return "retention-duration"
    if group == "P7_ACCOUNTABILITY":
        if _has_any(text, ("data protection officer", "privacy officer", "designate", "appoint", "nominate", "privacy impact assessment", "data protection impact assessment", "dpia", "impact assessment")) and _has_any(text, ("data", "privacy", "personal", "protection")):
            return "accountability-duty"
    if group == "P7_GOVERNMENT_ACCESS":
        if _has_any(text, PUBLIC_AUTHORITY_TERMS) and _has_any(text, ACCESS_POWER_TERMS) and _has_any(text, SENSITIVE_ACCESS_OBJECTS):
            return "authority-access-sensitive-data"
    return None


def _phrase_hit(text: str, specs) -> bool:
    return any(_has_any(text, spec.positive_expressions) for spec in specs)


def _semantic_threshold(text: str, specs) -> float:
    terms: set[str] = set()
    for spec in specs:
        terms.update(spec.object_terms)
        terms.update(spec.action_terms)
        terms.update(spec.geographic_or_duration_terms)
        terms.update(spec.functional_expressions)
    if not terms:
        return 0.0
    hits = sum(1 for term in terms if term and term in text)
    return hits / max(8, min(len(terms), 40))


def _semantic_threshold_required(group: str) -> float:
    return {
        "P6_LOCATION": 0.16,
        "P6_TREATY": 0.16,
        "P7_DATA_PROTECTION_FRAMEWORK": 0.16,
        "P7_CYBERSECURITY_FRAMEWORK": 0.16,
        "P7_RETENTION": 0.18,
        "P7_ACCOUNTABILITY": 0.18,
        "P7_GOVERNMENT_ACCESS": 0.18,
    }[group]


def _source_family_threshold(group: str) -> float:
    return {
        "P6_LOCATION": 0.10,
        "P6_TREATY": 0.10,
        "P7_DATA_PROTECTION_FRAMEWORK": 0.10,
        "P7_CYBERSECURITY_FRAMEWORK": 0.10,
        "P7_RETENTION": 0.12,
        "P7_ACCOUNTABILITY": 0.12,
        "P7_GOVERNMENT_ACCESS": 0.12,
    }[group]


def _minimal_group_signal(group: str, text: str, economy: str) -> bool:
    if group == "P6_LOCATION":
        return _has_any(text, DATA_OBJECTS + P6_I3_OBJECTS) and _has_any(text, P6_ACTIONS + P6_I3_ACTIONS) and (
            _has_any(text, _local_terms(economy)) or _has_any(text, _cross_border_terms(economy))
        )
    if group == "P7_RETENTION":
        return _has_any(text, ("retain", "keep", "preserve", "maintain")) and _has_any(text, RETENTION_OBJECTS) and _minimum_duration(text)
    if group == "P7_ACCOUNTABILITY":
        return _has_any(text, ("data protection officer", "privacy officer", "designate", "appoint", "nominate", "privacy impact assessment", "data protection impact assessment", "dpia", "impact assessment")) and _has_any(text, ("data", "privacy", "personal", "protection"))
    if group == "P7_GOVERNMENT_ACCESS":
        return _has_any(text, PUBLIC_AUTHORITY_TERMS) and _has_any(text, ACCESS_POWER_TERMS) and _has_any(text, SENSITIVE_ACCESS_OBJECTS)
    return False


def _candidate_task(*, context: ProvisionContext, clause_id: str, focal_text: str, clauses: list[tuple[str, str]], decision_group: str, economy: str, recall_source: str, matched_patterns: list[str], audit_confidence: str | None) -> CandidateTask:
    definition = indicator_definition(DECISION_GROUPS[decision_group][0]) if len(DECISION_GROUPS[decision_group]) == 1 else None
    evidence_segments = _evidence_segments(focal_text, clauses, clause_id, context)
    return CandidateTask(
        task_id=_task_id(context, clause_id, decision_group, focal_text),
        economy=economy,
        indicator_id=DECISION_GROUPS[decision_group][0] if len(DECISION_GROUPS[decision_group]) == 1 else None,
        task_kind=definition.task_kind if definition else "provision",
        source_type="subsidiary_legislation" if "subsidiary" in context.instrument_type.casefold() else "domestic_legislation",
        document_id=context.document_id,
        source_record_id=context.provision_id,
        law_title=context.law_title,
        instrument_type=context.instrument_type,
        parent_instrument=None,
        focal_provision_id=clause_id,
        focal_quote=focal_text,
        processing_mode=context.processing_mode,
        citation_mode=context.citation_mode,
        source_locator=context.source_locator,
        focal_text_hash=context.focal_text_hash,
        document_content_hash=context.document_content_hash,
        canonical_schema_version=context.canonical_schema_version,
        provision_metadata_snapshot=context.provision_metadata_snapshot,
        normalized_provision_id=_normalize_id(clause_id),
        section_heading=context.section_reference,
        focal_text=focal_text,
        supporting_provision_ids=[cid for cid, _body in clauses if cid != clause_id],
        parent_section_text=context.text,
        supporting_context=_supporting_context(evidence_segments),
        route_topic=decision_group,  # type: ignore[arg-type]
        candidate_indicators=list(DECISION_GROUPS[decision_group]),  # type: ignore[list-item]
        source_url=context.source_url,
        contract_version=definition.contract_version if definition else "",
        evidence_segments=evidence_segments,
        recall_source=recall_source,  # type: ignore[arg-type]
        matched_patterns=matched_patterns,
        audit_confidence=audit_confidence,  # type: ignore[arg-type]
    )


def _evidence_segments(focal_text: str, clauses: list[tuple[str, str]], focal_clause_id: str, context: ProvisionContext) -> dict[str, str]:
    segments = {"S1": focal_text}
    index = 2
    for label, text in clauses:
        if label == focal_clause_id:
            continue
        segments[f"S{index}"] = f"{label}: {text}"
        index += 1
        if index > 10:
            break
    if len(clauses) == 1:
        segments[f"S{index}"] = f"Parent section: {context.text[:4000]}"
    heading = " | ".join(value for value in (context.part_heading, context.division_heading, context.section_reference) if value)
    if heading:
        segments["H1"] = f"Headings: {heading}"
    return segments


def _supporting_context(segments: dict[str, str]) -> str:
    return "\n".join(f"[{sid}] {text}" for sid, text in segments.items() if sid != "S1")


def _framework_title_candidate(title: str) -> bool:
    folded = title.casefold()
    return any(
        term in folded
        for term in (
            "personal data protection",
            "privacy act",
            "privacy ",
            "cybersecurity",
            "cyber security",
            "critical infrastructure",
        )
    )


def _audit_row(context: ProvisionContext, clause_id: str, hit: DiscoveryHit, decision: str) -> dict:
    return {
        "document_id": context.document_id,
        "law_title": context.law_title,
        "provision_id": clause_id,
        "route_topic": hit.decision_group,
        "recall_source": "audit_only",
        "matched_patterns": list(hit.matched_patterns),
        "audit_confidence": hit.confidence,
        "promotion_decision": decision,
    }


def _dedupe_key(task: CandidateTask) -> str:
    element = next((pattern for pattern in task.matched_patterns if pattern.startswith("framework_element:")), "")
    return "|".join([task.document_id, task.normalized_provision_id, task.route_topic, ",".join(task.candidate_indicators), task.task_kind, element])


def _task_id(context: ProvisionContext, clause_id: str, group: str, text: str) -> str:
    digest = hashlib.sha256(f"{context.economy}|{context.document_id}|{clause_id}|{group}|{_fold(text)}".encode("utf-8")).hexdigest()[:20]
    return f"{context.document_id}:{_normalize_id(clause_id)}:{group}:{digest}"


def _context_text(context: ProvisionContext, text: str) -> str:
    return " ".join([context.law_title, context.part_heading, context.division_heading, context.section_reference, text])


def _minimum_duration(text: str) -> bool:
    if _has_any(text, ("not exceeding", "no longer than", "up to ", "may prescribe", "prescribed period may be")):
        return False
    return bool(
        re.search(r"\b(at least|not less than|minimum period|minimum of)\b", text)
        or re.search(r"\bfor (?:a )?period of \d+ (?:year|years|month|months|day|days)\b", text)
        or re.search(r"\buntil .*?\d+ (?:year|years|month|months|day|days) after\b", text)
        or re.search(r"\b\d+ (?:year|years|month|months|day|days) after\b", text)
    )


def _normalize_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _fold(value: str) -> str:
    return re.sub(r"\s+", " ", value).casefold()


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
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


def _spec_terms(indicators: tuple[str, ...], attribute: str) -> tuple[str, ...]:
    terms: list[str] = []
    for indicator in indicators:
        terms.extend(getattr(INDICATOR_SPECS[indicator], attribute))
    return tuple(dict.fromkeys(term.casefold() for term in terms if term))


DATA_OBJECTS = _spec_terms(("P6-I1", "P6-I2", "P6-I4"), "object_terms")
P6_ACTIONS = _spec_terms(("P6-I1", "P6-I2", "P6-I3", "P6-I4"), "action_terms")
P6_I2_OBJECTS = INDICATOR_SPECS["P6-I2"].object_terms
P6_I2_ACTIONS = INDICATOR_SPECS["P6-I2"].action_terms
P6_I3_OBJECTS = INDICATOR_SPECS["P6-I3"].object_terms
P6_I3_ACTIONS = INDICATOR_SPECS["P6-I3"].action_terms
CROSS_BORDER_TERMS = tuple(
    dict.fromkeys(
        [
            *INDICATOR_SPECS["P6-I1"].geographic_or_duration_terms,
            *INDICATOR_SPECS["P6-I4"].geographic_or_duration_terms,
            "outside the jurisdiction",
            "another jurisdiction",
        ]
    )
)
LOCAL_TERMS = tuple(
    dict.fromkeys(
        [
            *INDICATOR_SPECS["P6-I2"].geographic_or_duration_terms,
            *INDICATOR_SPECS["P6-I3"].geographic_or_duration_terms,
        ]
    )
)


def _local_terms(economy: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys([*LOCAL_TERMS, *domestic_terms(economy)]))


def _cross_border_terms(economy: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys([*CROSS_BORDER_TERMS, *foreign_terms(economy)]))
RETENTION_OBJECTS = INDICATOR_SPECS["P7-I3"].object_terms + ("records", "data", "information")
PHYSICAL_LOG_TERMS = ("equipment", "facility", "inspection", "fire safety", "petroleum", "dangerous goods", "weapon", "amusement ride", "aircraft maintenance", "workplace safety")
PUBLIC_AUTHORITY_TERMS = ("minister", "authority", "commissioner", "police", "authorised officer", "authorized officer", "public prosecutor", "public authority", "court")
ACCESS_POWER_TERMS = ("require", "compel", "obtain", "access", "inspect", "produce", "furnish", "copy", "seize", "retrieve", "intercept", "query")
SENSITIVE_ACCESS_OBJECTS = ("personal data", "customer information", "user data", "subscriber information", "subscriber data", "communications data", "traffic data", "location data", "computer data", "account information", "transaction information", "identification records", "identifiable")
EXTERNAL_HOLDER_TERMS = ("person", "organisation", "organization", "company", "bank", "employer", "provider", "licensee", "operator", "subscriber", "customer", "user")
