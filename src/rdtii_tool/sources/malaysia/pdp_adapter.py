"""Malaysia PDP official regulatory corpus Zone 1 adapter."""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from rdtii_tool.sources.malaysia.catalog import (
    PDP_BASE,
    USER_AGENT,
    clean_text,
    extract_href_links,
    html_text,
    pdf_text,
    safe_token,
    short_hash,
    write_json,
    write_jsonl,
)
from rdtii_tool.zone1.models import DocumentRef, DownloadCandidate, DownloadResult, HostPolicy


EXCLUDED_TITLE_TERMS = {
    "public consultation",
    "consultation paper",
    "faq",
    "frequently asked",
    "complaint form",
    "registration form",
}


class MalaysiaPDPAdapter:
    economy = "malaysia"

    def __init__(
        self,
        project_root: Path,
        *,
        limit: int | None = None,
        collection_filter: str | None = None,
        document_id: str | None = None,
    ) -> None:
        self.project_root = project_root
        self.limit = limit if limit and limit > 0 else None
        self.collection_filter = collection_filter
        self.document_id_filter = document_id
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,*/*"})
        self.catalogue_rows: list[dict[str, Any]] = []
        self.excluded_rows: list[dict[str, Any]] = []
        self.discovery_errors: list[dict[str, Any]] = []
        self._ocr_required: set[str] = set()

    def discover(self) -> Iterable[DocumentRef]:
        refs: list[DocumentRef] = []
        for post in self._archive_posts():
            category, binding = self._classify(post["title"], post["url"])
            if self.collection_filter and self.collection_filter not in {category, binding, "pdp"}:
                continue
            excluded_reason = self._exclude_reason(post["title"], post["url"])
            if excluded_reason:
                row = {**post, "source": "malaysia_pdp", "collection": category, "binding_status": binding, "included_in_active_download": False, "exclusion_reason": excluded_reason}
                self.catalogue_rows.append(row)
                self.excluded_rows.append(row)
                continue
            detail = self._post_detail(post)
            pdf_links = detail["pdf_links"]
            if not pdf_links:
                pdf_links = [(post["url"], "html", "post_html")]
            for index, (url, label, source_field) in enumerate(pdf_links):
                fmt = "pdf" if urlparse(url).path.casefold().endswith(".pdf") else "html"
                language = self._language(post["title"], url)
                version_id = safe_token(f"{post['slug']}-{language}-{short_hash(url, 8)}")
                document_id = safe_token(f"my-pdp-{post['slug']}-{language}-{index}-{short_hash(url, 8)}")
                metadata = {
                    "source": "malaysia_pdp",
                    "authority": "Department of Personal Data Protection",
                    "source_portal": "Personal Data Protection Department",
                    "official": True,
                    "storage_collection": safe_token(category),
                    "instrument_type": category,
                    "document_type": category,
                    "instrument_id": post["slug"],
                    "official_number": self._official_number(post["title"]),
                    "year": self._year(post["title"], url),
                    "language": language,
                    "lifecycle_status": "current",
                    "publication_date": detail.get("publication_date", ""),
                    "effective_date": detail.get("effective_date", ""),
                    "catalogue_url": post.get("archive_url", PDP_BASE),
                    "candidate_download_urls": [url],
                    "source_format": fmt,
                    "binding_status": binding,
                    "sector_coverage": self._sector(post["title"]),
                    "statutory_authority": detail.get("statutory_authority", ""),
                    "post_url": post["url"],
                    "post_title": post["title"],
                }
                ref = DocumentRef(
                    economy="malaysia",
                    document_id=document_id,
                    collection=category,
                    title=post["title"],
                    canonical_url=post["url"],
                    version_id=version_id,
                    status="current",
                    metadata=metadata,
                )
                include = not self.document_id_filter or ref.document_id == self.document_id_filter
                row = {
                    **post,
                    "source": "malaysia_pdp",
                    "collection": category,
                    "binding_status": binding,
                    "document_id": document_id,
                    "language": language,
                    "candidate_download_urls": [url],
                    "included_in_active_download": include,
                    "exclusion_reason": "" if include else "filtered",
                }
                self.catalogue_rows.append(row)
                if include:
                    refs.append(ref)
            time.sleep(0.1)
        self._write_catalogue_outputs()
        return refs

    def resolve_current_version(self, document: DocumentRef) -> DocumentRef:
        return document

    def get_download_candidates(self, document: DocumentRef) -> list[DownloadCandidate]:
        candidates: list[DownloadCandidate] = []
        for index, url in enumerate(document.metadata.get("candidate_download_urls") or []):
            fmt = "pdf" if str(url).casefold().split("?")[0].endswith(".pdf") else "html"
            candidates.append(
                DownloadCandidate(
                    url=str(url),
                    format=fmt,
                    source_type="malaysia_pdp_official_pdf" if fmt == "pdf" else "malaysia_pdp_official_html",
                    priority=index,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,text/html,*/*", "Referer": document.canonical_url},
                    required=True,
                    metadata={"extension": ".pdf" if fmt == "pdf" else ".html", "filename": f"{safe_token(document.document_id)}.{fmt}"},
                )
            )
        return candidates

    def validate_response(self, document: DocumentRef, candidate: DownloadCandidate, response: Any) -> bool:
        content = bytes(response.content)
        if candidate.format == "pdf":
            return content.startswith(b"%PDF") and len(content) > 500
        if candidate.format == "html":
            return "Personal Data Protection" in content.decode("utf-8", errors="replace") or document.title.casefold() in content.decode("utf-8", errors="replace").casefold()
        return bool(content)

    def normalize_response(self, document: DocumentRef, candidate: DownloadCandidate, content: bytes) -> str:
        if candidate.format == "pdf":
            text, requires_ocr = pdf_text(content)
            if requires_ocr:
                self._ocr_required.add(document.document_id)
                return f"{document.title}\n\n[OCR_REQUIRED] Official PDF was preserved but no embedded text layer was detected."
            return text
        return html_text(content)

    def normalize_file(self, document: DocumentRef, path: Path) -> str:
        if path.suffix.casefold() == ".pdf":
            text, requires_ocr = pdf_text(path.read_bytes())
            if requires_ocr:
                self._ocr_required.add(document.document_id)
                return f"{document.title}\n\n[OCR_REQUIRED] Official PDF was preserved but no embedded text layer was detected."
            return text
        return html_text(path.read_bytes())

    def existing_output_result(self, document: DocumentRef) -> DownloadResult | None:
        storage_collection = safe_token(str(document.metadata.get("storage_collection") or document.collection))
        base = self.project_root / "data" / "legal_sources" / "malaysia"
        norm = base / "normalized" / storage_collection / f"{safe_token(document.document_id)}.txt"
        meta = base / "metadata" / storage_collection / f"{safe_token(document.document_id)}.json"
        raw_dir = base / "raw" / storage_collection
        raw = next((p for p in raw_dir.glob(f"{safe_token(document.document_id)}.*") if p.is_file() and p.stat().st_size > 0), None) if raw_dir.exists() else None
        if not (norm.exists() and norm.stat().st_size > 0 and meta.exists() and meta.stat().st_size > 0 and raw):
            return None
        try:
            metadata = json.loads(meta.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            metadata = dict(document.metadata)
        metadata.update({"download_status": "cache_hit", "parse_status": "success"})
        return DownloadResult(
            document_id=document.document_id,
            success=True,
            selected_candidate=None,
            raw_path=str(raw),
            normalized_path=str(norm),
            attempts=[],
            final_error=None,
            status="existing_normalized",
            document=document,
            metadata=metadata,
        )

    def materialize_output(self, document: DocumentRef, metadata: dict[str, Any], text: str) -> None:
        metadata["requires_ocr"] = document.document_id in self._ocr_required

    def host_policy(self) -> HostPolicy:
        return HostPolicy(max_concurrency=2, retryable_statuses={429, 500, 502, 503, 504}, shared_backoff=True, backoff_seconds=2)

    def write_compat_outputs(self, results: list[DownloadResult], summary: dict[str, Any]) -> None:
        output_root = self.project_root / "outputs" / "corpus" / "malaysia"
        data_root = self.project_root / "data" / "legal_sources" / "malaysia"
        rows = [dict(result.metadata) for result in results if result.success]
        failures = [
            {
                "document_id": result.document_id,
                "collection": result.document.collection,
                "title": result.document.title,
                "source": "malaysia_pdp",
                "final_error": result.final_error,
                "attempts": result.attempts,
            }
            for result in results
            if not result.success
        ]
        write_jsonl(data_root / "manifests" / "malaysia_pdp_catalogue.jsonl", self.catalogue_rows)
        write_jsonl(output_root / "malaysia_pdp_catalogue.jsonl", self.catalogue_rows)
        write_jsonl(output_root / "malaysia_pdp_excluded.jsonl", self.excluded_rows)
        write_jsonl(output_root / "malaysia_pdp_failed_downloads.jsonl", failures)
        coverage = {
            "source": "malaysia_pdp",
            "documents_catalogued": len(self.catalogue_rows),
            "documents_available": len(rows),
            "documents_failed": len(failures),
            "collections": dict(Counter(row["collection"] for row in self.catalogue_rows)),
            "binding_status": dict(Counter(row["binding_status"] for row in self.catalogue_rows if row.get("binding_status"))),
            "excluded_rows": len(self.excluded_rows),
            "discovery_errors": self.discovery_errors,
            "summary": summary,
        }
        write_json(output_root / "malaysia_pdp_download_report.json", coverage)

    def _archive_posts(self) -> list[dict[str, Any]]:
        posts: list[dict[str, Any]] = []
        seen: set[str] = set()
        page_url = PDP_BASE
        archive_index = 1
        while page_url:
            try:
                response = self.session.get(page_url, timeout=30)
            except Exception as exc:
                self.discovery_errors.append({"url": page_url, "error": str(exc)})
                break
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                href = urljoin(page_url, anchor["href"])
                if "/en/akta/" not in href:
                    continue
                title = clean_text(anchor.get_text(" ", strip=True))
                if not title or title.casefold() in {"download", "relevant documents"}:
                    continue
                slug = urlparse(href).path.rstrip("/").split("/")[-1]
                if href in seen:
                    continue
                seen.add(href)
                posts.append({"title": title, "url": href, "slug": slug, "archive_url": page_url})
                if self.limit is not None and len(posts) >= self.limit:
                    return posts
            next_link = soup.find("link", rel=lambda value: value and "next" in value)
            if next_link and next_link.get("href"):
                page_url = urljoin(page_url, str(next_link["href"]))
                archive_index += 1
                if archive_index > 20:
                    break
                time.sleep(0.2)
            else:
                break
        return posts

    def _post_detail(self, post: dict[str, str]) -> dict[str, Any]:
        try:
            response = self.session.get(post["url"], timeout=30)
        except Exception as exc:
            self.discovery_errors.append({"url": post["url"], "error": str(exc)})
            return {"pdf_links": []}
        links: list[tuple[str, str, str]] = []
        for url, label in extract_href_links(response.text, base_url=post["url"]):
            if ".pdf" in url.casefold() and "/wp-content/uploads/" in url:
                links.append((url, label, "post_link"))
        return {"pdf_links": links, "publication_date": "", "effective_date": "", "statutory_authority": ""}

    @staticmethod
    def _exclude_reason(title: str, url: str) -> str:
        folded = f"{title} {url}".casefold()
        for term in EXCLUDED_TITLE_TERMS:
            if term in folded:
                return term
        return ""

    @staticmethod
    def _classify(title: str, url: str) -> tuple[str, str]:
        folded = f"{title} {url}".casefold()
        if "circular" in folded or "pekeliling" in folded:
            return "PDPCircular", "circular"
        if "standard" in folded:
            return "PDPStandard", "standard"
        if "guideline" in folded or "guidance" in folded or "quick guide" in folded:
            return "PDPGuideline", "guideline"
        if "code of practice" in folded or "practice" in folded:
            return "PDPCodeOfPractice", "registered_code"
        if "regulation" in folded or "order" in folded or "appointment" in folded or "p.u." in folded or "commencement" in folded:
            return "PDPSubsidiaryLegislation", "subsidiary_legislation"
        if "act" in folded or "akta" in folded:
            return "PDPAct", "legislation"
        return "PDPOtherInstrument", "unknown"

    @staticmethod
    def _language(title: str, url: str) -> str:
        folded = f"{title} {url}".casefold()
        if "-en" in folded or "english" in folded:
            return "en"
        if "akta" in folded or "malay" in folded:
            return "ms"
        return "bilingual"

    @staticmethod
    def _official_number(title: str) -> str:
        match = re.search(r"(Act|Akta)\s+([A-Z]?\d+)", title, re.I)
        if match:
            return f"{match.group(1)} {match.group(2)}"
        match = re.search(r"\b(A\d{3,5}|P\\.U\\.\\s*\\([AB]\\)\\s*\\d+/?\\d*)\b", title, re.I)
        return match.group(1) if match else ""

    @staticmethod
    def _year(title: str, url: str) -> str:
        match = re.search(r"(19|20)\d{2}", f"{title} {url}")
        return match.group(0) if match else ""

    @staticmethod
    def _sector(title: str) -> str:
        folded = title.casefold()
        for sector in ("banking", "financial", "communications", "utilities", "electricity", "water", "healthcare", "aviation", "insurance", "takaful"):
            if sector in folded:
                return sector
        return "general"

    def _write_catalogue_outputs(self) -> None:
        root = self.project_root / "data" / "legal_sources" / "malaysia"
        out = self.project_root / "outputs" / "corpus" / "malaysia"
        write_jsonl(root / "manifests" / "malaysia_pdp_catalogue.jsonl", self.catalogue_rows)
        write_jsonl(out / "malaysia_pdp_catalogue.jsonl", self.catalogue_rows)

