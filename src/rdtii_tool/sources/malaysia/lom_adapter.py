"""Malaysia Federal Legislation Portal (LOM) Zone 1 adapter."""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

import requests

from rdtii_tool.sources.malaysia.catalog import (
    LOM_BASE,
    USER_AGENT,
    clean_text,
    datatables_payload,
    extract_href_links,
    extract_pdf_links_from_record,
    html_text,
    language_from_link,
    pdf_text,
    safe_token,
    short_hash,
    write_json,
    write_jsonl,
)
from rdtii_tool.zone1.models import DocumentRef, DownloadCandidate, DownloadResult, HostPolicy


LOM_COLLECTIONS: dict[str, dict[str, Any]] = {
    "federal_constitution_reprint": {
        "collection": "FederalConstitution",
        "endpoint": "json-reprint-fc-2024.php",
        "referer": "federal-constitution.php",
        "lifecycle_status": "current",
        "active": True,
    },
    "federal_constitution_amendment": {
        "collection": "FederalConstitution",
        "endpoint": "json-amendment-fc-2024.php",
        "referer": "federal-constitution.php",
        "lifecycle_status": "current",
        "active": True,
    },
    "federal_constitution_subsidiary": {
        "collection": "FederalConstitution",
        "endpoint": "json-subsid-fc-2024.php",
        "referer": "federal-constitution.php",
        "lifecycle_status": "current",
        "active": True,
    },
    "principal_updated": {
        "collection": "PrincipalActUpdated",
        "endpoint": "json-updated-2024.php",
        "referer": "principal.php?type=updated",
        "lifecycle_status": "current",
        "active": True,
    },
    "principal_translated": {
        "collection": "PrincipalActTranslated",
        "endpoint": "json-translated-2024.php",
        "referer": "principal.php?type=translated",
        "lifecycle_status": "current",
        "active": True,
    },
    "principal_revised": {
        "collection": "PrincipalActRevised",
        "endpoint": "json-revised-2024.php",
        "referer": "principal.php?type=revised",
        "lifecycle_status": "current",
        "active": True,
    },
    "amendment_act": {
        "collection": "AmendmentAct",
        "endpoint": "json-amendment-2024.php",
        "referer": "principal.php?type=amendment",
        "lifecycle_status": "current",
        "active": True,
    },
    "ordinance": {
        "collection": "Ordinance",
        "endpoint": "json-ordinance-2024.php",
        "referer": "ordinance.php",
        "lifecycle_status": "current",
        "active": True,
    },
    "subsidiary_pua": {
        "collection": "SubsidiaryLegislationPUA",
        "endpoint": "json-subsid-2024.php?type=pua",
        "referer": "subsid.php?type=pua",
        "lifecycle_status": "current",
        "active": True,
    },
    "subsidiary_pub": {
        "collection": "SubsidiaryLegislationPUB",
        "endpoint": "json-subsid-2024.php?type=pub",
        "referer": "subsid.php?type=pub",
        "lifecycle_status": "current",
        "active": True,
    },
    "principal_repealed": {
        "collection": "PrincipalActRepealed",
        "endpoint": "json-repealed-2024.php",
        "referer": "principal.php?type=repealed",
        "lifecycle_status": "repealed",
        "active": False,
    },
}


class MalaysiaLOMAdapter:
    economy = "malaysia"

    def __init__(
        self,
        project_root: Path,
        *,
        source: str = "lom",
        limit: int | None = None,
        include_repealed: bool = False,
        collection_filter: str | None = None,
        document_id: str | None = None,
    ) -> None:
        self.project_root = project_root
        self.limit = limit if limit and limit > 0 else None
        self.include_repealed = include_repealed
        self.collection_filter = collection_filter
        self.document_id_filter = document_id
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json,text/html,*/*"})
        self.catalogue_rows: list[dict[str, Any]] = []
        self.excluded_rows: list[dict[str, Any]] = []
        self.discovery_errors: list[dict[str, Any]] = []
        self._ocr_required: set[str] = set()
        self._source_unavailable_ids = self._load_source_unavailable_ids()

    def discover(self) -> Iterable[DocumentRef]:
        self._write_source_registry()
        yielded: list[DocumentRef] = []
        for key, spec in LOM_COLLECTIONS.items():
            if self.collection_filter and self.collection_filter not in {key, spec["collection"]}:
                continue
            rows = self._fetch_collection(key, spec)
            for record in rows:
                refs = self._record_to_refs(key, spec, record)
                if not refs:
                    self.catalogue_rows.append(self._catalogue_row(key, spec, record, None, included=False, reason="no_download_url"))
                for ref in refs:
                    include = bool(spec["active"]) or self.include_repealed
                    if self.document_id_filter and ref.document_id != self.document_id_filter:
                        include = False
                    if (
                        include
                        and not self.document_id_filter
                        and ref.document_id in self._source_unavailable_ids
                    ):
                        include = False
                        reason = "source_unavailable"
                    else:
                        reason = "" if include else "excluded"
                    row = self._catalogue_row(key, spec, record, ref, included=include, reason=reason)
                    self.catalogue_rows.append(row)
                    if include:
                        yielded.append(ref)
                    else:
                        self.excluded_rows.append(row)
            time.sleep(0.2)
        self._write_catalogue_outputs()
        return yielded

    def resolve_current_version(self, document: DocumentRef) -> DocumentRef:
        return document

    def get_download_candidates(self, document: DocumentRef) -> list[DownloadCandidate]:
        urls = list(document.metadata.get("candidate_download_urls") or [])
        candidates: list[DownloadCandidate] = []
        for index, url in enumerate(urls):
            fmt = "pdf" if str(url).casefold().split("?")[0].endswith(".pdf") else "html"
            candidates.append(
                DownloadCandidate(
                    url=str(url),
                    format=fmt,
                    source_type="malaysia_lom_official_pdf" if fmt == "pdf" else "malaysia_lom_official_html",
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
            text = content.decode("utf-8", errors="replace")
            return document.title.casefold() in text.casefold() or "Malaysia Federal Legislation" in text
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
        norm = self.project_root / "data" / "legal_sources" / "malaysia" / "normalized" / storage_collection / f"{safe_token(document.document_id)}.txt"
        meta = self.project_root / "data" / "legal_sources" / "malaysia" / "metadata" / storage_collection / f"{safe_token(document.document_id)}.json"
        raw_dir = self.project_root / "data" / "legal_sources" / "malaysia" / "raw" / storage_collection
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
                "source": "malaysia_lom",
                "final_error": result.final_error,
                "attempts": result.attempts,
            }
            for result in results
            if not result.success
        ]
        write_jsonl(data_root / "manifests" / "malaysia_lom_catalogue.jsonl", self.catalogue_rows)
        write_jsonl(output_root / "malaysia_lom_catalogue.jsonl", self.catalogue_rows)
        write_jsonl(output_root / "malaysia_lom_excluded_repealed.jsonl", self.excluded_rows)
        write_jsonl(output_root / "malaysia_lom_failed_downloads.jsonl", failures)
        coverage = {
            "source": "malaysia_lom",
            "documents_catalogued": len(self.catalogue_rows),
            "documents_available": len(rows),
            "documents_failed": len(failures),
            "collections": dict(Counter(row["collection"] for row in self.catalogue_rows)),
            "active_download_candidates": len(results),
            "excluded_rows": len(self.excluded_rows),
            "discovery_errors": self.discovery_errors,
            "summary": summary,
        }
        write_json(output_root / "malaysia_lom_download_report.json", coverage)

    def _fetch_collection(self, key: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
        endpoint = str(spec["endpoint"])
        url = LOM_BASE + endpoint
        referer = LOM_BASE + str(spec["referer"])
        rows: list[dict[str, Any]] = []
        start = 0
        page_size = min(self.limit or 500, 500)
        total: int | None = None
        while total is None or start < total:
            if self.limit is not None and len(rows) >= self.limit:
                break
            length = page_size if self.limit is None else min(page_size, self.limit - len(rows))
            payload = datatables_payload(start=start, length=length, columns=12)
            try:
                response = self.session.post(url, data=payload, headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"}, timeout=30)
                data = response.json()
            except Exception as exc:
                self.discovery_errors.append({"collection_key": key, "endpoint": endpoint, "error": str(exc)})
                break
            batch = data.get("records") if isinstance(data, dict) else []
            if not isinstance(batch, list):
                self.discovery_errors.append({"collection_key": key, "endpoint": endpoint, "error": "records_not_list"})
                break
            total = int(data.get("recordsTotal") or len(batch) or 0)
            rows.extend(batch)
            if not batch or len(batch) < length:
                break
            start += len(batch)
            time.sleep(0.2)
        return rows

    def _record_to_refs(self, key: str, spec: dict[str, Any], record: dict[str, Any]) -> list[DocumentRef]:
        pdf_links = extract_pdf_links_from_record(record, base_url=LOM_BASE)
        if not pdf_links and key != "principal_repealed":
            pdf_links = self._detail_pdf_links(record)
        refs: list[DocumentRef] = []
        if not pdf_links and key == "principal_repealed":
            return refs
        for url, label, source_field in pdf_links:
            language = language_from_link(url, source_field, str(record.get("LANGUAGE") or record.get("lang") or ""))
            official_number = self._official_number(record, key)
            title = self._title(record, language)
            instrument_id = self._instrument_id(record, key, official_number)
            version_date = self._version_date(record)
            version_id = safe_token(f"{instrument_id}-{version_date or 'current'}-{language}-{short_hash(url, 8)}")
            document_id = safe_token(f"my-lom-{instrument_id}-{language}-{version_id}")
            canonical_url = self._canonical_url(record, key, language, official_number)
            if key.startswith("subsidiary_"):
                canonical_url = f"{LOM_BASE}act-view.php?type={'pua' if key.endswith('pua') else 'pub'}&language=BI&no={official_number}"
            metadata = {
                "source": "malaysia_lom",
                "authority": "Attorney General's Chambers of Malaysia",
                "source_portal": "Malaysia Federal Legislation Portal",
                "official": True,
                "storage_collection": safe_token(str(spec["collection"])),
                "instrument_type": str(spec["collection"]),
                "document_type": str(spec["collection"]),
                "instrument_id": instrument_id,
                "official_number": official_number,
                "year": self._year(record, official_number),
                "language": language,
                "lifecycle_status": spec["lifecycle_status"],
                "publication_date": self._publication_date(record),
                "effective_date": self._effective_date(record),
                "catalogue_url": LOM_BASE + str(spec["referer"]),
                "candidate_download_urls": [url],
                "source_format": "pdf",
                "collection_key": key,
                "raw_catalogue_record": record,
            }
            refs.append(
                DocumentRef(
                    economy="malaysia",
                    document_id=document_id,
                    collection=str(spec["collection"]),
                    title=title or official_number or document_id,
                    canonical_url=canonical_url,
                    version_id=version_id,
                    status=str(spec["lifecycle_status"]),
                    metadata=metadata,
                )
            )
        return refs

    def _detail_pdf_links(self, record: dict[str, Any]) -> list[tuple[str, str, str]]:
        detail_urls: list[str] = []
        for value in record.values():
            if isinstance(value, str) and "act-detail.php" in value:
                for url, _ in extract_href_links(value, base_url=LOM_BASE):
                    if "act-detail.php" in url and url not in detail_urls:
                        detail_urls.append(url)
        links: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for detail_url in detail_urls[:2]:
            try:
                response = self.session.get(detail_url, timeout=30, headers={"Referer": LOM_BASE})
            except Exception as exc:
                self.discovery_errors.append({"url": detail_url, "error": str(exc), "stage": "detail_pdf_links"})
                continue
            text = response.text
            # LOM embeds the PDF viewer URL in iframe data-src attributes.
            for match in re.finditer(r"file=([^\"&]+\\.pdf)", text, re.I):
                url = match.group(1)
                if url not in seen:
                    seen.add(url)
                    links.append((self._absolute_lom_url(url), "detail_pdf", "detail_iframe"))
            for url, label in extract_href_links(text, base_url=detail_url):
                if ".pdf" in url.casefold() and url not in seen:
                    seen.add(url)
                    links.append((url, label, "detail_link"))
        return links

    @staticmethod
    def _absolute_lom_url(value: str) -> str:
        return urljoin(LOM_BASE, value.replace("../", ""))

    def _catalogue_row(self, key: str, spec: dict[str, Any], record: dict[str, Any], ref: DocumentRef | None, *, included: bool, reason: str) -> dict[str, Any]:
        official_number = self._official_number(record, key)
        return {
            "economy": "malaysia",
            "source": "malaysia_lom",
            "collection": spec["collection"],
            "collection_key": key,
            "document_id": ref.document_id if ref else "",
            "instrument_id": ref.metadata.get("instrument_id") if ref else self._instrument_id(record, key, official_number),
            "official_number": official_number,
            "title": ref.title if ref else self._title(record, "en"),
            "language": ref.metadata.get("language") if ref else "",
            "lifecycle_status": spec["lifecycle_status"],
            "included_in_active_download": included,
            "exclusion_reason": reason,
            "catalogue_url": LOM_BASE + str(spec["referer"]),
            "canonical_url": ref.canonical_url if ref else "",
            "candidate_download_urls": ref.metadata.get("candidate_download_urls") if ref else [],
            "raw_catalogue_record": record,
        }

    def _write_source_registry(self) -> None:
        root = self.project_root / "data" / "legal_sources" / "malaysia"
        registry = {
            "economy": "Malaysia",
            "country_code": "MY",
            "sources": [
                {
                    "portal_name": "Malaysia Federal Legislation Portal",
                    "portal_code": "malaysia_lom",
                    "authority": "Attorney General's Chambers of Malaysia",
                    "base_url": LOM_BASE,
                    "official": True,
                    "source_type": "official_legislation_portal",
                    "collections": list(LOM_COLLECTIONS),
                },
                {
                    "portal_name": "Personal Data Protection Department",
                    "portal_code": "malaysia_pdp",
                    "authority": "Department of Personal Data Protection",
                    "base_url": "https://www.pdp.gov.my/ppdpv1/en/akta709/",
                    "official": True,
                    "source_type": "official_regulatory_portal",
                },
                {
                    "portal_name": "Bank Negara Malaysia",
                    "portal_code": "bnm",
                    "official": True,
                    "source_type": "regulatory_supplement",
                    "status": "pending_stable_catalogue",
                },
                {
                    "portal_name": "Securities Commission Malaysia",
                    "portal_code": "sc_my",
                    "official": True,
                    "source_type": "regulatory_supplement",
                    "status": "pending_stable_catalogue",
                },
                {
                    "portal_name": "Malaysian Communications and Multimedia Commission",
                    "portal_code": "mcmc",
                    "official": True,
                    "source_type": "regulatory_supplement",
                    "status": "pending_stable_catalogue",
                },
            ],
        }
        write_json(root / "source_registry.json", registry)
        report = (
            "# Malaysia Source Gap Report\n\n"
            "The first Malaysia Zone 1 implementation includes LOM and PDP official sources. "
            "BNM, Securities Commission Malaysia, and MCMC are registered as pending because no stable "
            "complete official catalogue has been implemented in this pass. They must not be treated as complete.\n"
        )
        out = self.project_root / "outputs" / "corpus" / "malaysia"
        out.mkdir(parents=True, exist_ok=True)
        (out / "malaysia_source_gap_report.md").write_text(report, encoding="utf-8")

    def _write_catalogue_outputs(self) -> None:
        root = self.project_root / "data" / "legal_sources" / "malaysia"
        out = self.project_root / "outputs" / "corpus" / "malaysia"
        write_jsonl(root / "manifests" / "malaysia_lom_catalogue.jsonl", self.catalogue_rows)
        write_jsonl(out / "malaysia_lom_catalogue.jsonl", self.catalogue_rows)

    def _load_source_unavailable_ids(self) -> set[str]:
        rows: list[dict[str, Any]] = []
        for path in (
            self.project_root / "data" / "legal_sources" / "malaysia" / "manifests" / "malaysia_source_unavailable.jsonl",
            self.project_root / "outputs" / "corpus" / "malaysia" / "malaysia_source_unavailable.jsonl",
        ):
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(payload.get("retry_policy") or "") == "future_refresh_only":
                    rows.append(payload)
        return {str(row.get("document_id")) for row in rows if row.get("document_id")}

    @staticmethod
    def _official_number(record: dict[str, Any], key: str) -> str:
        for field in ("lgt_act_no", "lgt_act_id", "ACTNO_LEGISLATION", "noPU", "NOMBOR_ORDINAN", "NO_ORDINAN", "ILA_ACT_NO"):
            value = clean_text(record.get(field))
            if value:
                return value
        return safe_token(json.dumps(record, ensure_ascii=False))[:24]

    @staticmethod
    def _instrument_id(record: dict[str, Any], key: str, official_number: str) -> str:
        if key.startswith("subsidiary_"):
            return safe_token(official_number)
        prefix = {
            "amendment_act": "amendment",
            "ordinance": "ordinance",
            "federal_constitution_reprint": "federal-constitution",
            "federal_constitution_amendment": "federal-constitution-amendment",
            "federal_constitution_subsidiary": "federal-constitution-subsidiary",
        }.get(key, "act")
        return safe_token(f"{prefix}-{official_number}")

    @staticmethod
    def _title(record: dict[str, Any], language: str) -> str:
        fields = ["titlebi", "TajukBI", "TITLEBI", "LEGISLATIONTITLEBI", "titleBI", "LRA_BI"]
        if language == "ms":
            fields = ["titlebm", "TajukBM", "TITLEBM", "LEGISLATIONTITLEBM", "titleBM", "LRA_BM", *fields]
        else:
            fields = [*fields, "titlebm", "TajukBM", "TITLEBM", "titleBM"]
        for field in fields:
            value = clean_text(record.get(field))
            if value:
                return value
        for field in ("TAJUK_ORDINAN", "PSKey"):
            value = clean_text(record.get(field))
            if value:
                return value
        return ""

    @staticmethod
    def _version_date(record: dict[str, Any]) -> str:
        for field in ("lgt_timeline_date", "publicationDate", "PUBLICATIONDATE", "PUBLICATIONDATE_X", "ROYALASSENTDATE", "commencementDate", "COMMENCEMENTDATE"):
            value = clean_text(record.get(field))
            if value:
                return value
        return "current"

    @staticmethod
    def _publication_date(record: dict[str, Any]) -> str:
        for field in ("publicationDate", "PUBLICATIONDATE", "PUBLICATIONDATE_X"):
            value = clean_text(record.get(field))
            if value:
                return value
        return ""

    @staticmethod
    def _effective_date(record: dict[str, Any]) -> str:
        for field in ("commencementDate", "COMMENCEMENTDATE", "TARIKH_KUAT_KUASA"):
            value = clean_text(record.get(field))
            if value:
                return value
        return ""

    @staticmethod
    def _year(record: dict[str, Any], official_number: str) -> str:
        text = " ".join(clean_text(record.get(field)) for field in ("publicationDate", "PUBLICATIONDATE", "titleBI", "TajukBI"))
        match = re.search(r"(19|20)\d{2}", f"{official_number} {text}")
        return match.group(0) if match else ""

    @staticmethod
    def _canonical_url(record: dict[str, Any], key: str, language: str, official_number: str) -> str:
        lang = "BM" if language == "ms" else "BI"
        if key.startswith("federal_constitution"):
            return f"{LOM_BASE}federal-constitution.php?language={lang}"
        if key == "amendment_act":
            return f"{LOM_BASE}act-detail.php?language={lang}&type=amendment&act={official_number}"
        if key == "ordinance":
            return f"{LOM_BASE}act-detail.php?language={lang}&status=ordinance&act={official_number}"
        return f"{LOM_BASE}act-detail.php?language={lang}&act={official_number}"
