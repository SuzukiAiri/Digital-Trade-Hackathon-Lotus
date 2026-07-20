"""Core output schemas for extracted legal evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, ClassVar


@dataclass(slots=True)
class EvidenceRow:
    """One item of legal evidence mapped to an RDTII indicator."""

    economy: str = ""
    pillar_id: str = ""
    indicator_id: str = ""
    law_name: str = ""
    law_number_or_ref: str = ""
    source_type: str = ""
    legal_rank: str = ""
    coverage: str = ""
    sector: str = ""
    article_or_section: str = ""
    verbatim_snippet: str = ""
    mapping_rationale: str = ""
    source_url: str = ""
    location_ref: str = ""
    current_version_date: str = ""
    last_updated_date: str = ""
    effective_date: str = ""
    version_status: str = ""
    binding_status: str = ""
    discovery_tag: str = ""
    confidence: float = 0.0
    review_flag: bool = False
    notes: str = ""

    FIELD_NAMES: ClassVar[tuple[str, ...]]

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        """Return output field names in their canonical order."""
        return tuple(field.name for field in fields(cls))

    def to_json_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        return asdict(self)

    def to_csv_dict(self) -> dict[str, str | float]:
        """Return a flat representation suitable for csv.DictWriter."""
        data = asdict(self)
        data["review_flag"] = "true" if self.review_flag else "false"
        return data
