"""Discovery Tag assignment against the supplied Legal Inventory baseline."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path


LEGAL_INVENTORY_FILENAME = "Singapore, Malaysia, Australia, Legal Inventory.csv"
BASELINE_REGISTRY_VERSION = "legal-inventory-baseline-v2-law-number-identities"
TARGET_ECONOMIES = {"singapore", "australia", "malaysia"}
TARGET_INDICATOR_PREFIXES = ("P4-", "P6-", "P7-")
INDICATOR_MAPPING_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("P4-I10", ("trade secret framework", "breach of confidence", "trade secret protection")),
    ("P4-I9", ("mandatory disclosure", "source code", "algorithm", "trade secret disclosure")),
    ("P4-I8", ("wipo performances and phonograms treaty", "wppt")),
    ("P4-I7", ("wipo copyright treaty", "wct")),
    ("P4-I6", ("online copyright enforcement", "website blocking", "online infringement")),
    ("P4-I5", ("copyright framework", "copyright exceptions", "fair dealing", "fair use")),
    ("P4-I4", ("patent cooperation treaty", "pct")),
    ("P4-I3", ("compulsory licence", "government use", "patent working requirement")),
    ("P4-I2", ("patent enforcement", "patent remedies", "provisional measures")),
    ("P4-I1", ("patent application", "foreign applicant", "patent filing")),
    ("P7-I4", ("data protection officer", "privacy impact assessment", "dpo", "dpia")),
    ("P7-I5", ("government access", "public authority access", "law enforcement access", "compelled disclosure")),
    ("P7-I3", ("data retention", "record retention", "records retention", "record-keeping", "record keeping", "retention period")),
    ("P7-I2", ("cybersecurity", "cyber security", "cyber-security")),
    ("P7-I1", ("data protection", "privacy framework", "personal data protection", "privacy law")),
    ("P6-I5", ("digital trade agreement", "trade agreement", "cross-border data flow commitment")),
    ("P6-I4", ("cross-border data transfer", "cross border data transfer", "transfer of personal data", "overseas transfer", "data flows")),
    ("P6-I3", ("local server", "data centre", "data center", "local computing", "computing facilities")),
    ("P6-I2", ("local storage", "data storage", "stored locally", "kept domestically", "kept in-country")),
    ("P6-I1", ("data localisation", "data localization", "local processing", "domestic processing", "data local")),
)


class DiscoveryTagBaselineError(RuntimeError):
    """Raised when the required Legal Inventory baseline cannot be used."""


@dataclass(frozen=True)
class DiscoveryTagMatch:
    discovery_tag: str
    baseline_match_key: str = ""
    baseline_match_basis: str = ""
    baseline_row_id: str = ""
    baseline_file_hash: str = ""


@dataclass
class BaselineRow:
    row_id: str
    economy: str
    indicator_id: str
    law_identity: str
    law_identities: tuple[str, ...]
    law_display: str
    fields: dict[str, str]


@dataclass
class KnownEvidenceRegistry:
    source_path: Path
    file_hash: str
    header: list[str]
    rows: list[BaselineRow] = field(default_factory=list)
    unmapped_rows: list[dict[str, str]] = field(default_factory=list)

    @property
    def registry_version(self) -> str:
        return BASELINE_REGISTRY_VERSION

    def match(
        self,
        *,
        economy: str,
        indicator_id: str,
        law_name: str,
        law_number_ref: str = "",
        article: str = "",
        verbatim_snippet: str = "",
    ) -> DiscoveryTagMatch:
        economy_key = normalize_economy(economy)
        indicator_key = normalize_indicator_id(indicator_id)
        if not economy_key or not indicator_key:
            return DiscoveryTagMatch("NEW", baseline_file_hash=self.file_hash)

        candidates = law_identity_candidate_entries(law_name, law_number_ref)
        for candidate, basis in candidates:
            key = (economy_key, indicator_key, candidate)
            for row in self._index().get(key, ()):
                return DiscoveryTagMatch(
                    discovery_tag="KNOWN",
                    baseline_match_key="|".join(key),
                    baseline_match_basis=f"economy+indicator+{basis}",
                    baseline_row_id=row.row_id,
                    baseline_file_hash=self.file_hash,
                )
        return DiscoveryTagMatch("NEW", baseline_file_hash=self.file_hash)

    def match_row(self, row: dict[str, str]) -> DiscoveryTagMatch:
        return self.match(
            economy=str(row.get("Economy") or row.get("economy") or ""),
            indicator_id=str(row.get("Indicator ID") or row.get("indicator_id") or ""),
            law_name=str(row.get("Law Name") or row.get("law_name") or ""),
            law_number_ref=str(row.get("Law Number / Ref") or row.get("law_number_ref") or ""),
            article=str(row.get("Article / Section") or row.get("article") or ""),
            verbatim_snippet=str(row.get("Verbatim Snippet") or row.get("focal_quote") or ""),
        )

    def summary(self) -> dict:
        by_economy_indicator: dict[str, int] = {}
        for row in self.rows:
            key = f"{row.economy}:{row.indicator_id}"
            by_economy_indicator[key] = by_economy_indicator.get(key, 0) + 1
        return {
            "source": str(self.source_path),
            "file_hash": self.file_hash,
            "registry_version": self.registry_version,
            "header": self.header,
            "recognized_rows": len(self.rows),
            "unrecognized_rows": len(self.unmapped_rows),
            "by_economy_indicator": dict(sorted(by_economy_indicator.items())),
            "indicator_mapping_rules": {indicator: list(needles) for indicator, needles in INDICATOR_MAPPING_RULES},
        }

    def write_audit(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "summary": self.summary(),
            "unmapped_rows": self.unmapped_rows,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _index(self) -> dict[tuple[str, str, str], list[BaselineRow]]:
        index = getattr(self, "__index", None)
        if index is not None:
            return index
        built: dict[tuple[str, str, str], list[BaselineRow]] = {}
        for row in self.rows:
            for identity in row.law_identities:
                built.setdefault((row.economy, row.indicator_id, identity), []).append(row)
        setattr(self, "__index", built)
        return built


def load_legal_inventory_registry(project_root: Path, *, required: bool = True) -> KnownEvidenceRegistry:
    path = Path(project_root) / LEGAL_INVENTORY_FILENAME
    if not path.exists():
        if required:
            raise DiscoveryTagBaselineError(
                f"Legal Inventory baseline missing: {path}. Discovery Tag export cannot safely continue."
            )
        return KnownEvidenceRegistry(source_path=path, file_hash="", header=[])
    data = path.read_bytes()
    file_hash = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DiscoveryTagBaselineError(f"Legal Inventory baseline is not UTF-8 compatible: {path}") from exc

    reader = csv.DictReader(text.splitlines())
    header = list(reader.fieldnames or [])
    required_columns = {"country", "Act.and.or.practice"}
    missing = sorted(required_columns - set(header))
    if missing:
        raise DiscoveryTagBaselineError(f"Legal Inventory baseline missing required columns {missing}: {path}")

    registry = KnownEvidenceRegistry(source_path=path, file_hash=file_hash, header=header)
    for idx, raw in enumerate(reader, start=1):
        row = {str(key or ""): str(value or "") for key, value in raw.items()}
        economy = normalize_economy(row.get("country", ""))
        if economy not in TARGET_ECONOMIES:
            continue
        indicator_id = infer_indicator_id(row)
        if not indicator_id or not indicator_id.startswith(TARGET_INDICATOR_PREFIXES):
            registry.unmapped_rows.append(_unmapped_payload(idx, row, reason="indicator_not_recognized"))
            continue
        law_identities = law_identity_candidates(row.get("Act.and.or.practice", ""))
        law_identity = law_identities[0] if law_identities else ""
        if not law_identities:
            registry.unmapped_rows.append(_unmapped_payload(idx, row, reason="law_identity_missing"))
            continue
        registry.rows.append(
            BaselineRow(
                row_id=str(idx),
                economy=economy,
                indicator_id=indicator_id,
                law_identity=law_identity,
                law_identities=law_identities,
                law_display=row.get("Act.and.or.practice", ""),
                fields=row,
            )
        )
    return registry


def infer_indicator_id(row: dict[str, str]) -> str:
    """Conservatively map Legal Inventory text to current indicator IDs."""

    text = " | ".join(str(row.get(field) or "") for field in ("name", "policy.description", "cluster", "Coverage"))
    norm = normalize_text(text)
    explicit = _explicit_indicator(norm)
    if explicit:
        return explicit

    for indicator_id, needles in INDICATOR_MAPPING_RULES:
        if any(needle in norm for needle in needles):
            return indicator_id
    return ""


def normalize_economy(value: str) -> str:
    text = normalize_text(value)
    if "singapore" in text:
        return "singapore"
    if "australia" in text:
        return "australia"
    if "malaysia" in text:
        return "malaysia"
    return text


def normalize_indicator_id(value: str) -> str:
    text = str(value or "").strip().upper().replace("_", "-")
    match = re.search(r"\bP([0-9]+)\s*[-–—]?\s*I\s*([0-9]+)\b", text)
    if match:
        return f"P{match.group(1)}-I{match.group(2)}"
    return text


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = re.sub(r"[^\w\s./()'-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_law_identity(value: str) -> str:
    text = normalize_text(value)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b(the)\b", " ", text)
    text = re.sub(r"\bact\s+no\.\s*", "act ", text)
    text = re.sub(r"\bp\.?\s*u\.?\s*\(\s*a\s*\)", "pua", text)
    text = re.sub(r"\bp\.?\s*u\.?\s*\(\s*b\s*\)", "pub", text)
    text = re.sub(r"\bregulations?\b", "regulation", text)
    text = re.sub(r"\brules?\b", "rule", text)
    text = re.sub(r"\bord(er)?s?\b", "order", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def law_identity_candidates(law_name: str, law_number_ref: str = "") -> tuple[str, ...]:
    return tuple(candidate for candidate, _basis in law_identity_candidate_entries(law_name, law_number_ref))


def law_identity_candidate_entries(law_name: str, law_number_ref: str = "") -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    values = (
        (law_number_ref, "normalized_law_number_ref"),
        (law_name, "act_number_from_law_name"),
        (f"{law_name} {law_number_ref}".strip(), "act_number_from_law_name"),
        (law_name, "canonical_full_law_name"),
        (f"{law_name} {law_number_ref}".strip(), "canonical_full_law_name"),
        (law_number_ref, "canonical_full_law_name"),
    )
    for value, basis in values:
        for key in normalize_law_number_identities(value):
            _append_candidate(entries, key, basis)
        if basis == "canonical_full_law_name":
            key = normalize_law_identity(value)
            _append_candidate(entries, key, basis)
    return tuple(entries)


def normalize_law_number_identities(value: str) -> tuple[str, ...]:
    text = normalize_text(value)
    out: list[str] = []

    def add(identity: str) -> None:
        if identity and identity not in out:
            out.append(identity)

    for match in re.finditer(r"\bact\s*(?:no\.?\s*)?([a-z]?\d+[a-z]?)\b", text, flags=re.I):
        token = match.group(1).casefold()
        add(f"act:{token}")
        if token.startswith("a") and re.search(r"\d", token):
            add(f"amendment:{token}")
    for match in re.finditer(r"\b(?:act\s*)?([a]\d{3,5}[a-z]?)\b", text, flags=re.I):
        token = match.group(1).casefold()
        add(f"act:{token}")
        add(f"amendment:{token}")
    for match in re.finditer(r"\bp\.?\s*u\.?\s*\(\s*([ab])\s*\)\s*([0-9]+)\s*/\s*([0-9]{2,4})\b", text, flags=re.I):
        kind = "pua" if match.group(1).casefold() == "a" else "pub"
        add(f"{kind}:{int(match.group(2))}/{match.group(3)}")
    for match in re.finditer(r"\b(sabah|sarawak)\s+cap\.?\s*([0-9a-z]+)\b", text, flags=re.I):
        add(f"{match.group(1).casefold()}cap:{match.group(2).casefold()}")
    stripped = text.strip()
    if re.fullmatch(r"\d{1,4}[a-z]?", stripped):
        add(f"act:{stripped.casefold()}")
    if re.fullmatch(r"a\d{3,5}[a-z]?", stripped):
        add(f"act:{stripped.casefold()}")
        add(f"amendment:{stripped.casefold()}")
    return tuple(out)


def _append_candidate(entries: list[tuple[str, str]], key: str, basis: str) -> None:
    if key and key not in {candidate for candidate, _ in entries}:
        entries.append((key, basis))


def _explicit_indicator(norm_text: str) -> str:
    match = re.search(r"\bp\s*([0-9]+)\s*[-–—]?\s*i\s*([0-9]+)\b", norm_text)
    if match:
        return f"P{match.group(1)}-I{match.group(2)}"
    return ""


def _unmapped_payload(row_id: int, row: dict[str, str], *, reason: str) -> dict[str, str]:
    return {
        "row_id": str(row_id),
        "reason": reason,
        "country": row.get("country", ""),
        "Act.and.or.practice": row.get("Act.and.or.practice", ""),
        "name": row.get("name", ""),
        "policy.description": row.get("policy.description", ""),
        "cluster": row.get("cluster", ""),
    }


def validate_discovery_tag(value: str) -> str:
    tag = str(value or "").strip().upper()
    if tag not in {"NEW", "KNOWN"}:
        raise DiscoveryTagBaselineError(f"Discovery Tag must be NEW or KNOWN, got {value!r}")
    return tag
