"""Discovery from curated official URLs in country configuration."""

from __future__ import annotations

from typing import Any

from rdtii_tool.discovery.base import DiscoveryContext
from rdtii_tool.document_models import CandidateURL


class KnownURLDiscovery:
    """Default Singapore discovery adapter."""

    method_name = "known_url"

    def __init__(self, *, include_disabled: bool = False) -> None:
        self.include_disabled = include_disabled

    def discover(self, context: DiscoveryContext) -> list[CandidateURL]:
        entries = [
            entry
            for entry in context.country_config.get("known_official_urls", [])
            if isinstance(entry, dict)
            and (self.include_disabled or entry.get("mvp_enabled") is True)
        ]
        entries.sort(
            key=lambda entry: (
                self._priority(entry.get("priority")),
                str(entry.get("key", "")).casefold(),
            )
        )

        candidates = []
        for entry in entries:
            canonical_url = str(entry.get("canonical_url", "")).strip()
            if not canonical_url:
                continue
            source_name = str(
                entry.get("source_name", "Singapore Statutes Online")
            ).strip()
            source = context.registry.get(source_name)
            document_type = self._document_type(entry)
            candidates.append(
                CandidateURL(
                    url=canonical_url,
                    title=str(entry.get("title", "")).strip(),
                    country=context.registry.country,
                    source_name=source_name,
                    source_type=(
                        source.source_type if source is not None else document_type
                    ),
                    document_type=document_type,
                    discovered_by=self.method_name,
                    metadata={
                        "registry_key": entry.get("key", ""),
                        "canonical_url": canonical_url,
                        "candidate_indicators": list(
                            entry.get("candidate_indicators", [])
                        ),
                        "legal_rank": entry.get("legal_rank", ""),
                        "source_category": entry.get(
                            "source_category",
                            source.source_category if source else "",
                        ),
                        "notes": entry.get("notes", ""),
                        "authorising_act_key": entry.get(
                            "authorising_act_key",
                            "",
                        ),
                        "priority": self._priority(entry.get("priority")),
                    },
                )
            )
        return candidates

    @staticmethod
    def _document_type(entry: dict[str, Any]) -> str:
        value = str(
            entry.get("document_type")
            or entry.get("sso_type")
            or ""
        ).casefold()
        if value in {"subsidiary legislation", "subsidiary_legislation", "sl"}:
            return "subsidiary_legislation"
        if value == "act":
            return "act"
        return value

    @staticmethod
    def _priority(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 100
