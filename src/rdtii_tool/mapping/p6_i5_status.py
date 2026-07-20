"""P6-I5 external treaty-status assessment backed by the treaty source library."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .treaty_sources import load_treaty_registry


def check_agreements(report_dir: Path | None = None, *, economy: str = "Singapore") -> list[dict]:
    """Return P6-I5 rows from official-source treaty registry.

    P6-I5 is not a domestic provision mapping. This function keeps source problems
    explicit as external-source review rows instead of mixing them with legal review.
    """

    project_root = Path(__file__).resolve().parents[3]
    registry = load_treaty_registry(project_root)
    economy_name = {"australia": "Australia", "malaysia": "Malaysia", "singapore": "Singapore"}.get(economy.casefold(), economy)
    if registry is None:
        return [_external_gap_row("CPTPP", "treaty_registry_missing", economy=economy_name), _external_gap_row("RCEP", "treaty_registry_missing", economy=economy_name)]

    rows: list[dict] = []
    agreements = registry.get("agreements", {})
    for agreement in ("CPTPP", "RCEP"):
        agreement_record = agreements.get(agreement) or {}
        economy_record = agreement_record.get(economy_name) or {}
        commitments = agreement_record.get("data_flow_commitments") or economy_record.get("data_flow_commitments") or []
        core_complete = bool(agreement_record.get("core_complete"))
        status_complete = bool(economy_record.get("status_complete"))
        in_force = bool(economy_record.get("in_force"))
        binding = any(item.get("binding_language_present") for item in commitments)
        if core_complete and status_complete and in_force and binding:
            text_source = _official_text_source(agreement, economy_record)
            rows.append(
                {
                    "economy": economy_name,
                    "pillar_id": 6,
                    "indicator_id": "P6-I5",
                    "task_type": "external_status",
                    "queue_type": "none",
                    "result_code": None,
                    "agreement_name": agreement,
                    "participation_status": "BINDING_COMMITMENT_IN_FORCE",
                    "binding_commitment_present": True,
                    "effective_date": economy_record.get("effective_date"),
                    "official_source": text_source,
                    "agreement_text_source": text_source,
                    "article_reference": _article_reference(agreement),
                    "verbatim_snippet": _commitment_snippet(commitments, project_root=project_root, agreement=agreement),
                    "last_checked": date.today().isoformat(),
                    "review_required": False,
                    "external_source_reason": "",
                    "error": "",
                }
            )
        else:
            reason_parts = []
            if not core_complete:
                reason_parts.append("official registry did not confirm complete core treaty text and data-flow commitments")
            if not status_complete:
                reason_parts.append(f"official registry did not confirm complete {economy_name} signature/ratification/in-force status")
            if not in_force:
                reason_parts.append(f"official registry did not confirm {economy_name} in-force status")
            if not binding:
                reason_parts.append("official registry did not confirm binding data-flow commitment")
            rows.append(_external_gap_row(agreement, "; ".join(reason_parts), economy_record, economy=economy_name))
    return rows


def _registry_needs_refresh(registry: dict, economy: str) -> bool:
    agreements = registry.get("agreements", {})
    for agreement in ("CPTPP", "RCEP"):
        record = agreements.get(agreement) or {}
        economy_record = record.get(economy) or {}
        if not record.get("core_complete") or not economy_record.get("status_complete"):
            return True
    return False


def _external_gap_row(agreement: str, reason: str, economy_record: dict | None = None, *, economy: str = "Singapore") -> dict:
    economy_record = economy_record or {}
    return {
        "economy": economy,
        "pillar_id": 6,
        "indicator_id": "P6-I5",
        "task_type": "external_status",
        "queue_type": "external_source",
        "result_code": "EXTERNAL_SOURCE_UNAVAILABLE",
        "agreement_name": agreement,
        "participation_status": "REVIEW_REQUIRED",
        "binding_commitment_present": None,
        "effective_date": economy_record.get("effective_date"),
        "official_source": economy_record.get("official_status_source"),
        "agreement_text_source": economy_record.get("official_status_source"),
        "article_reference": _article_reference(agreement),
        "verbatim_snippet": _commitment_snippet(economy_record.get("data_flow_commitments") or [], agreement=agreement),
        "last_checked": date.today().isoformat(),
        "review_required": True,
        "external_source_reason": reason,
        "error": reason,
    }


def _article_reference(agreement: str) -> str:
    return "Article 14.11 / Article 14.13" if agreement == "CPTPP" else "Article 12.14 / Article 12.15"


def _official_text_source(agreement: str, economy_record: dict | None = None) -> str | None:
    if agreement == "CPTPP":
        return "https://www.mfat.govt.nz/assets/Trade-agreements/TPP/Text-ENGLISH/14.-Electronic-Commerce-Chapter.pdf"
    if agreement == "RCEP":
        return "https://asean.org/wp-content/uploads/2024/10/Regional-Comprehensive-Economic-Partnership-RCEP-Agreement-Full-Text.pdf"
    return (economy_record or {}).get("official_status_source")


def _commitment_snippet(commitments: list[dict], *, project_root: Path | None = None, agreement: str | None = None) -> str:
    local = _local_commitment_snippet(project_root, agreement)
    if local:
        return local
    for item in commitments:
        snippet = item.get("snippet")
        if snippet:
            return str(snippet)
    return ""


def _local_commitment_snippet(project_root: Path | None, agreement: str | None) -> str:
    if project_root is None or not agreement:
        return ""
    root = project_root / "data" / "legal_sources" / "international_agreements"
    if agreement == "CPTPP":
        path = root / "CPTPP" / "normalized" / "14.-Electronic-Commerce-Chapter.pdf-f79dbd68db05.txt"
        return _extract_articles(path, ("Article 14.11:", "Article 14.13:"), ("Article 14.12:", "Article 14.14:"))
    if agreement == "RCEP":
        path = root / "RCEP" / "normalized" / "Regional-Comprehensive-Economic-Partnership-RCEP-Agreement-Full-Text.pdf-b5d5b02d946e.txt"
        return _extract_articles(path, ("Article 12.14:", "Article 12.15:"), ("Article 12.15:", "Article 12.16:"))
    return ""


def _extract_articles(path: Path, starts: tuple[str, str], ends: tuple[str, str]) -> str:
    if not path.exists():
        return ""
    text = " ".join(path.read_text(encoding="utf-8", errors="ignore").split())
    parts: list[str] = []
    for start_marker, end_marker in zip(starts, ends, strict=True):
        start = text.find(start_marker)
        if start < 0:
            return ""
        end = text.find(end_marker, start + len(start_marker))
        if end < 0:
            return ""
        part = text[start:end].strip()
        if not part:
            return ""
        parts.append(part)
    return "\n\n".join(parts)
