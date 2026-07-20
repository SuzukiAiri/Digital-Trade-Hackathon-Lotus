"""Persistent, allowlisted store for discovered official URLs."""

from __future__ import annotations

import json
import posixpath
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rdtii_tool.document_models import CandidateURL


TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_url(url: str) -> str:
    """Normalize an HTTP(S) URL for official-domain deduplication."""
    parsed = urlsplit(url.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Candidate URL must be absolute HTTP(S): {url!r}")

    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname.casefold()
    port = parsed.port
    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    raw_path = parsed.path or "/"
    normalized_path = posixpath.normpath(raw_path)
    if raw_path.endswith("/") and not normalized_path.endswith("/"):
        normalized_path += "/"
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"

    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        query_items.append((key, value))
    query = urlencode(sorted(query_items))
    return urlunsplit((scheme, netloc, normalized_path, query, ""))


class CandidateURLStore:
    """Deduplicates discovered URLs and persists them as JSONL."""

    def __init__(
        self,
        path: str | Path,
        *,
        official_domains: Iterable[str],
    ) -> None:
        self.path = Path(path)
        self.official_domains = {
            domain.casefold().lstrip(".")
            for domain in official_domains
            if domain
        }
        self._candidates: dict[str, CandidateURL] = {}

    @property
    def candidates(self) -> list[CandidateURL]:
        return list(self._candidates.values())

    def add(self, candidate: CandidateURL) -> CandidateURL:
        normalized = normalize_url(candidate.normalized_url or candidate.url)
        host = (urlsplit(normalized).hostname or "").casefold()
        if not self._is_allowed_domain(host):
            raise ValueError(f"Candidate domain is not allowlisted: {host}")

        now = utc_now()
        candidate.normalized_url = normalized
        candidate.first_seen_at = candidate.first_seen_at or now
        candidate.last_seen_at = now

        existing = self._candidates.get(normalized)
        if existing is None:
            self._candidates[normalized] = candidate
            return candidate

        existing.last_seen_at = now
        existing.title = existing.title or candidate.title
        existing.source_name = existing.source_name or candidate.source_name
        existing.source_type = existing.source_type or candidate.source_type
        existing.document_type = (
            existing.document_type or candidate.document_type
        )
        existing.country = existing.country or candidate.country
        existing.discovered_by = self._merge_discovered_by(
            existing.discovered_by,
            candidate.discovered_by,
        )
        existing.metadata = self._merge_metadata(
            existing.metadata,
            candidate.metadata,
        )
        return existing

    def add_many(self, candidates: Iterable[CandidateURL]) -> list[CandidateURL]:
        added = []
        for candidate in candidates:
            try:
                added.append(self.add(candidate))
            except ValueError:
                continue
        return added

    def write(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            for candidate in self.candidates:
                handle.write(
                    json.dumps(
                        candidate.to_json_dict(),
                        ensure_ascii=False,
                        sort_keys=False,
                    )
                )
                handle.write("\n")
        return self.path

    def retain_first(self, limit: int) -> list[CandidateURL]:
        retained = self.candidates[:limit]
        self._candidates = {
            candidate.normalized_url: candidate for candidate in retained
        }
        return retained

    def retain(self, candidates: Iterable[CandidateURL]) -> list[CandidateURL]:
        retained = list(candidates)
        self._candidates = {
            candidate.normalized_url: candidate for candidate in retained
        }
        return retained

    def load(self) -> list[CandidateURL]:
        self._candidates.clear()
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                    self.add(CandidateURL.from_json_dict(payload))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"Invalid candidate JSONL at line {line_number}"
                    ) from exc
        return self.candidates

    def update_from_download(
        self,
        normalized_url: str,
        *,
        status: str,
        http_status: int | None,
        error: str,
        error_type: str,
    ) -> None:
        candidate = self._candidates.get(normalized_url)
        if candidate is None:
            return
        status_map = {
            "success": "downloaded",
            "cache_hit": "cache_hit",
            "blocked": "blocked",
            "failed": "failed",
        }
        candidate.download_status = status_map.get(status, "failed")
        candidate.http_status = http_status
        candidate.error = error
        candidate.error_type = error_type
        candidate.last_seen_at = utc_now()

    def _is_allowed_domain(self, host: str) -> bool:
        return any(
            host == domain or host.endswith(f".{domain}")
            for domain in self.official_domains
        )

    @staticmethod
    def _merge_discovered_by(left: str, right: str) -> str:
        methods = {
            item.strip()
            for value in (left, right)
            for item in value.split(",")
            if item.strip()
        }
        return ",".join(sorted(methods))

    @classmethod
    def _merge_metadata(
        cls,
        left: dict[str, Any],
        right: dict[str, Any],
    ) -> dict[str, Any]:
        merged = dict(left)
        for key, value in right.items():
            if key not in merged or merged[key] in (None, "", [], {}):
                merged[key] = value
            elif isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = cls._merge_metadata(merged[key], value)
            elif isinstance(merged[key], list) and isinstance(value, list):
                merged[key] = list(dict.fromkeys([*merged[key], *value]))
        return merged
