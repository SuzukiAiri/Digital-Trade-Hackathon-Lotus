"""Document-direct mapping over Docling page-aware text artifacts."""

from __future__ import annotations

import time

from .economy_profiles import economy_profile
from .indicator_specs import INDICATOR_SPECS, P4_MAPPING_BOUNDARY_RULES, specs_for_group
from .model_config import pdf_mapper_model_name, openai_client
from .models import PDFDocumentTask, PDFMappingDecision, P67PDFMappingDecision


PDF_PROMPT_VERSION = "rdtii-document-direct-mapper-v2-docling-pages"
PDF_OUTPUT_SCHEMA_VERSION = "rdtii-pdf-mapping-schema-v1"
PDF_CITATION_PROMPT_VERSION = "rdtii-docling-citation-v1-page-text"
P4_PDF_PROMPT_VERSION = "rdtii-p4-document-direct-mapper-v2-boundary-contracts"
P4_PDF_OUTPUT_SCHEMA_VERSION = "rdtii-p4-pdf-mapping-schema-v2-element-quality"


def pdf_prompt_version_for_task(task: PDFDocumentTask) -> str:
    return P4_PDF_PROMPT_VERSION if any(indicator.startswith("P4-") for indicator in task.candidate_indicators) else PDF_PROMPT_VERSION


def build_pdf_mapping_prompt(task: PDFDocumentTask) -> str:
    if any(indicator.startswith("P4-") for indicator in task.candidate_indicators):
        return _build_p4_pdf_mapping_prompt(task)
    profile = economy_profile(task.economy)
    specs = []
    candidate_set = set(task.candidate_indicators)
    for group in ("P6_LOCATION", "P7_RETENTION", "P7_ACCOUNTABILITY", "P7_GOVERNMENT_ACCESS"):
        for spec in specs_for_group(group):
            if spec.indicator_id == "P6-I5":
                continue
            if candidate_set and spec.indicator_id not in candidate_set:
                continue
            specs.append(
                "\n".join(
                    [
                        f"{spec.indicator_id}:",
                        f"- decision_group: {group}",
                        f"- required_elements: {'; '.join(spec.required_elements)}",
                        f"- excluded_cases: {'; '.join(spec.excluded_cases)}",
                        f"- evidence_rules: {'; '.join(spec.evidence_rules)}",
                        f"- context_rules: {'; '.join(spec.context_rules)}",
                        f"- positive_expressions: {'; '.join(spec.positive_expressions[:10])}",
                        f"- object_terms: {'; '.join(spec.object_terms[:15])}",
                        f"- action_terms: {'; '.join(spec.action_terms[:12])}",
                        f"- adjacent_indicators: {'; '.join(spec.adjacent_indicators)}",
                    ]
                )
            )
    pages = task.matched_context.strip() or "(No candidate page context was provided.)"
    candidate_indicators = ", ".join(task.candidate_indicators) if task.candidate_indicators else "P6/P7 candidate indicators"
    matched_pages = ", ".join(str(page) for page in task.matched_pages) if task.matched_pages else "unknown"
    return f"""You are performing document-direct legal mapping for RDTII Pillar 6/7 using local Docling-extracted page text.

Use only the page-marked text in this prompt as the authority. Do not infer from the file name, law title, page number, or outside knowledge.
Return strict JSON matching the provided schema. Do not answer in prose.

Economy: {task.economy}
Domestic terms: {'; '.join(profile.domestic_terms)}
Foreign terms: {'; '.join(profile.foreign_terms)}
Document ID: {task.document_id}
Law title: {task.title}
Law number/reference: {task.official_number}
Collection: {task.collection}
Language: {task.language}
Official source URL: {task.source_url}
Prefilter status: {task.prefilter_status}
PDF page count: {task.page_count or 'unknown'}
Matched pages: {matched_pages}
Candidate indicators: {candidate_indicators}
Document text hash: {task.document_text_hash or task.source_sha256}

Scope:
- Cover P6-I1 through P7-I5, except P6-I5. P6-I5 is external treaty status and must not be mapped from this PDF.
- One PDF may contain zero, one, or many claims.
- If one article supports two indicators, output two claims.
- Each claim must identify a concrete article/section/paragraph and a 1-based PDF page number.
- verbatim_snippet must be exact legal text from the provided page text, not a summary.
- Do not use the law title, file name, or page number as article.
- Only fill article when a printed article/section/regulation/rule/paragraph identifier is explicit in the page text. Otherwise leave it empty.
- Distinguish operative focal provisions from supporting-only provisions.
- P7-I3 requires a mandatory minimum retention period; retention limitation, audit/review frequency, or maximum-only retention is not enough.
- P7-I3 excludes government-data-only and public-administration-internal-only retention.
- P7-I4 must distinguish DPO, DPIA, or both using accountability_path.
- P7-I5 must cover public authority, compulsory power, externally held data, personal/identifiable data, and judicial_authorization status.

Indicator rules generated from the canonical local IndicatorSpec:
{chr(10).join(specs)}

Docling page text:
{pages}

Output schema:
{{
  "document_id": "{task.document_id}",
  "document_decision": "no_match|claims_found|uncertain|technical_failure",
  "claims": [
    {{
      "indicator_id": "P6-I1",
      "article": "Section 12(3)(a)",
      "page_number": 17,
      "verbatim_snippet": "exact text from PDF",
      "mapping_rationale": "...",
      "coverage": "horizontal|sectoral|uncertain",
      "sector": null,
      "focal_role": "operative|supporting_only|uncertain",
      "confidence": 0.0,
      "elements": [
        {{"element_id": "OPERATIVE_RULE", "status": "supported|not_supported|uncertain", "evidence_ids": ["PDF_PAGE"], "reason": "..."}}
      ],
      "exclusions": [
        {{"exclusion_id": "NO_DURATION", "status": "triggered|not_triggered|uncertain", "evidence_ids": [], "reason": "..."}}
      ],
      "attributes": {{
        "coverage": null,
        "sector": null,
        "record_scope_basis": null,
        "judicial_authorization": null,
        "accountability_path": null,
        "minimum_duration_value": null,
        "minimum_duration_unit": null,
        "trigger_event": null
      }}
    }}
  ],
  "document_notes": ""
}}
"""


def _build_p4_pdf_mapping_prompt(task: PDFDocumentTask) -> str:
    candidates = [
        indicator
        for indicator in task.candidate_indicators
        if indicator in {"P4-I1", "P4-I2", "P4-I3", "P4-I5", "P4-I6", "P4-I9", "P4-I10"}
    ]
    contracts = []
    for indicator in candidates:
        spec = INDICATOR_SPECS[indicator]
        contracts.append(
            f"{indicator} ({spec.title}): required={'; '.join(spec.required_elements)}; "
            f"excluded={'; '.join(spec.excluded_cases)}"
        )
    pages = task.matched_context.strip() or "(No candidate page context was provided.)"
    boundaries = "\n".join(f"- {rule}" for rule in P4_MAPPING_BOUNDARY_RULES)
    return f"""You are performing document-direct legal mapping for RDTII Pillar 4 using local Docling page text.

Use only the page-marked text below. Do not infer from the title, filename, page number, or outside knowledge.
Return strict JSON matching PDFMappingDecision. P4-I4, P4-I7, and P4-I8 are external treaty status and must never be returned.

Rules:
- Every claim must cite an exact printed article/section, 1-based page number, and verbatim snippet from the supplied text.
- Return no_match for definitions, headings, directories, pure cross-references, or supporting-only text.
- P4-I1 requires a material patent-application burden, not routine forms or deadlines.
- P4-I3 requires the complete operative patent-right restriction; keywords alone are insufficient.
- P4-I9 requires protected subject, compelled disclosure, and legal compulsion. Extract safeguards in the rationale.
- P4-I6 requires an explicit online nexus; general copyright remedies are insufficient.
- For P4-I2, P4-I5, P4-I6, and P4-I10, every claim is element-level evidence. Set both framework element attributes to the exact configured element.
- Do not infer framework absence from missing evidence.
- Keep indicator IDs as strings.

Canonical P4 boundary rules shared with the structured-provision path:
{boundaries}

Economy: {task.economy}
Document ID: {task.document_id}
Law title: {task.title}
Law number/reference: {task.official_number}
Official source URL: {task.source_url}
Candidate indicators: {', '.join(candidates)}
Document text hash: {task.document_text_hash or task.source_sha256}

Indicator contracts:
{chr(10).join(contracts)}

Docling page text:
{pages}
"""


def extract_pdf_mapping_decision(task: PDFDocumentTask, model_name: str | None = None, *, max_retries: int = 1) -> tuple[PDFMappingDecision, int]:
    model = model_name or pdf_mapper_model_name()
    prompt = build_pdf_mapping_prompt(task)
    schema = (
        PDFMappingDecision
        if any(str(indicator).startswith("P4-") for indicator in task.candidate_indicators)
        else P67PDFMappingDecision
    )
    parsed, retries = _parse_text(prompt, schema, model, max_retries=max_retries)
    return PDFMappingDecision.model_validate(parsed.model_dump()), retries


def _parse_text(prompt: str, schema: type, model: str, *, max_retries: int) -> tuple[object, int]:
    client = openai_client()
    attempt = 0
    while True:
        try:
            response = client.responses.parse(
                model=model,
                input=[
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}],
                    }
                ],
                text_format=schema,
            )
            return response.output_parsed, attempt
        except Exception:
            if attempt >= max_retries:
                raise
            time.sleep(min(20, 2 ** attempt * 3))
            attempt += 1
