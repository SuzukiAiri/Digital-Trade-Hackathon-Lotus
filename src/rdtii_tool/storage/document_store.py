"""SSO HTML storage, download logs, and acquisition summaries."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from rdtii_tool.cache import safe_filename
from rdtii_tool.document_models import CandidateURL, DownloadResult


SUCCESS_STATUSES = {"success", "cache_hit"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DocumentStore:
    """Own acquired SSO HTML and acquisition run metadata."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)
        self.html_dir = self.output_dir / "documents" / "html"
        self.download_log_path = self.output_dir / "download_log.jsonl"
        self.summary_path = self.output_dir / "crawl_summary.json"

    def path_for(self, candidate: CandidateURL) -> Path:
        title = (
            str(candidate.metadata.get("registry_key", "")).strip()
            or candidate.title
            or "document"
        )
        digest = hashlib.sha256(
            (candidate.normalized_url or candidate.url).encode("utf-8")
        ).hexdigest()[:12]
        filename = safe_filename(f"{title}_{digest}", suffix=".html")
        path = self.html_dir / filename
        resolved_directory = self.html_dir.resolve()
        resolved_path = path.resolve()
        if resolved_directory not in resolved_path.parents:
            raise ValueError("Document path escaped the output directory")
        return path

    def save_html(self, candidate: CandidateURL, content: bytes) -> Path:
        path = self.path_for(candidate)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def find_cached(self, candidate: CandidateURL) -> Path | None:
        path = self.path_for(candidate)
        return path if path.exists() else None

    def write_download_log(
        self,
        results: Iterable[DownloadResult],
    ) -> Path:
        self.download_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.download_log_path.open("w", encoding="utf-8") as handle:
            for result in results:
                handle.write(
                    json.dumps(result.to_json_dict(), ensure_ascii=False)
                )
                handle.write("\n")
        return self.download_log_path

    def write_summary(
        self,
        *,
        country: str,
        sources_checked: Iterable[str],
        discovery_methods_used: Iterable[str],
        candidates_discovered: int,
        results: Iterable[DownloadResult] = (),
        started_at: str,
        finished_at: str | None = None,
    ) -> Path:
        materialized = list(results)
        successful = [
            result
            for result in materialized
            if result.status in SUCCESS_STATUSES
        ]
        summary = {
            "country": country.upper(),
            "sources_checked": sorted(set(sources_checked)),
            "discovery_methods_used": sorted(set(discovery_methods_used)),
            "candidates_discovered": candidates_discovered,
            "documents_downloaded": len(successful),
            "direct_sso_html_downloaded": sum(
                result.status in SUCCESS_STATUSES
                and result.content_validation_passed
                for result in materialized
            ),
            "blocked": sum(
                result.status == "blocked" for result in materialized
            ),
            "failed": sum(
                result.status == "failed" for result in materialized
            ),
            "cache_hits": sum(
                result.status == "cache_hit" for result in materialized
            ),
            "started_at": started_at,
            "finished_at": finished_at or utc_now(),
        }
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return self.summary_path
