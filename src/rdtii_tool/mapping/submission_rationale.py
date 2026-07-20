"""Public submission rationale rendering and validation.

The mapping pipeline keeps detailed internal audit rationales for traceability.
Formal RDTII submission rows need a shorter public rationale that explains the
legal mapping without leaking Mapper/Reviewer/final-audit implementation terms.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping


MAX_SUBMISSION_RATIONALE_CHARS = 300

INTERNAL_RATIONALE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bE\d+\b", re.I),
    re.compile(r"\bMapper\b", re.I),
    re.compile(r"\bReviewer\b", re.I),
    re.compile(r"\brouter\b", re.I),
    re.compile(r"\bcandidate element\b", re.I),
    re.compile(r"\bfocal evidence\b", re.I),
    re.compile(r"\bfocal clause\b", re.I),
    re.compile(r"\bfocal provision\b", re.I),
    re.compile(r"\bsupporting evidence\b", re.I),
    re.compile(r"\bno exclusion is triggered\b", re.I),
    re.compile(r"\baccepted/rejected by\b", re.I),
    re.compile(r"\baccepted by\b", re.I),
    re.compile(r"\brejected by\b", re.I),
    re.compile(r"\bfinal audit\b", re.I),
    re.compile(r"\bAccept\s*:", re.I),
    re.compile(r"\bmodel\b", re.I),
    re.compile(r"\bprompt\b", re.I),
    re.compile(r"\bschema\b", re.I),
)


def render_submission_rationale(
    indicator_id: str,
    law_name: str,
    article_section: str,
    verbatim_snippet: str,
    attributes: Mapping | None,
    notes: str,
    existing_rationale: str,
) -> str:
    """Return a public Mapping Rationale suitable for formal submission.

    This function is deterministic and does not make new legal decisions.  It
    reuses structured attributes, notes and the accepted snippet to express why
    the already-accepted row fits its indicator.
    """

    existing = _clean_text(existing_rationale)
    if existing and existing[-1] not in ".!?)":
        existing += "."
    if not validate_submission_rationale(existing):
        return existing

    attrs = _combined_attributes(attributes, notes)
    indicator = str(indicator_id or "").strip().upper()
    snippet = _clean_text(verbatim_snippet)
    text = _template(indicator, attrs, snippet)
    text = _normalize_sentence(text)
    if not validate_submission_rationale(text):
        return text
    return _safe_fallback(indicator)


def validate_submission_rationale(text: str) -> list[str]:
    """Return validation failure reasons for public Mapping Rationale."""

    failures: list[str] = []
    value = _clean_text(text)
    if value and value[-1] not in ".!?)":
        value += "."
    if not value:
        failures.append("mapping_rationale_empty")
    if len(value) > MAX_SUBMISSION_RATIONALE_CHARS:
        failures.append("mapping_rationale_over_300_chars")
    for pattern in INTERNAL_RATIONALE_PATTERNS:
        if pattern.search(value):
            failures.append(f"mapping_rationale_internal_term:{pattern.pattern}")
            break
    if value and not re.search(r"[.!?)]$", value):
        failures.append("mapping_rationale_incomplete_sentence")
    if value.endswith((";", ",", ":", "-", "—", "–")):
        failures.append("mapping_rationale_incomplete_sentence")
    folded = value.casefold()
    if "at least at least" in folded or "at least not less than" in folded:
        failures.append("mapping_rationale_redundant_minimum_phrase")
    if "requires year of assessment to retain" in folded or "requires rules and such register" in folded:
        failures.append("mapping_rationale_bad_subject")
    if re.search(r"\bas\.$", folded):
        failures.append("mapping_rationale_incomplete_sentence")
    return failures


def sanitize_submission_row(row: Mapping[str, object]) -> dict[str, str]:
    """Return a row with a public rationale rendered from existing fields."""

    out = {key: str(value or "") for key, value in dict(row).items()}
    out["Mapping Rationale"] = render_submission_rationale(
        indicator_id=out.get("Indicator ID", ""),
        law_name=out.get("Law Name", ""),
        article_section=out.get("Article / Section", ""),
        verbatim_snippet=out.get("Verbatim Snippet", ""),
        attributes=_attributes_from_notes(out.get("Notes", "")),
        notes=out.get("Notes", ""),
        existing_rationale=out.get("Mapping Rationale", ""),
    )
    return out


def _template(indicator: str, attrs: Mapping[str, object], snippet: str) -> str:
    subject = _subject_hint(snippet)
    record_object = _object_hint(attrs, snippet)
    location = _location_hint(attrs, snippet)
    duration = _duration_hint(attrs)
    condition = _condition_hint(attrs, snippet)
    framework_element = _framework_element_hint(attrs)
    accountability = _accountability_hint(attrs)
    judicial = _judicial_authorization_hint(attrs)

    if indicator == "P4-I1":
        return "The provision establishes an operative restriction or material additional burden in the patent-application process."
    if indicator == "P4-I2":
        return f"The provision supplies {framework_element} within the patent-enforcement framework."
    if indicator == "P4-I3":
        return "The provision imposes an operative restriction on the use, licensing or exercise of patent rights in stated circumstances."
    if indicator in {"P4-I4", "P4-I7", "P4-I8"}:
        return "The official WIPO status record confirms whether the treaty is in force for the economy."
    if indicator == "P4-I5":
        return f"The provision supplies {framework_element} within the copyright legal framework."
    if indicator == "P4-I6":
        return f"The provision supplies {framework_element} for copyright enforcement with an explicit online or digital nexus."
    if indicator == "P4-I9":
        return "The provision compels disclosure of protected commercial or technical information and establishes a legal consequence for non-compliance."
    if indicator == "P4-I10":
        return f"The provision supplies {framework_element} within the trade-secret protection framework."
    if indicator == "P6-I1":
        return f"The provision restricts cross-border handling of {record_object}, establishing a data or information transfer control."
    if indicator == "P6-I2":
        if location:
            return f"The provision requires {subject} to keep {record_object} {location}, establishing a domestic storage requirement."
        return f"The provision requires {subject} to keep {record_object} at a domestic location, establishing a storage-localisation requirement."
    if indicator == "P6-I3":
        return "The provision requires specified computing, server, network or digital infrastructure to be located or operated domestically."
    if indicator == "P6-I4":
        if condition:
            return f"The provision permits overseas disclosure or transfer of {record_object} only where {condition} is satisfied."
        return f"The provision permits overseas disclosure or transfer of {record_object} only subject to stated legal conditions."
    if indicator == "P6-I5":
        return "The measure records the economy's binding status or commitment under the relevant international trade agreement."
    if indicator == "P7-I1":
        return f"The provision establishes {framework_element} within the personal-data protection framework."
    if indicator == "P7-I2":
        return f"The provision establishes {framework_element} within the cybersecurity framework."
    if indicator == "P7-I3":
        if duration:
            return f"The provision requires {subject} to retain {record_object} {_retention_phrase(duration)}."
        return f"The provision requires {subject} to retain {record_object} for a stated minimum period."
    if indicator == "P7-I4":
        return f"The provision requires {accountability} in relation to personal-data processing or protection."
    if indicator == "P7-I5":
        if judicial:
            return f"The provision authorises a public authority to compel or obtain externally held identifiable information, with {judicial}."
        return "The provision authorises a public authority to compel or obtain externally held identifiable or personal information."
    return "The provision contains an accepted legal measure for the mapped RDTII indicator."


def _safe_fallback(indicator: str) -> str:
    if indicator == "P4-I1":
        return "The provision establishes a material patent-application restriction or burden."
    if indicator == "P4-I2":
        return "The provision forms part of the patent-enforcement remedies framework."
    if indicator == "P4-I3":
        return "The provision restricts the use, licensing or exercise of patent rights."
    if indicator in {"P4-I4", "P4-I7", "P4-I8"}:
        return "The official status record establishes whether the treaty is in force for the economy."
    if indicator == "P4-I5":
        return "The provision forms part of the copyright protection and exceptions framework."
    if indicator == "P4-I6":
        return "The provision forms part of the online copyright-enforcement framework."
    if indicator == "P4-I9":
        return "The provision compels disclosure of protected commercial or technical information."
    if indicator == "P4-I10":
        return "The provision forms part of the effective trade-secret protection framework."
    if indicator == "P6-I1":
        return "The provision establishes a cross-border restriction on data, information or records."
    if indicator == "P6-I2":
        return "The provision establishes a domestic storage or maintenance requirement for records or information."
    if indicator == "P6-I3":
        return "The provision establishes a local computing, server or digital infrastructure requirement."
    if indicator == "P6-I4":
        return "The provision allows cross-border data or information transfer only subject to stated legal conditions."
    if indicator == "P6-I5":
        return "The measure records binding status under the relevant international trade agreement."
    if indicator == "P7-I1":
        return "The provision forms part of the personal-data protection legal framework."
    if indicator == "P7-I2":
        return "The provision forms part of the cybersecurity legal framework."
    if indicator == "P7-I3":
        return "The provision establishes a minimum retention period for records or information."
    if indicator == "P7-I4":
        return "The provision establishes a data-protection accountability requirement."
    if indicator == "P7-I5":
        return "The provision establishes public authority access to externally held identifiable information."
    return "The provision contains an accepted legal measure for the mapped RDTII indicator."


def _combined_attributes(attributes: Mapping | None, notes: str) -> dict[str, object]:
    out: dict[str, object] = {}
    if isinstance(attributes, Mapping):
        out.update({str(key): value for key, value in attributes.items() if value not in (None, "", [])})
    for key, value in _attributes_from_notes(notes).items():
        out.setdefault(key, value)
    return out


def _attributes_from_notes(notes: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in re.split(r"\s*;\s*", str(notes or "")):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            out[key] = value
    return out


def _subject_hint(snippet: str) -> str:
    patterns = (
        r"\b(?:the|a|an)\s+([A-Za-z][A-Za-z0-9 ,()/-]{1,60}?)(?:\s+must|\s+shall|\s+is required to|\s+must not|\s+may not)",
        r"\b([A-Za-z][A-Za-z0-9 ,()/-]{1,60}?)(?:\s+must|\s+shall|\s+is required to)",
    )
    for pattern in patterns:
        match = re.search(pattern, snippet, flags=re.I)
        if match:
            candidate = _clean_text(match.group(1)).strip(" ,.;:-")
            if 2 <= len(candidate) <= 70:
                return candidate[0].lower() + candidate[1:]
    return "the regulated entity"


def _object_hint(attrs: Mapping[str, object], snippet: str) -> str:
    for key in ("information_object", "information_bearing_object_evidence", "record_scope_basis", "regulated_object", "regulated_object_type"):
        value = _short_attr(attrs.get(key))
        if value:
            return _object_phrase(value)
    match = re.search(
        r"\b(personal information|personal data|customer information|identifiable information|information|electronic records?|records?|registers?|documents?|books?|data)\b",
        snippet,
        flags=re.I,
    )
    if match:
        return _object_phrase(match.group(1))
    return "records or information"


def _object_phrase(value: str) -> str:
    text = value.replace("_", " ").strip()
    if text in {"data or information", "data information"}:
        return "data or information"
    if "record" in text or "document" in text or "book" in text or "register" in text:
        return text
    if "personal" in text and ("data" in text or "information" in text):
        return text
    if "information" in text or "data" in text:
        return text
    return "records or information"


def _location_hint(attrs: Mapping[str, object], snippet: str) -> str:
    value = _short_attr(attrs.get("location_text") or attrs.get("geographic_nexus"))
    if value:
        return value if re.match(r"\b(in|within|at)\b", value, flags=re.I) else f"in {value}"
    match = re.search(r"\b(in|within|at)\s+(Australia|Singapore|Malaysia)\b", snippet, flags=re.I)
    if match:
        return match.group(0)
    return ""


def _duration_hint(attrs: Mapping[str, object]) -> str:
    periods = attrs.get("retention_periods")
    if isinstance(periods, list) and periods:
        item = periods[0]
        if isinstance(item, Mapping):
            value = " ".join(str(item.get(key) or "").strip() for key in ("value", "unit") if str(item.get(key) or "").strip()).strip()
            trigger = str(item.get("trigger_event") or "").strip()
            return _limit_phrase(f"{value} from {trigger}" if value and trigger else value)
    for key in ("retention_periods", "minimum_duration", "duration", "duration_exact_span"):
        value = _short_attr(attrs.get(key))
        if value:
            return _limit_phrase(value)
    return ""


def _retention_phrase(duration: str) -> str:
    text = _limit_phrase(duration)
    folded = text.casefold()
    if folded.startswith(("at least", "not less than", "no less than", "minimum", "for at least", "for not less than")):
        return f"for {text}" if not folded.startswith("for ") else text
    return f"for at least {text}"


def _condition_hint(attrs: Mapping[str, object], snippet: str) -> str:
    value = _short_attr(attrs.get("transfer_condition") or attrs.get("conditions"))
    if value:
        return _limit_phrase(value, limit=90)
    lowered = snippet.casefold()
    if "reasonable steps" in lowered:
        return "reasonable steps or safeguards"
    if "consent" in lowered:
        return "consent or legal permission"
    if "approval" in lowered or "authorised" in lowered or "authorized" in lowered:
        return "approval or authorisation"
    return ""


def _framework_element_hint(attrs: Mapping[str, object]) -> str:
    element = _short_attr(attrs.get("framework_element") or attrs.get("legal_function"))
    aliases = {
        "personal_data_scope": "the scope of the personal-data regime",
        "substantive_duties_or_rights": "a substantive privacy duty or right",
        "regulator_or_enforcement": "a regulator, enforcement power or remedy",
        "complaint_or_remedy": "a complaint or remedy mechanism",
        "governance_mechanism": "a governance or compliance mechanism",
        "cybersecurity_scope": "the scope of the cybersecurity regime",
        "substantive_cybersecurity_obligation": "a substantive cybersecurity obligation",
        "authority_or_enforcement": "a cybersecurity authority or enforcement power",
        "scope_rule": "a statutory scope rule",
        "substantive_duty": "a substantive statutory duty",
        "regulator_power": "a regulatory power",
    }
    return aliases.get(element, element.replace("_", " ") if element else "a substantive duty, right, scope rule or enforcement mechanism")


def _accountability_hint(attrs: Mapping[str, object]) -> str:
    value = _short_attr(attrs.get("accountability_path"))
    if value == "dpo":
        return "appointment or designation of a privacy or data protection officer"
    if value == "dpia":
        return "completion of a privacy or data protection impact assessment"
    if value == "dpo_and_dpia":
        return "a privacy officer and privacy impact assessment accountability measure"
    return "a privacy officer, data protection officer or privacy impact assessment"


def _judicial_authorization_hint(attrs: Mapping[str, object]) -> str:
    value = _short_attr(attrs.get("judicial_authorization"))
    if value == "required":
        return "judicial authorisation required"
    if value == "not_required":
        return "no judicial authorisation requirement recorded"
    if value:
        return f"authorisation status {value}"
    return ""


def _short_attr(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return _limit_phrase("; ".join(_short_attr(item) for item in value if _short_attr(item)))
    if isinstance(value, Mapping):
        return _limit_phrase(" ".join(str(item or "").strip() for item in value.values() if str(item or "").strip()))
    return _limit_phrase(str(value).strip())


def _limit_phrase(value: str, limit: int = 110) -> str:
    text = _clean_text(value).strip(" .;:")
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].strip(" ,;:")
    return cut or text[:limit].strip(" ,;:")


def _clean_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.translate(str.maketrans({
        "\u00a0": " ",
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
    return re.sub(r"\s+", " ", text).strip()


def _normalize_sentence(value: str) -> str:
    text = _clean_text(value)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    if text and text[-1] not in ".!?)":
        text += "."
    if len(text) <= MAX_SUBMISSION_RATIONALE_CHARS:
        return text
    cut = text[:MAX_SUBMISSION_RATIONALE_CHARS].rsplit(" ", 1)[0].strip(" ,;:")
    if not cut:
        cut = text[: MAX_SUBMISSION_RATIONALE_CHARS - 1].strip(" ,;:")
    if cut and cut[-1] not in ".!?)":
        cut += "."
    return cut[:MAX_SUBMISSION_RATIONALE_CHARS]
