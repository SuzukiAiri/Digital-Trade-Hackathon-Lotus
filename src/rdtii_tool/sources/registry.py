"""Defensive loader for official country source registries."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from rdtii_tool.config_loader import load_country_config


@dataclass(slots=True)
class SourceRecord:
    """Normalized source metadata used by discovery and downloading."""

    country: str = ""
    name: str = ""
    base_url: str = ""
    source_type: str = ""
    source_category: str = ""
    official_status: str = ""
    priority: int = 100
    notes: str = ""
    mvp_enabled: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return (urlsplit(self.base_url).hostname or "").lower()

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


class SourceRegistry:
    """Collection of normalized official sources for one country."""

    def __init__(self, country: str, sources: Iterable[SourceRecord]) -> None:
        self.country = country.upper()
        self.sources = list(sources)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "SourceRegistry":
        country = str(config.get("country", {}).get("code", "")).upper()
        records = []
        for raw_source in config.get("sources", []):
            if not isinstance(raw_source, dict):
                continue
            expected_types = raw_source.get("expected_source_types", [])
            if isinstance(expected_types, str):
                expected_types = [expected_types]
            source_type = str(raw_source.get("source_type", "")).strip()
            if not source_type and expected_types:
                source_type = str(expected_types[0])

            records.append(
                SourceRecord(
                    country=str(raw_source.get("country", country)).upper(),
                    name=str(raw_source.get("name", "")).strip(),
                    base_url=str(raw_source.get("base_url", "")).strip(),
                    source_type=source_type,
                    source_category=str(
                        raw_source.get("source_category", "")
                    ).strip(),
                    official_status=str(
                        raw_source.get("official_status", "")
                    ).strip(),
                    priority=cls._as_int(raw_source.get("priority"), 100),
                    notes=str(raw_source.get("notes", "")).strip(),
                    mvp_enabled=bool(raw_source.get("mvp_enabled", False)),
                    metadata={
                        key: value
                        for key, value in raw_source.items()
                        if key
                        not in {
                            "country",
                            "name",
                            "base_url",
                            "source_type",
                            "source_category",
                            "official_status",
                            "priority",
                            "notes",
                            "mvp_enabled",
                        }
                    },
                )
            )
        return cls(country=country, sources=records)

    def enabled_sources(self) -> list[SourceRecord]:
        return sorted(
            (source for source in self.sources if source.mvp_enabled),
            key=lambda source: (source.priority, source.name.casefold()),
        )

    def official_domains(self, *, enabled_only: bool = False) -> set[str]:
        sources = self.enabled_sources() if enabled_only else self.sources
        return {
            source.domain
            for source in sources
            if source.domain and source.official_status.casefold() == "official"
        }

    def get(self, name: str) -> SourceRecord | None:
        normalized = name.casefold()
        return next(
            (
                source
                for source in self.sources
                if source.name.casefold() == normalized
            ),
            None,
        )

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default


def load_source_registry(
    country_code: str = "SG",
    *,
    config_dir: str | Path | None = None,
) -> SourceRegistry:
    """Load and normalize a configured country source registry."""
    return SourceRegistry.from_config(
        load_country_config(country_code, config_dir=config_dir)
    )
