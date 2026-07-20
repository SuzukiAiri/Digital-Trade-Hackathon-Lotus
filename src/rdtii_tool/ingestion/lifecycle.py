"""Defensive rule-based legal document lifecycle detection."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from rdtii_tool.document_models import LifecycleInfo


DATE_PATTERN = r"(?:\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{4}-\d{2}-\d{2})"


class LifecycleDetector:
    """Infer coarse lifecycle status without legal classification."""

    REPEALED_SIGNALS = ("repealed", "revoked", "ceased", "expired")
    PENDING_SIGNALS = (
        "not yet in force",
        "commences on",
        "coming into operation",
        "pending",
        "uncommenced",
    )

    def detect(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        raw_text: str = "",
        source_metadata: dict[str, Any] | None = None,
    ) -> LifecycleInfo:
        combined_metadata = {**(source_metadata or {}), **(metadata or {})}
        current_version_date = self._value(
            combined_metadata,
            "current_version_date",
        )
        last_updated_date = self._value(
            combined_metadata,
            "last_updated_date",
        )
        effective_date = self._value(combined_metadata, "effective_date")
        existing_status = self._value(combined_metadata, "version_status")

        text = " ".join(
            str(value)
            for value in (
                raw_text,
                existing_status,
                combined_metadata.get("status_text", ""),
                combined_metadata.get("notes", ""),
            )
            if value
        )
        normalized = re.sub(r"\s+", " ", text).casefold()

        current_version_date = current_version_date or self._extract_date(
            text,
            ("current version as at", "as at"),
        )
        last_updated_date = last_updated_date or self._extract_date(
            text,
            ("last updated",),
        )
        effective_date = effective_date or self._extract_date(
            text,
            (
                "effective date",
                "commences on",
                "coming into operation on",
            ),
        )

        notes = []
        if any(signal in normalized for signal in self.REPEALED_SIGNALS):
            status = "repealed"
            notes.append("Repeal, revocation, cessation, or expiry signal detected.")
        elif (
            any(signal in normalized for signal in self.PENDING_SIGNALS)
            or self._is_future_date(effective_date)
        ):
            status = "pending"
            notes.append("Pending or future commencement signal detected.")
        elif "current version as at" in normalized or existing_status.casefold() in {
            "current version",
            "in_force",
            "in force",
        }:
            status = "in_force"
            notes.append("Current-version signal detected.")
        elif "amended" in normalized or "amendment" in normalized:
            status = "amended"
            notes.append("Amendment signal detected without a current-version marker.")
        else:
            status = "unknown"
            notes.append("Lifecycle metadata is incomplete or inconclusive.")

        return LifecycleInfo(
            version_status=status,
            current_version_date=current_version_date,
            last_updated_date=last_updated_date,
            effective_date=effective_date,
            lifecycle_notes=" ".join(notes),
        )

    @staticmethod
    def _value(metadata: dict[str, Any], key: str) -> str:
        return str(metadata.get(key, "") or "").strip()

    @staticmethod
    def _extract_date(text: str, labels: tuple[str, ...]) -> str:
        for label in labels:
            match = re.search(
                rf"\b{re.escape(label)}\b\s*:?\s*({DATE_PATTERN})",
                text,
                flags=re.IGNORECASE,
            )
            if match:
                return re.sub(r"\s+", " ", match.group(1)).strip()
        return ""

    @classmethod
    def _is_future_date(cls, value: str) -> bool:
        parsed = cls._parse_date(value)
        return parsed is not None and parsed > date.today()

    @staticmethod
    def _parse_date(value: str) -> date | None:
        for date_format in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(value, date_format).date()
            except ValueError:
                continue
        return None
