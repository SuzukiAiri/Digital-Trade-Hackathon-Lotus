"""Australia Commonwealth Zone 1 corpus discovery and download.

This module is intentionally limited to source acquisition.  It does not call
RDTII mapping, mapper, reviewer, aggregation, or indicator logic.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import threading
import time
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from pypdf import PdfReader

from rdtii_tool.config_loader import PROJECT_ROOT


FRL_BASE = "https://www.legislation.gov.au"
FRL_API = "https://api.prod.legislation.gov.au/v1"
USER_AGENT = "RDTII-Australia-Zone1/0.1 (official-document downloader; contact: research use)"
MIN_HTML_BYTES = 800
MIN_PDF_BYTES = 800
FRL_REQUEST_TIMEOUT = (10, 30)
FRL_DOWNLOAD_WORKERS = 8
FRL_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


CORE_SEEDS: list[dict[str, str]] = [
    {"title": "Privacy Act 1988", "register_id": "C2004A03712", "url": "https://www.legislation.gov.au/C2004A03712/latest", "authorises": "true"},
    {"title": "My Health Records Act 2012", "register_id": "C2012A00063", "url": "https://www.legislation.gov.au/C2012A00063/latest", "authorises": "true"},
    {"title": "Telecommunications (Interception and Access) Act 1979", "register_id": "C2004A02124", "url": "https://www.legislation.gov.au/C2004A02124/latest", "authorises": "true"},
    {"title": "Telecommunications Act 1997", "register_id": "", "url": "", "authorises": "true"},
    {"title": "Telecommunications Regulations 2021", "register_id": "F2021L00289", "url": "https://www.legislation.gov.au/F2021L00289/latest", "authorises": "false"},
    {"title": "Australian Security Intelligence Organisation Act 1979", "register_id": "C2004A02123", "url": "https://www.legislation.gov.au/C2004A02123/latest", "authorises": "true"},
    {"title": "Security of Critical Infrastructure Act 2018", "register_id": "C2018A00029", "url": "https://www.legislation.gov.au/C2018A00029/latest", "authorises": "true"},
    {"title": "Cyber Security Act 2024", "register_id": "C2024A00098", "url": "https://www.legislation.gov.au/C2024A00098/latest", "authorises": "true"},
    {"title": "Cyber Security (Ransomware Payment Reporting) Rules 2025", "register_id": "F2025L00278", "url": "https://www.legislation.gov.au/F2025L00278/latest", "authorises": "false"},
    {"title": "Data Availability and Transparency Act 2022", "register_id": "C2022A00011", "url": "https://www.legislation.gov.au/C2022A00011/latest", "authorises": "true"},
    {"title": "Competition and Consumer Act 2010", "register_id": "C2004A00109", "url": "https://www.legislation.gov.au/C2004A00109/latest", "authorises": "true"},
    {"title": "Competition and Consumer (Consumer Data Right) Rules 2020", "register_id": "F2020L00094", "url": "https://www.legislation.gov.au/F2020L00094/latest", "authorises": "false"},
    {"title": "Corporations Act 2001", "register_id": "C2004A00818", "url": "https://www.legislation.gov.au/C2004A00818/latest", "authorises": "true"},
    {"title": "Anti-Money Laundering and Counter-Terrorism Financing Act 2006", "register_id": "C2006A00169", "url": "https://www.legislation.gov.au/C2006A00169/latest", "authorises": "true"},
    {"title": "Surveillance Devices Act 2004", "register_id": "", "url": "", "authorises": "true"},
    {"title": "Digital ID Act 2024", "register_id": "", "url": "", "authorises": "true"},
    {"title": "Online Safety Act 2021", "register_id": "C2021A00076", "url": "https://www.legislation.gov.au/C2021A00076/latest", "authorises": "true"},
]

OAIC_SOURCES = [
    "https://www.oaic.gov.au/privacy/australian-privacy-principles",
    "https://www.oaic.gov.au/privacy/australian-privacy-principles/australian-privacy-principles-guidelines",
    "https://www.oaic.gov.au/privacy/australian-privacy-principles/australian-privacy-principles-guidelines/chapter-8-app-8-cross-border-disclosure-of-personal-information",
    "https://www.oaic.gov.au/privacy/guidance-and-advice/privacy-management-framework-enabling-compliance-and-encouraging-good-practice",
]

CYBER_STRATEGY_URL = "https://www.homeaffairs.gov.au/about-us/our-portfolios/cyber-security/strategy/2023-2030-australian-cyber-security-strategy"

DFAT_TREATY_SEEDS = [
    {"agreement": "CPTPP", "official_status_source": "https://www.dfat.gov.au/trade/agreements/in-force/cptpp/comprehensive-and-progressive-agreement-for-trans-pacific-partnership"},
    {"agreement": "RCEP", "official_status_source": "https://www.dfat.gov.au/trade/agreements/in-force/rcep"},
    {"agreement": "Australia-Singapore Digital Economy Agreement", "official_status_source": "https://www.dfat.gov.au/trade/services-and-digital-trade/australia-and-singapore-digital-economy-agreement"},
    {"agreement": "Singapore-Australia Free Trade Agreement", "official_status_source": "https://www.dfat.gov.au/trade/agreements/in-force/safta"},
    {"agreement": "Australia-United Kingdom Free Trade Agreement", "official_status_source": "https://www.dfat.gov.au/trade/agreements/in-force/aukfta"},
    {"agreement": "Australia-Hong Kong Free Trade Agreement", "official_status_source": "https://www.dfat.gov.au/trade/agreements/in-force/a-hkfta"},
    {"agreement": "Indonesia-Australia Comprehensive Economic Partnership Agreement", "official_status_source": "https://www.dfat.gov.au/trade/agreements/in-force/iacepa"},
    {"agreement": "Peru-Australia Free Trade Agreement", "official_status_source": "https://www.dfat.gov.au/trade/agreements/in-force/pafta"},
]

STATE_TERRITORY_SOURCES = [
    "New South Wales", "Victoria", "Queensland", "Western Australia",
    "South Australia", "Tasmania", "Australian Capital Territory", "Northern Territory",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def normalise_text(value: str) -> str:
    value = html.unescape(value)
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\u00a0", " ")
    value = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2212]", "-", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def slug(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return value[:90] or "document"


def classify_document_type(collection: str, register_id: str, title: str) -> str:
    if collection == "Act" or register_id.startswith("C"):
        return "act"
    lowered = title.casefold()
    if "regulation" in lowered:
        return "regulation"
    if "rule" in lowered:
        return "rules"
    if "code" in lowered:
        return "code"
    if "standard" in lowered:
        return "standard"
    if "determination" in lowered:
        return "determination"
    if "declaration" in lowered:
        return "declaration"
    if "guideline" in lowered:
        return "guideline"
    return "legislative_instrument"


def is_authorised_instrument_type(title: str) -> bool:
    lowered = title.casefold()
    return any(term in lowered for term in ("regulation", "rule", "code", "standard", "determination", "declaration", "guideline"))


def _is_hierarchy_heading(text: str) -> bool:
    lowered = text.casefold()
    return lowered.startswith(("chapter ", "part ", "division ", "subdivision ", "schedule "))


def _is_provision_heading(text: str) -> bool:
    if _is_hierarchy_heading(text):
        return False
    return bool(re.match(r"^(?:[0-9]+[A-Z]*|[A-Z]{1,3}[0-9]+[A-Z]*)\b\s+.+", text))


def _provision_number(text: str) -> str:
    match = re.match(r"^([0-9]+[A-Z]*|[A-Z]{1,3}[0-9]+[A-Z]*)\b", text)
    return match.group(1) if match else ""


def _normalise_provision_id(value: str) -> str:
    text = value.strip().casefold()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "provision"


def _updated_hierarchy(current: list[str], heading: str) -> list[str]:
    lowered = heading.casefold()
    if lowered.startswith("chapter "):
        return [heading]
    if lowered.startswith("part "):
        return [*(current[:1] if current and current[0].casefold().startswith("chapter ") else []), heading]
    if lowered.startswith("division "):
        prefix = [item for item in current if item.casefold().startswith(("chapter ", "part "))][:2]
        return [*prefix, heading]
    if lowered.startswith("subdivision "):
        prefix = [item for item in current if item.casefold().startswith(("chapter ", "part ", "division "))][:3]
        return [*prefix, heading]
    if lowered.startswith("schedule "):
        return [heading]
    return current


@dataclass(slots=True)
class FRLTitle:
    register_id: str
    title: str
    collection: str
    document_type: str
    status: str
    is_principal: bool
    is_in_force: bool
    latest_url: str
    source_url: str
    year: int | None = None
    number: int | None = None
    administering_department: str = ""
    classification: str = ""
    sub_collection: str = ""
    discovery_channel: str = "frl_api_catalogue"

    def to_manifest(self) -> dict[str, Any]:
        return asdict(self)


class AustraliaFRLCorpusBuilder:
    """Minimal FRL adapter for Australia Commonwealth Zone 1 acquisition."""

    def __init__(self, project_root: Path | str = PROJECT_ROOT, *, force: bool = False, download_all: bool = False) -> None:
        self.project_root = Path(project_root)
        self.force = force
        self.download_all = download_all
        self.data_root = self.project_root / "data" / "legal_sources" / "australia"
        self.output_root = self.project_root / "outputs" / "corpus" / "australia"
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=FRL_DOWNLOAD_WORKERS * 2, pool_maxsize=FRL_DOWNLOAD_WORKERS * 2, max_retries=0, pool_block=False)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/json,application/pdf,*/*"})
        self.failures: list[dict[str, Any]] = []
        self.downloaded: list[dict[str, Any]] = []
        self.duplicates: list[dict[str, Any]] = []
        self.sha_seen: dict[str, str] = {}
        self.cache_hits = 0
        self.documents_skipped_existing = 0
        self.full_catalogue: dict[str, FRLTitle] = {}
        self.authorised_instruments: dict[str, list[dict[str, Any]]] = {}
        self._state_lock = threading.Lock()
        self._manifest_lock = threading.Lock()
        self._manifest_success: dict[str, dict[str, Any]] = {}
        self.document_failures: dict[str, dict[str, Any]] = {}
        self.format_success: Counter[str] = Counter()

    def build_zone1(self) -> dict[str, Any]:
        from rdtii_tool.sources.australia.frl_adapter import AustraliaFRLAdapter
        from rdtii_tool.zone1.engine import Zone1CorpusEngine

        adapter = AustraliaFRLAdapter(self.project_root, force=self.force, download_all=self.download_all)
        run = Zone1CorpusEngine(adapter=adapter, project_root=self.project_root, workers=FRL_DOWNLOAD_WORKERS, force=self.force).run()
        self._download_official_guidance()
        self._write_treaty_status_seeds()
        report_path = self.output_root / "australia_download_report.json"
        if report_path.exists():
            return json.loads(report_path.read_text(encoding="utf-8"))
        return run["summary"]

    def _load_success_manifest(self) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for path in [
            self.data_root / "manifests" / "australia_downloaded_manifest.jsonl",
            self.output_root / "australia_source_manifest.jsonl",
        ]:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                register_id = str(row.get("register_id") or "").strip()
                if register_id and self._manifest_row_success(row):
                    rows[register_id] = row
        return rows

    def _manifest_row_success(self, row: dict[str, Any]) -> bool:
        status = str(row.get("html_download_status") or row.get("download_status") or "").casefold()
        if status not in {"success", "cache_hit"}:
            return False
        raw_path = self._existing_path(str(row.get("raw_file_path") or ""))
        if raw_path is None:
            register_id = str(row.get("register_id") or row.get("official_id") or "").strip()
            raw_path = self._existing_raw_for_register_id(register_id)
        if raw_path is None:
            return False
        return raw_path.exists() and raw_path.stat().st_size > 0

    def _local_manifest_success(self, row: dict[str, Any]) -> bool:
        if not self._manifest_row_success(row):
            return False
        norm_value = str(row.get("normalized_file_path") or "")
        if not norm_value:
            return True
        norm_path = self._existing_path(norm_value)
        if norm_path is None:
            register_id = str(row.get("register_id") or row.get("official_id") or "").strip()
            fallback = self.data_root / "normalized" / "frl" / f"{register_id}.txt"
            norm_path = fallback if fallback.exists() else None
        if norm_path is None:
            return False
        return norm_path.exists() and norm_path.stat().st_size > 0

    def _existing_path(self, value: str) -> Path | None:
        if not value:
            return None
        path = Path(value)
        candidates = [path]
        if not path.is_absolute():
            candidates.append(self.project_root / path)
        # Windows absolute paths are not meaningful under WSL/Linux. Fall back to
        # the repository-relative suffix when possible.
        normalized = value.replace("\\", "/")
        for marker in ("data/legal_sources/australia/", "outputs/corpus/australia/"):
            if marker in normalized:
                candidates.append(self.project_root / normalized[normalized.index(marker):])
        for candidate in candidates:
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
        return None

    def _existing_raw_for_register_id(self, register_id: str) -> Path | None:
        if not register_id:
            return None
        for folder in ("html", "api"):
            root = self.data_root / "raw" / "frl" / folder
            if not root.exists():
                continue
            for path in root.glob(f"{register_id}*"):
                if path.exists() and path.stat().st_size > 0:
                    return path
        return None

    def _record_failure(self, row: dict[str, Any]) -> None:
        with self._state_lock:
            self.failures.append(row)

    def _record_downloaded(self, metadata: dict[str, Any]) -> None:
        with self._state_lock:
            html_sha = str(metadata.get("sha256") or "")
            register_id = str(metadata.get("register_id") or "")
            if html_sha:
                duplicate_of = self.sha_seen.get(html_sha)
                if duplicate_of and duplicate_of != register_id:
                    self.duplicates.append({"register_id": register_id, "duplicate_of": duplicate_of, "sha256": html_sha})
                self.sha_seen.setdefault(html_sha, register_id)
            self.downloaded.append(metadata)
            self._manifest_success[register_id] = metadata
        self._append_downloaded_manifest(metadata)

    def _append_downloaded_manifest(self, metadata: dict[str, Any]) -> None:
        line = json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n"
        with self._manifest_lock:
            for path in [
                self.data_root / "manifests" / "australia_downloaded_manifest.jsonl",
                self.output_root / "australia_source_manifest.jsonl",
            ]:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
                    handle.flush()

    @staticmethod
    def _dedupe_downloaded(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        for index, row in enumerate(rows):
            register_id = str(row.get("register_id") or "").strip()
            key = register_id or str(row.get("source_url") or row.get("url") or row.get("title") or index)
            deduped[key] = row
        return list(deduped.values())

    def build_zone2(self) -> dict[str, Any]:
        """Create provision JSONL files consumed by the generic mapping pipeline."""
        self._ensure_dirs()
        for path in [
            self.output_root / "acts",
            self.output_root / "subsidiary",
            self.output_root / "manifests",
        ]:
            path.mkdir(parents=True, exist_ok=True)

        downloaded = self._load_downloaded_manifest()
        acts_manifest: list[dict[str, Any]] = []
        subsidiary_manifest: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        provision_count = 0

        for item in downloaded:
            register_id = str(item.get("register_id") or "").strip()
            if not register_id:
                continue
            metadata = self._metadata_for_register_id(register_id, item)
            title = str(metadata.get("title") or item.get("title") or register_id)
            raw_html = self.data_root / "raw" / "frl" / "html" / f"{register_id}.html"
            if not raw_html.exists():
                failures.append({"register_id": register_id, "title": title, "stage": "zone2", "reason": "raw_html_missing"})
                continue
            body_url = self._ensure_frl_body_html(raw_html, register_id=register_id, title=title)
            html_text = raw_html.read_text(encoding="utf-8", errors="replace")
            rows = self._extract_provision_rows(metadata, html_text, body_url)
            if len(rows) <= 1:
                rows = self._rows_from_normalized_text(metadata, fallback_rows=rows)
            document_type = str(metadata.get("document_type") or item.get("document_type") or "")
            is_act = register_id.startswith("C") or document_type == "act"
            document_id = self._document_id(register_id, is_act=is_act)
            out_dir = self.output_root / ("acts" if is_act else "subsidiary")
            jsonl_path = out_dir / f"{document_id}.jsonl"
            rows = [dict(row, document_id=document_id, instrument_type="act" if is_act else "subsidiary_legislation") for row in rows]
            write_jsonl(jsonl_path, rows)
            manifest_row = {
                "document_id": document_id,
                "official_title": title,
                "canonical_url": metadata.get("latest_version_url") or metadata.get("source_url") or item.get("source_url"),
                "download_url": metadata.get("text_url") or metadata.get("source_url") or item.get("source_url"),
                "final_response_url": body_url or metadata.get("text_url") or item.get("source_url"),
                "raw_html_path": str(raw_html),
                "jsonl_path": str(jsonl_path),
                "provision_count": len(rows),
                "download_status": "success",
                "parse_status": "success" if rows else "failed",
                "document_completeness": "complete" if rows else "empty",
                "error_type": "" if rows else "parse_error",
                "error": "" if rows else "No provisions parsed from FRL body HTML",
                "register_id": register_id,
                "status": metadata.get("status") or item.get("status") or "InForce",
            }
            provision_count += len(rows)
            if is_act:
                acts_manifest.append(manifest_row)
            else:
                subsidiary_manifest.append(manifest_row)
            if not rows:
                failures.append({"register_id": register_id, "title": title, "stage": "zone2", "reason": "no_provisions_parsed"})

        write_jsonl(self.output_root / "manifests" / "acts_manifest.jsonl", acts_manifest)
        write_jsonl(self.output_root / "manifests" / "subsidiary_manifest.jsonl", subsidiary_manifest)
        summary = {
            "economy": "Australia",
            "zone": 2,
            "generated_at": utc_now(),
            "acts_parsed": len(acts_manifest),
            "subsidiary_legislation_parsed": len(subsidiary_manifest),
            "documents_parsed": len(acts_manifest) + len(subsidiary_manifest),
            "total_provisions": provision_count,
            "failures": len(failures),
            "source_manifest": str(self.data_root / "manifests" / "australia_downloaded_manifest.jsonl"),
        }
        write_json(self.output_root / "zone2_provision_summary.json", summary)
        write_jsonl(self.output_root / "zone2_failed_provisions.jsonl", failures)
        return summary

    def build_zone1_provisions(self) -> dict[str, Any]:
        """Create deterministic provision JSONL as part of Zone 1 standardization.

        The implementation is the former Australia provisionization step.  The
        public workflow no longer treats this as RDTII mapping/Zone 2; mapping
        consumes the per-document provision manifest generated afterwards.
        """
        summary = self.build_zone2()
        summary["zone"] = 1
        summary["stage"] = "zone1_provisionization"
        return summary

    def _ensure_dirs(self) -> None:
        for path in [
            self.data_root / "raw" / "frl" / "html",
            self.data_root / "raw" / "frl" / "pdf",
            self.data_root / "raw" / "official_guidance" / "html",
            self.data_root / "raw" / "official_guidance" / "pdf",
            self.data_root / "normalized" / "frl",
            self.data_root / "normalized" / "official_guidance",
            self.data_root / "metadata" / "frl",
            self.data_root / "metadata" / "official_guidance",
            self.data_root / "manifests",
            self.output_root,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def _write_source_registry(self) -> None:
        registry = {
            "economy": "Australia",
            "country_code": "AU",
            "generated_at": utc_now(),
            "sources": [
                {
                    "portal_name": "Australian Federal Register of Legislation",
                    "portal_code": "australia_frl",
                    "jurisdiction": "Commonwealth of Australia",
                    "authority": "Office of Parliamentary Counsel",
                    "base_url": FRL_BASE,
                    "directories": [f"{FRL_BASE}/acts", f"{FRL_BASE}/legislative-instruments"],
                    "official": True,
                    "source_type": "official_legislation_portal",
                    "state_and_territory_phase": "not_applicable",
                },
                {
                    "portal_name": "Office of the Australian Information Commissioner",
                    "portal_code": "oaic",
                    "jurisdiction": "Commonwealth of Australia",
                    "authority": "OAIC",
                    "base_url": "https://www.oaic.gov.au/",
                    "official": True,
                    "source_type": "official_regulatory_guidance",
                    "legally_binding": False,
                    "usable_for_interpretation": True,
                    "usable_as_legal_basis": False,
                },
                {
                    "portal_name": "Department of Home Affairs",
                    "portal_code": "home_affairs_cyber_strategy",
                    "jurisdiction": "Commonwealth of Australia",
                    "authority": "Department of Home Affairs",
                    "base_url": "https://www.homeaffairs.gov.au/",
                    "official": True,
                    "source_type": "government_strategy",
                    "legally_binding": False,
                    "do_not_treat_as_statutory_obligation": True,
                },
                {
                    "portal_name": "Department of Foreign Affairs and Trade",
                    "portal_code": "dfat_trade_agreements",
                    "jurisdiction": "Commonwealth of Australia",
                    "authority": "DFAT",
                    "base_url": "https://www.dfat.gov.au/trade/agreements/trade-agreements",
                    "official": True,
                    "source_type": "official_treaty_status_seed",
                },
            ],
            "state_and_territory_sources": [
                {"jurisdiction": name, "state_and_territory_phase": "pending"}
                for name in STATE_TERRITORY_SOURCES
            ],
        }
        write_json(self.data_root / "source_registry.json", registry)

    def capability_check(self) -> dict[str, Any]:
        checks = {
            "can_list_acts": self._http_ok(f"{FRL_BASE}/acts"),
            "can_list_legislative_instruments": self._http_ok(f"{FRL_BASE}/legislative-instruments"),
            "can_resolve_latest": self._http_ok(f"{FRL_BASE}/C2004A03712/latest"),
            "can_download_html": self._http_ok(f"{FRL_BASE}/C2004A03712/latest/text"),
            "can_download_pdf": False,
            "can_parse_details": self._http_ok(f"{FRL_BASE}/C2004A03712/latest/details"),
            "can_parse_versions": self._http_ok(f"{FRL_BASE}/C2004A03712/latest/versions"),
            "can_parse_authorises": self._http_ok(f"{FRL_BASE}/C2004A03712/latest/authorises"),
            "requires_new_adapter": True,
            "reason": "Existing downloader/parser are Singapore SSO-specific; FRL uses different API, /latest, /downloads, /authorises, and EPUB-derived HTML structure.",
            "checked_at": utc_now(),
        }
        try:
            downloads = self._get_text(f"{FRL_BASE}/C2004A03712/latest/downloads")
            pdf_url = self._first_pdf_url(downloads, f"{FRL_BASE}/C2004A03712/latest/downloads")
            checks["can_download_pdf"] = bool(pdf_url and self._head_or_get_ok(pdf_url, expect_pdf=True))
        except Exception as exc:  # noqa: BLE001
            checks["can_download_pdf_error"] = str(exc)
        return checks

    def discover_catalogue(self) -> dict[str, FRLTitle]:
        cached = self._load_catalogue_cache()
        if cached and not self.force:
            print(f"FRL catalogue discovery cache hit: {len(cached)} records", flush=True)
            return cached
        result: dict[str, FRLTitle] = {}
        for collection in ("Act", "LegislativeInstrument", "NotifiableInstrument"):
            skip = 0
            top = 100
            while True:
                payload = self._api_search(collection, skip=skip, top=top)
                for item in payload.get("value", []):
                    register_id = str(item.get("id", "")).strip()
                    title = normalise_text(str(item.get("name", "")))
                    if not register_id or not title:
                        continue
                    latest_url = f"{FRL_BASE}/{register_id}/latest"
                    document_type = classify_document_type(collection, register_id, title)
                    dept_names = ""
                    result[register_id] = FRLTitle(
                        register_id=register_id,
                        title=title,
                        collection=collection,
                        document_type=document_type,
                        status="InForce" if item.get("isInForce") else str(item.get("status", "")),
                        is_principal=bool(item.get("isPrincipal", False)),
                        is_in_force=bool(item.get("isInForce", False)),
                        latest_url=latest_url,
                        source_url=latest_url,
                        year=item.get("year"),
                        number=item.get("number"),
                        administering_department=dept_names,
                        classification=str(item.get("seriesType") or item.get("collection") or collection),
                        sub_collection=str(item.get("subCollection") or ""),
                    )
                count = int(payload.get("@odata.count") or 0)
                skip += top
                if skip % 1000 == 0 or skip >= count:
                    print(f"FRL catalogue discovery {collection}: {min(skip, count)}/{count}", flush=True)
                if skip >= count or not payload.get("value"):
                    break
        return result


    def _load_catalogue_cache(self) -> dict[str, FRLTitle]:
        path = self.data_root / "manifests" / "australia_frl_current_catalog.jsonl"
        if not path.exists() or not path.stat().st_size:
            return {}
        result: dict[str, FRLTitle] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                title = FRLTitle(
                    register_id=str(payload.get("register_id", "")),
                    title=str(payload.get("title", "")),
                    collection=str(payload.get("collection", "")),
                    document_type=str(payload.get("document_type", "")),
                    status=str(payload.get("status", "")),
                    is_principal=bool(payload.get("is_principal", False)),
                    is_in_force=bool(payload.get("is_in_force", False)),
                    latest_url=str(payload.get("latest_url", "")),
                    source_url=str(payload.get("source_url", "")),
                    year=payload.get("year"),
                    number=payload.get("number"),
                    administering_department=str(payload.get("administering_department", "")),
                    classification=str(payload.get("classification", "")),
                    sub_collection=str(payload.get("sub_collection", "")),
                    discovery_channel=str(payload.get("discovery_channel", "frl_api_catalogue")),
                )
            except Exception:
                continue
            if title.register_id:
                result[title.register_id] = title
        if result and "NotifiableInstrument" not in {item.collection for item in result.values()}:
            return {}
        return result

    def _api_search(self, collection: str, *, skip: int, top: int) -> dict[str, Any]:
        criteria = f"and(collection({collection}),status(InForce),type(Principal))"
        url = f"{FRL_API}/titles/search(criteria='{criteria}')"
        params = {
            "$select": "collection,id,isInForce,isPrincipal,name,number,optionalSeriesNumber,seriesType,subCollection,year",
            "$orderby": "name asc",
            "$count": "true",
            "$top": str(top),
            "$skip": str(skip),
        }
        response = self.session.get(url, params=params, timeout=60)
        response.raise_for_status()
        return response.json()

    def _write_catalogue_manifests(self, titles: Iterable[FRLTitle]) -> None:
        rows = [title.to_manifest() for title in sorted(titles, key=lambda x: (x.collection, x.title.casefold(), x.register_id))]
        write_jsonl(self.data_root / "manifests" / "australia_frl_current_catalog.jsonl", rows)
        write_jsonl(self.output_root / "australia_source_manifest.jsonl", rows)

    def _target_documents(self, catalogue: dict[str, FRLTitle]) -> dict[str, FRLTitle]:
        targets: dict[str, FRLTitle] = {}
        title_index = {v.title.casefold(): v for v in catalogue.values()}
        for seed in CORE_SEEDS:
            title = None
            if seed.get("register_id"):
                title = catalogue.get(seed["register_id"])
                if title is None:
                    title = self._title_from_id(seed["register_id"], fallback_title=seed["title"])
            else:
                title = title_index.get(seed["title"].casefold()) or self._find_title_by_name(seed["title"], catalogue)
            if title is None:
                self.failures.append({"stage": "seed_resolution", "title": seed["title"], "reason": "not_found_in_frl_catalogue"})
                continue
            title.discovery_channel = "core_validation_seed"
            targets[title.register_id] = title

        # Authorised current instruments for core Acts, plus CDR/Cyber security related instruments.
        for title in list(targets.values()):
            if title.register_id.startswith("C"):
                auth = self.parse_authorises(title)
                self.authorised_instruments[title.register_id] = auth
                for row in auth:
                    if row.get("include_in_corpus"):
                        inst = catalogue.get(row["register_id"]) or self._title_from_id(row["register_id"], fallback_title=row.get("title", ""))
                        if inst is not None:
                            inst.discovery_channel = f"authorised_by:{title.register_id}"
                            targets[inst.register_id] = inst

        cdr_extra = [item for item in catalogue.values() if "consumer data right" in item.title.casefold() and item.collection == "LegislativeInstrument"]
        for item in cdr_extra:
            item.discovery_channel = "consumer_data_right_title_match"
            targets[item.register_id] = item
        return targets

    def _find_title_by_name(self, name: str, catalogue: dict[str, FRLTitle]) -> FRLTitle | None:
        wanted = name.casefold()
        for item in catalogue.values():
            if item.title.casefold() == wanted:
                return item
        for item in catalogue.values():
            if wanted in item.title.casefold():
                return item
        return None

    def _title_from_id(self, register_id: str, *, fallback_title: str = "") -> FRLTitle | None:
        try:
            response = self.session.get(f"{FRL_API}/titles('{register_id}')", timeout=30)
            if response.status_code != 200:
                return None
            item = response.json()
            title = normalise_text(str(item.get("name") or fallback_title or register_id))
            collection = str(item.get("collection") or ("Act" if register_id.startswith("C") else "LegislativeInstrument"))
            return FRLTitle(
                register_id=register_id,
                title=title,
                collection=collection,
                document_type=classify_document_type(collection, register_id, title),
                status=str(item.get("status") or ("InForce" if item.get("isInForce") else "unknown")),
                is_principal=bool(item.get("isPrincipal", True)),
                is_in_force=bool(item.get("isInForce", False)),
                latest_url=f"{FRL_BASE}/{register_id}/latest",
                source_url=f"{FRL_BASE}/{register_id}/latest",
                year=item.get("year"),
                number=item.get("number"),
                classification=str(item.get("seriesType") or collection),
            )
        except requests.RequestException:
            return None

    def parse_authorises(self, title: FRLTitle) -> list[dict[str, Any]]:
        url = f"{FRL_BASE}/{title.register_id}/latest/authorises"
        rows: list[dict[str, Any]] = []
        try:
            page = self._get_text(url)
        except requests.RequestException as exc:
            self.failures.append({"stage": "authorises", "register_id": title.register_id, "url": url, "reason": str(exc)})
            return rows
        soup = BeautifulSoup(page, "html.parser")
        seen: set[str] = set()
        for link in soup.find_all("a", href=True):
            href = urljoin(url, link["href"])
            match = re.search(r"/([CF]\d{4}[A-Z]\d{5})/(latest|asmade)", urlsplit(href).path)
            if not match:
                continue
            rid = match.group(1)
            if rid == title.register_id or rid in seen:
                continue
            seen.add(rid)
            label = normalise_text(link.get_text(" ", strip=True))
            item = self._title_from_id(rid, fallback_title=label)
            in_force = bool(item and item.is_in_force)
            rows.append({
                "parent_register_id": title.register_id,
                "register_id": rid,
                "title": item.title if item else label,
                "url": f"{FRL_BASE}/{rid}/latest" if in_force else href,
                "status": item.status if item else "unknown",
                "document_type": item.document_type if item else "unknown",
                "include_in_corpus": bool(in_force and item and rid.startswith("F") and is_authorised_instrument_type(item.title)),
            })
        return rows

    def download_frl_document(self, title: FRLTitle) -> dict[str, Any] | None:
        if not title.is_in_force and title.status != "InForce":
            self._record_failure({"stage": "lifecycle", "register_id": title.register_id, "title": title.title, "reason": "not_in_force"})
            self._record_document_failure(title, version={}, attempts=[{"source": "frl_api", "status": "not_in_force", "error": "title_not_in_force"}])
            return None
        raw_html = self.data_root / "raw" / "frl" / "html" / f"{title.register_id}.html"
        norm_path = self.data_root / "normalized" / "frl" / f"{title.register_id}.txt"
        meta_path = self.data_root / "metadata" / "frl" / f"{title.register_id}.json"
        details: dict[str, str] = {}
        authorises_url = f"{FRL_BASE}/{title.register_id}/latest/authorises" if title.register_id.startswith("C") else ""
        if not self.force and norm_path.exists() and norm_path.stat().st_size > 0:
            existing_raw = self._existing_raw_for_register_id(title.register_id)
            if existing_raw:
                self._ensure_synthetic_html_from_normalized(title=title.title, norm_path=norm_path, raw_html=raw_html)
                metadata = self._metadata_from_existing(title, existing_raw, norm_path, meta_path)
                self._record_downloaded(metadata)
                return metadata

        attempts: list[dict[str, Any]] = []
        version = self._api_current_version(title, attempts)
        documents = self._api_primary_documents(title, version, attempts)
        selected = self._download_and_normalize_api_document(title, documents, attempts)
        if selected is None:
            selected = self._website_fallback_download(title, attempts)
        if selected is None:
            self._record_document_failure(title, version=version or {}, attempts=attempts)
            return None

        raw_path = selected["raw_path"]
        normalized = selected["normalized_text"]
        norm_path.parent.mkdir(parents=True, exist_ok=True)
        norm_path.write_text(normalized + "\n", encoding="utf-8")
        raw_html.parent.mkdir(parents=True, exist_ok=True)
        raw_html.write_text(self._normalized_text_to_html(title.title, normalized), encoding="utf-8")
        sections = self._extract_section_headings(raw_html.read_text(encoding="utf-8", errors="replace"))
        version_register_id = str((version or {}).get("registerId") or title.register_id)
        version_id = str((version or {}).get("compilationNumber") or selected.get("compilation_number") or "latest")
        raw_sha = sha256_file(raw_path)
        metadata = {
            "economy": "Australia",
            "country": "Australia",
            "jurisdiction": "Commonwealth of Australia",
            "portal_name": "Australian Federal Register of Legislation",
            "portal_code": "australia_frl",
            "authority": "Office of Parliamentary Counsel",
            "official": True,
            "title": title.title,
            "official_title": title.title,
            "register_id": title.register_id,
            "official_id": title.register_id,
            "series_id": title.register_id,
            "collection": title.collection,
            "instrument_type": title.document_type,
            "document_type": title.document_type,
            "classification": details.get("series") or title.classification,
            "administering_department": details.get("administering_department") or title.administering_department,
            "status": details.get("status") or title.status,
            "compilation_number": version_id,
            "registered_date": str((version or {}).get("registeredAt") or ""),
            "effective_from": str((version or {}).get("start") or ""),
            "effective_date": str((version or {}).get("start") or ""),
            "effective_to": str((version or {}).get("end") or ""),
            "commencement_date": details.get("commencement_date", ""),
            "repeal_date": details.get("repeal_date", ""),
            "latest_version_url": title.latest_url,
            "version_id": version_id,
            "current_version_register_id": version_register_id,
            "text_url": selected.get("source_url", ""),
            "legal_text_url": selected.get("source_url", ""),
            "downloads_url": "",
            "versions_url": f"{FRL_API}/versions/find(titleid='{title.register_id}',asat={datetime.now(timezone.utc).date().isoformat()})",
            "authorises_url": authorises_url,
            "source_url": title.source_url,
            "retrieved_at": utc_now(),
            "sha256": raw_sha,
            "pdf_sha256": raw_sha if selected.get("format") == "Pdf" else "",
            "raw_file_path": str(raw_path),
            "raw_html_path": str(raw_html),
            "raw_pdf_path": str(raw_path) if selected.get("format") == "Pdf" else "",
            "normalized_file_path": str(norm_path),
            "local_path": str(norm_path),
            "metadata_path": str(meta_path),
            "download_status": "success",
            "html_download_status": "success" if selected.get("format") in {"Epub", "Html"} else "",
            "pdf_download_status": "success" if selected.get("format") == "Pdf" else "",
            "api_download_status": "success",
            "source_format": selected.get("format", ""),
            "source_extension": selected.get("extension", ""),
            "api_document": selected.get("document", {}),
            "file_size": raw_path.stat().st_size,
            "normalized_char_count": len(normalized),
            "section_heading_count": len(sections),
            "section_headings_sample": sections[:25],
            "versions": [version] if version else [],
            "pending_versions": [],
            "authorised_instruments": self.authorised_instruments.get(title.register_id, []),
            "discovery_channel": title.discovery_channel,
        }
        write_json(meta_path, metadata)
        with self._state_lock:
            self.format_success[str(selected.get("format") or "unknown")] += 1
        self._record_downloaded(metadata)
        return metadata

    def _metadata_from_existing(self, title: FRLTitle, raw_path: Path, norm_path: Path, meta_path: Path) -> dict[str, Any]:
        metadata = {}
        if meta_path.exists():
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                metadata = {}
        metadata.update({
            "economy": "Australia",
            "country": "Australia",
            "title": title.title,
            "official_title": title.title,
            "register_id": title.register_id,
            "official_id": title.register_id,
            "collection": title.collection,
            "instrument_type": title.document_type,
            "document_type": title.document_type,
            "status": title.status,
            "source_url": title.source_url,
            "download_status": "cache_hit",
            "api_download_status": metadata.get("api_download_status") or "cache_hit",
            "raw_file_path": str(raw_path),
            "raw_html_path": str(self.data_root / "raw" / "frl" / "html" / f"{title.register_id}.html"),
            "normalized_file_path": str(norm_path),
            "local_path": str(norm_path),
            "metadata_path": str(meta_path),
            "sha256": sha256_file(raw_path),
            "file_size": raw_path.stat().st_size,
            "normalized_char_count": norm_path.stat().st_size,
            "retrieved_at": metadata.get("retrieved_at") or utc_now(),
        })
        write_json(meta_path, metadata)
        return metadata

    def _ensure_synthetic_html_from_normalized(self, *, title: str, norm_path: Path, raw_html: Path) -> None:
        if raw_html.exists() and raw_html.stat().st_size > 0:
            return
        if not norm_path.exists() or norm_path.stat().st_size <= 0:
            return
        text = norm_path.read_text(encoding="utf-8", errors="replace")
        raw_html.parent.mkdir(parents=True, exist_ok=True)
        raw_html.write_text(self._normalized_text_to_html(title, text), encoding="utf-8")

    @staticmethod
    def _normalized_text_to_html(title: str, text: str) -> str:
        clean_title = html.escape(normalise_text(title) or "Document")
        sections = AustraliaFRLCorpusBuilder._split_normalized_text_sections(text)
        body_parts = [
            "<!doctype html>",
            "<html><head><meta charset=\"utf-8\"></head><body>",
            f"<p class=\"ActHead1\">{clean_title}</p>",
        ]
        for index, section in enumerate(sections, start=1):
            heading = html.escape(section["heading"])
            body_parts.append(f"<p class=\"ActHead5\" id=\"s{index}\">{heading}</p>")
            for paragraph in section["body"]:
                normalized_para = normalise_text(paragraph)
                if normalized_para:
                    body_parts.append(f"<p class=\"subsection\">{html.escape(normalized_para)}</p>")
        body_parts.append("</body></html>")
        return "\n".join(body_parts)

    @staticmethod
    def _split_normalized_text_sections(text: str) -> list[dict[str, Any]]:
        raw_lines = [normalise_text(line) for line in text.splitlines()]
        lines = [line for line in raw_lines if line]
        if len(lines) <= 1:
            collapsed = normalise_text(text)
            if not collapsed:
                return []
            lines = AustraliaFRLCorpusBuilder._split_inline_section_text(collapsed)
        heading_re = re.compile(
            r"^(?:"
            r"[0-9]{1,4}[A-Z]*[A-Z]?(?:\s+[A-Z][A-Za-z0-9()/,'’.-]+.*)?|"
            r"[A-Z]{1,4}[0-9]{1,4}[A-Z]*\s+.+|"
            r"Part\s+[0-9IVXLCDM]+[A-Z]*(?:\s+.+)?|"
            r"Division\s+[0-9]+[A-Z]*(?:\s+.+)?|"
            r"Subdivision\s+[A-Z0-9]+(?:\s+.+)?|"
            r"Chapter\s+[0-9IVXLCDM]+(?:\s+.+)?|"
            r"Schedule\s+[0-9A-Z]+(?:\s+.+)?"
            r")$",
            re.IGNORECASE,
        )
        sections: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        preamble: list[str] = []
        for line in lines:
            is_heading = bool(heading_re.match(line)) and len(line) <= 220
            if is_heading:
                if current:
                    sections.append(current)
                elif preamble:
                    sections.append({"heading": "Preamble", "body": preamble})
                    preamble = []
                current = {"heading": line, "body": []}
            elif current:
                current["body"].append(line)
            else:
                preamble.append(line)
        if current:
            sections.append(current)
        elif preamble:
            sections.append({"heading": "1 Text", "body": preamble})
        if not sections and text.strip():
            sections.append({"heading": "1 Text", "body": [normalise_text(text)]})
        return sections

    @staticmethod
    def _split_inline_section_text(text: str) -> list[str]:
        pattern = re.compile(
            r"(?=\b(?:Part\s+[0-9IVXLCDM]+|Division\s+[0-9]+|Schedule\s+[0-9A-Z]+|[0-9]{1,4}[A-Z]*\s+[A-Z][A-Za-z0-9()/,'’.-]+))"
        )
        parts = [part.strip() for part in pattern.split(text) if part.strip()]
        return parts if len(parts) > 1 else [text]

    def _api_current_version(self, title: FRLTitle, attempts: list[dict[str, Any]]) -> dict[str, Any]:
        as_at = datetime.now(timezone.utc).date().isoformat()
        url = f"{FRL_API}/versions/find(titleid='{title.register_id}',asat={as_at})"
        try:
            response = self._get_with_retries(url, accept="application/json")
            if response.status_code != 200:
                attempts.append({"source": "frl_api", "endpoint": "Versions.Find", "status": response.status_code, "error": response.text[:300]})
                return {}
            version = response.json()
            if isinstance(version, dict):
                return version
        except Exception as exc:
            attempts.append({"source": "frl_api", "endpoint": "Versions.Find", "status": None, "error": str(exc)})
        return {}

    def _api_primary_documents(self, title: FRLTitle, version: dict[str, Any], attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        docs = version.get("documents") if isinstance(version, dict) else None
        if isinstance(docs, list) and docs:
            return [doc for doc in docs if self._doc_type(doc) == "Primary" and self._doc_format(doc) != "NameOnly"]
        register_id = str(version.get("registerId") or title.register_id) if isinstance(version, dict) else title.register_id
        url = f"{FRL_API}/Documents?$top=100&$filter=registerId%20eq%20'{register_id}'%20and%20type%20eq%20'Primary'"
        try:
            response = self._get_with_retries(url, accept="application/json")
            if response.status_code != 200:
                attempts.append({"source": "frl_api", "endpoint": "Documents", "status": response.status_code, "error": response.text[:300]})
                return []
            payload = response.json()
            values = payload.get("value") if isinstance(payload, dict) else payload
            if isinstance(values, list):
                return [doc for doc in values if self._doc_type(doc) == "Primary" and self._doc_format(doc) != "NameOnly"]
        except Exception as exc:
            attempts.append({"source": "frl_api", "endpoint": "Documents", "status": None, "error": str(exc)})
        return []

    @staticmethod
    def _doc_type(document: dict[str, Any]) -> str:
        value = document.get("type")
        return {0: "Primary", 1: "ES", 2: "SupportingMaterial", 3: "IncorporatedByReference", 4: "SupplementaryES"}.get(value, str(value or ""))

    @staticmethod
    def _doc_format(document: dict[str, Any]) -> str:
        value = document.get("format")
        return {0: "NameOnly", 1: "Word", 2: "Pdf", 3: "Epub"}.get(value, str(value or ""))

    def _download_and_normalize_api_document(self, title: FRLTitle, documents: list[dict[str, Any]], attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not documents:
            attempts.append({"source": "frl_api", "document_type": "Primary", "status": "no_primary_documents", "error": "No primary legislation documents in API metadata"})
            return None
        priority = {"Epub": 0, "Word": 1, "Pdf": 2}
        ordered = sorted(
            documents,
            key=lambda doc: (
                priority.get(self._doc_format(doc), 99),
                int(doc.get("volumeNumber") or 0),
                int(doc.get("uniqueTypeNumber") or 0),
            ),
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for doc in ordered:
            grouped.setdefault(self._doc_format(doc), []).append(doc)
        for fmt in ("Epub", "Word", "Pdf"):
            docs = grouped.get(fmt) or []
            if not docs:
                continue
            pieces: list[str] = []
            raw_paths: list[Path] = []
            source_urls: list[str] = []
            used_docs: list[dict[str, Any]] = []
            for doc in docs:
                content = self._download_api_document_bytes(doc, attempts)
                if content is None:
                    continue
                extension = str(doc.get("extension") or self._extension_for_format(fmt)).lower()
                raw_path = self.data_root / "raw" / "frl" / "api" / f"{title.register_id}_{doc.get('registerId') or title.register_id}_{fmt.lower()}_{doc.get('volumeNumber', 0)}{extension}"
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_bytes(content)
                text = self._normalise_api_content(content, fmt=fmt, extension=extension, title=title.title)
                if text:
                    pieces.append(text)
                    raw_paths.append(raw_path)
                    source_urls.append(self._api_document_url(doc))
                    used_docs.append(doc)
            normalized = normalise_text("\n\n".join(pieces))
            if len(normalized) >= 50 and raw_paths:
                return {
                    "format": fmt,
                    "extension": str(used_docs[0].get("extension") or self._extension_for_format(fmt)),
                    "raw_path": raw_paths[0],
                    "raw_paths": [str(p) for p in raw_paths],
                    "source_url": source_urls[0],
                    "normalized_text": normalized,
                    "document": used_docs[0],
                    "documents": used_docs,
                    "compilation_number": used_docs[0].get("compilationNumber"),
                }
            attempts.append({"source": "frl_api", "document_type": "Primary", "format": fmt, "status": "normalize_failed", "error": "Downloaded content could not be normalized"})
        return None

    def _download_api_document_bytes(self, document: dict[str, Any], attempts: list[dict[str, Any]]) -> bytes | None:
        url = self._api_document_url(document)
        try:
            response = self._get_with_retries(url, accept="application/octet-stream")
        except Exception as exc:
            attempts.append({"source": "frl_api", "document_type": self._doc_type(document), "format": self._doc_format(document), "status": None, "error": str(exc), "url": url})
            self._record_failure({"stage": "api_document_download", "register_id": document.get("titleId"), "url": url, "reason": str(exc)})
            return None
        if response.status_code != 200:
            attempts.append({"source": "frl_api", "document_type": self._doc_type(document), "format": self._doc_format(document), "status": response.status_code, "error": response.text[:300], "url": url})
            self._record_failure({"stage": "api_document_download", "register_id": document.get("titleId"), "url": url, "http_status": response.status_code, "reason": "non_200"})
            return None
        return response.content

    def _api_document_url(self, document: dict[str, Any]) -> str:
        register_id = str(document.get("registerId") or document.get("titleId") or "")
        doc_type = self._doc_type(document)
        fmt = self._doc_format(document)
        unique = int(document.get("uniqueTypeNumber") or 0)
        volume = int(document.get("volumeNumber") or 0)
        rectification = int(document.get("rectificationVersionNumber") or 0)
        return (
            f"{FRL_API}/documents/find(registerId='{register_id}',type='{doc_type}',format='{fmt}',"
            f"uniqueTypeNumber={unique},volumeNumber={volume},rectificationVersionNumber={rectification})"
        )

    @staticmethod
    def _extension_for_format(fmt: str) -> str:
        return {"Epub": ".epub", "Word": ".docx", "Pdf": ".pdf"}.get(fmt, ".bin")

    def _normalise_api_content(self, content: bytes, *, fmt: str, extension: str, title: str) -> str:
        try:
            if fmt == "Epub":
                return self._normalise_epub(content)
            if fmt == "Word":
                if extension.lower() == ".docx" or content.startswith(b"PK"):
                    return self._normalise_docx(content)
                if extension.lower() == ".rtf" or content.lstrip().startswith(b"{\\rtf"):
                    return self._normalise_rtf(content.decode("utf-8", errors="replace"))
                return ""
            if fmt == "Pdf":
                return self._normalise_pdf_bytes(content)
        except Exception:
            return ""
        return ""

    def _normalise_epub(self, content: bytes) -> str:
        texts: list[str] = []
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = sorted(name for name in archive.namelist() if name.lower().endswith((".html", ".xhtml", ".htm")))
            for name in names:
                try:
                    html_text = archive.read(name).decode("utf-8", errors="replace")
                except Exception:
                    continue
                soup = BeautifulSoup(html_text, "html.parser")
                text = normalise_text(soup.get_text(" ", strip=True))
                if text:
                    texts.append(text)
        return normalise_text("\n\n".join(texts))

    def _normalise_docx(self, content: bytes) -> str:
        texts: list[str] = []
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for name in sorted(n for n in archive.namelist() if n.startswith("word/") and n.endswith(".xml")):
                if not (name == "word/document.xml" or name.startswith("word/header") or name.startswith("word/footer")):
                    continue
                xml_bytes = archive.read(name)
                root = ET.fromstring(xml_bytes)
                for elem in root.iter():
                    if elem.tag.endswith("}t") and elem.text:
                        texts.append(elem.text)
                    elif elem.tag.endswith("}p"):
                        texts.append("\n")
        return normalise_text(" ".join(texts))

    @staticmethod
    def _normalise_rtf(text: str) -> str:
        text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
        text = re.sub(r"\\[a-zA-Z]+-?[0-9]* ?", " ", text)
        text = text.replace("{", " ").replace("}", " ").replace("\\", " ")
        return normalise_text(text)

    @staticmethod
    def _normalise_pdf_bytes(content: bytes) -> str:
        reader = PdfReader(io.BytesIO(content))
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        return normalise_text("\n\n".join(pages))

    def _website_fallback_download(self, title: FRLTitle, attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
        # Final fallback only: use actual links on the public title page. Do not
        # construct /latest/text, /latest/downloads, or EPUB internal routes.
        url = title.source_url
        try:
            response = self._get_with_retries(url)
            if response.status_code != 200:
                attempts.append({"source": "website_fallback", "status": response.status_code, "error": response.text[:200], "url": url})
                return None
        except Exception as exc:
            attempts.append({"source": "website_fallback", "status": None, "error": str(exc), "url": url})
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        links = []
        for link in soup.find_all("a", href=True):
            href = urljoin(url, str(link["href"]))
            lowered = href.casefold()
            if any(lowered.endswith(ext) or f"/{ext.strip('.')}" in lowered for ext in (".docx", ".rtf", ".doc", ".pdf", ".epub")):
                links.append(href)
        for href in links[:10]:
            ext = "." + href.rsplit(".", 1)[-1].split("?", 1)[0].lower() if "." in href.rsplit("/", 1)[-1] else ".bin"
            fmt = "Pdf" if ext == ".pdf" else "Epub" if ext == ".epub" else "Word"
            raw_path = self.data_root / "raw" / "frl" / "api" / f"{title.register_id}_fallback{ext}"
            result = self._download_binary(href, raw_path, expect="pdf" if fmt == "Pdf" else "html" if fmt == "Epub" else "binary", title=title.title, register_id=title.register_id)
            if result.get("status") not in {"success", "cache_hit"}:
                attempts.append({"source": "website_fallback", "format": fmt, "status": result.get("http_status"), "error": result.get("reason"), "url": href})
                continue
            content = raw_path.read_bytes()
            text = self._normalise_api_content(content, fmt=fmt, extension=ext, title=title.title)
            if len(text) >= 50:
                return {"format": fmt, "extension": ext, "raw_path": raw_path, "source_url": href, "normalized_text": text, "document": {}, "compilation_number": "fallback"}
        return None

    def _record_document_failure(self, title: FRLTitle, *, version: dict[str, Any], attempts: list[dict[str, Any]]) -> None:
        row = {
            "title_id": title.register_id,
            "register_id": title.register_id,
            "collection": title.collection,
            "title": title.title,
            "version_id": str(version.get("registerId") or version.get("compilationNumber") or ""),
            "final_status": "failed",
            "attempts": attempts,
        }
        with self._state_lock:
            self.document_failures[title.register_id] = row

    def _ensure_frl_body_html(self, path: Path, *, register_id: str, title: str) -> str:
        html_text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        if self._looks_like_frl_body(html_text):
            return ""
        body_urls = self._body_document_urls(html_text, fallback_base=f"{FRL_BASE}/{register_id}/latest/text")
        if not body_urls:
            return ""
        bodies: list[str] = []
        failed: list[str] = []
        for body_url in body_urls:
            try:
                response = self._get_with_retries(body_url)
                response.raise_for_status()
            except requests.RequestException as exc:
                failed.append(f"{body_url}: {exc}")
                continue
            content = response.content
            text = content.decode("utf-8", errors="replace")
            if len(content) < MIN_HTML_BYTES or not self._looks_like_frl_body(text):
                failed.append(f"{body_url}: body_html_invalid_or_empty")
                continue
            bodies.append(text)
        if not bodies:
            self._record_failure({"stage": "download_body_html", "register_id": register_id, "title": title, "url": body_urls[0], "reason": "; ".join(failed) or "no_body_downloaded"})
            return ""
        combined = "\n".join(bodies)
        path.write_text(combined, encoding="utf-8")
        return body_urls[0]

    @staticmethod
    def _looks_like_frl_body(html_text: str) -> bool:
        return "ActHead" in html_text or 'class="subsection' in html_text or "class='subsection" in html_text

    @staticmethod
    def _body_document_urls(wrapper_html: str, *, fallback_base: str) -> list[str]:
        soup = BeautifulSoup(wrapper_html, "html.parser")
        urls: list[str] = []
        frame = soup.find("iframe", src=True)
        if frame and "document_" in str(frame["src"]):
            urls.append(urljoin(fallback_base, str(frame["src"])))
        for link in soup.find_all("a", href=True):
            href = str(link["href"])
            if "/epub/" in href and "document_" in href and href.endswith(".html"):
                urls.append(urljoin(fallback_base, href))
        out = []
        seen = set()
        for url in urls:
            if url not in seen:
                seen.add(url)
                out.append(url)
        return out

    @staticmethod
    def _body_document_url(wrapper_html: str, *, fallback_base: str) -> str:
        urls = AustraliaFRLCorpusBuilder._body_document_urls(wrapper_html, fallback_base=fallback_base)
        return urls[0] if urls else ""

    def _download_binary(self, url: str, path: Path, *, expect: str, title: str = "", register_id: str = "") -> dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= (MIN_PDF_BYTES if expect == "pdf" else MIN_HTML_BYTES) and not self.force:
            with self._state_lock:
                self.cache_hits += 1
            return {"status": "cache_hit", "url": url, "path": str(path), "sha256": sha256_file(path), "file_size": path.stat().st_size}
        response = None
        last_error = ""
        for attempt in range(3):
            try:
                response = self.session.get(url, timeout=FRL_REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return {"status": "failed", "url": url, "path": str(path), "reason": last_error, "error_type": "network_error", "attempts": attempt + 1}
            if response.status_code in {403, 404}:
                break
            if response.status_code in FRL_RETRY_STATUS_CODES and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            break
        if response is None:
            return {"status": "failed", "url": url, "path": str(path), "reason": last_error or "no_response", "error_type": "network_error"}
        content = response.content
        content_type = response.headers.get("Content-Type", "")
        if response.status_code != 200:
            return {"status": "failed", "url": url, "path": str(path), "http_status": response.status_code, "content_type": content_type, "reason": "non_200"}
        if expect == "pdf":
            if not content.startswith(b"%PDF") or b"<html" in content[:500].lower():
                return {"status": "failed", "url": url, "path": str(path), "http_status": response.status_code, "content_type": content_type, "reason": "invalid_pdf_magic"}
            if len(content) < MIN_PDF_BYTES:
                return {"status": "failed", "url": url, "path": str(path), "http_status": response.status_code, "content_type": content_type, "reason": "pdf_too_small"}
        elif expect == "html":
            lowered = content[:500].lower()
            if b"<html" not in lowered and b"<!doctype html" not in lowered:
                return {"status": "failed", "url": url, "path": str(path), "http_status": response.status_code, "content_type": content_type, "reason": "invalid_html_magic"}
            if len(content) < MIN_HTML_BYTES:
                return {"status": "failed", "url": url, "path": str(path), "http_status": response.status_code, "content_type": content_type, "reason": "html_too_small"}
            text = content.decode("utf-8", errors="replace")
            if register_id and register_id not in text and title and title.casefold() not in text.casefold():
                return {"status": "failed", "url": url, "path": str(path), "http_status": response.status_code, "content_type": content_type, "reason": "title_or_register_id_not_found"}
        elif len(content) < 1:
            return {"status": "failed", "url": url, "path": str(path), "http_status": response.status_code, "content_type": content_type, "reason": "empty_binary"}
        path.write_bytes(content)
        return {"status": "success", "url": url, "path": str(path), "sha256": sha256_bytes(content), "file_size": len(content), "content_type": content_type, "http_status": response.status_code}

    def _get_text(self, url: str) -> str:
        response = self._get_with_retries(url)
        response.raise_for_status()
        return response.text

    def _get_with_retries(self, url: str, *, accept: str | None = None) -> requests.Response:
        last_exc: requests.RequestException | None = None
        headers = {"Accept": accept} if accept else None
        for attempt in range(3):
            try:
                response = self.session.get(url, timeout=FRL_REQUEST_TIMEOUT, headers=headers)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
            if response.status_code in {403, 404}:
                return response
            if response.status_code in FRL_RETRY_STATUS_CODES and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            return response
        if last_exc:
            raise last_exc
        raise requests.RequestException(f"failed to fetch {url}")

    def _http_ok(self, url: str) -> bool:
        try:
            response = self.session.get(url, timeout=FRL_REQUEST_TIMEOUT)
            return response.status_code == 200 and bool(response.content)
        except requests.RequestException:
            return False

    def _head_or_get_ok(self, url: str, *, expect_pdf: bool = False) -> bool:
        try:
            response = self.session.get(url, timeout=FRL_REQUEST_TIMEOUT, stream=True)
            chunk = next(response.iter_content(16), b"")
            return response.status_code == 200 and ((not expect_pdf) or chunk.startswith(b"%PDF"))
        except requests.RequestException:
            return False

    def _first_pdf_url(self, downloads_html: str, base_url: str) -> str:
        soup = BeautifulSoup(downloads_html, "html.parser")
        for link in soup.find_all("a", href=True):
            text = normalise_text(link.get_text(" ", strip=True)).casefold()
            href = urljoin(base_url, link["href"])
            if href.casefold().endswith("/pdf") or text.endswith(".pdf") or "/pdf" in href.casefold():
                return href
        return ""

    def _parse_details(self, details_url: str, title: FRLTitle) -> dict[str, str]:
        try:
            page = self._get_text(details_url)
        except requests.RequestException:
            return {}
        text = normalise_text(BeautifulSoup(page, "html.parser").get_text(" ", strip=True))
        out: dict[str, str] = {"status": "InForce" if " In force " in f" {text} " else title.status}
        pairs = {
            "registered_date": r"Registered\s+([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
            "effective_from": r"Effective\s+([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})",
            "series": r"Series\s+(.+?)\s+Type\s+",
            "type": r"Type\s+([A-Za-z ]+)",
        }
        for key, pattern in pairs.items():
            match = re.search(pattern, text)
            if match:
                out[key] = normalise_text(match.group(1))
        comp = re.search(r"\b(C\d{4}C\d{5})\s+(C\d+)\s+([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})", text)
        if comp:
            out["compilation_id"] = comp.group(1)
            out["compilation_number"] = comp.group(2)
            out.setdefault("effective_from", comp.group(3))
        admin = re.search(r"Administered by\s+(.+?)\s+Latest version", text)
        if admin:
            out["administering_department"] = normalise_text(admin.group(1))
        return out

    def _parse_versions(self, versions_url: str) -> list[dict[str, Any]]:
        try:
            page = self._get_text(versions_url)
        except requests.RequestException:
            return []
        soup = BeautifulSoup(page, "html.parser")
        rows: list[dict[str, Any]] = []
        for link in soup.find_all("a", href=True):
            href = urljoin(versions_url, link["href"])
            txt = normalise_text(link.get_text(" ", strip=True))
            if re.match(r"^[0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4}(?:\s+-\s+.*)?$", txt):
                rows.append({"label": txt, "url": href, "pending": "future" in txt.casefold() or "not yet" in txt.casefold()})
        return rows[:200]

    def _normalise_html_document(self, html_text: str) -> str:
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        if self._looks_like_frl_body(str(soup)):
            paragraphs = []
            seen_body = False
            for p_tag in soup.find_all("p"):
                classes = set(p_tag.get("class") or [])
                text = normalise_text(p_tag.get_text(" ", strip=True))
                if not text:
                    continue
                if any(str(cls).startswith("TOC") for cls in classes):
                    continue
                if "ActHead5" in classes or "ActHead6" in classes:
                    seen_body = True
                if seen_body or classes.intersection({"ActHead1", "ActHead2", "ActHead3", "ActHead4", "LongT", "subsection", "paragraph", "subparagraph", "definition", "notetext"}):
                    paragraphs.append(text)
            if paragraphs:
                return normalise_text("\n".join(paragraphs))
        main = soup.select_one("main") or soup.body or soup
        return normalise_text(main.get_text(" ", strip=True))

    def _extract_section_headings(self, html_text: str) -> list[str]:
        soup = BeautifulSoup(html_text, "html.parser")
        headings: list[str] = []
        for link in soup.find_all("a", href=True):
            text = normalise_text(link.get_text(" ", strip=True))
            if re.match(r"^(?:[0-9]+[A-Z]*|Part\s+|Chapter\s+|Division\s+|Schedule\s+)", text):
                headings.append(text)
        seen: set[str] = set()
        deduped = []
        for heading in headings:
            if heading not in seen:
                seen.add(heading)
                deduped.append(heading)
        return deduped

    def _download_official_guidance(self) -> None:
        for url in OAIC_SOURCES:
            self._download_guidance_page(url, source_type="official_regulatory_guidance", metadata={
                "legally_binding": False,
                "primary_legislation": False,
                "usable_for_interpretation": True,
                "usable_as_legal_basis": False,
            })
        self._download_guidance_page(CYBER_STRATEGY_URL, source_type="government_strategy", metadata={
            "legally_binding": False,
            "possible_indicator": "P7-I2",
            "do_not_treat_as_statutory_obligation": True,
        })

    def _download_guidance_page(self, url: str, *, source_type: str, metadata: dict[str, Any]) -> None:
        stem = slug(urlsplit(url).path) or slug(url)
        raw = self.data_root / "raw" / "official_guidance" / "html" / f"{stem}.html"
        norm = self.data_root / "normalized" / "official_guidance" / f"{stem}.txt"
        meta = self.data_root / "metadata" / "official_guidance" / f"{stem}.json"
        try:
            response = self.session.get(url, timeout=60)
            if response.status_code != 200:
                self.failures.append({"stage": "guidance_html", "url": url, "http_status": response.status_code, "reason": "non_200"})
                return
        except requests.RequestException as exc:
            self.failures.append({"stage": "guidance_html", "url": url, "reason": str(exc)})
            return
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(response.content)
        text = self._normalise_html_document(response.text)
        norm.parent.mkdir(parents=True, exist_ok=True)
        norm.write_text(text + "\n", encoding="utf-8")
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else stem
        pdfs = []
        for link in soup.find_all("a", href=True):
            href = urljoin(url, link["href"])
            if ".pdf" in href.casefold() or link.get_text(" ", strip=True).casefold().endswith("pdf"):
                if "oaic.gov.au" in urlsplit(href).netloc or "homeaffairs.gov.au" in urlsplit(href).netloc:
                    pdfs.append(href)
        pdf_records = []
        for pdf_url in sorted(set(pdfs))[:5]:
            pdf_stem = slug(Path(urlsplit(pdf_url).path).stem)[:36]
            pdf_hash = hashlib.sha256(pdf_url.encode("utf-8")).hexdigest()[:10]
            pdf_path = self.data_root / "raw" / "official_guidance" / "pdf" / f"{stem[:40]}-{pdf_stem}-{pdf_hash}.pdf"
            pdf_info = self._download_binary(pdf_url, pdf_path, expect="pdf")
            if pdf_info["status"] in {"success", "cache_hit"}:
                pdf_records.append(pdf_info)
            else:
                self.failures.append({**pdf_info, "stage": "guidance_pdf", "source_page": url})
        row = {
            "economy": "Australia",
            "title": normalise_text(title),
            "source_url": url,
            "source_type": source_type,
            "official": True,
            "retrieved_at": utc_now(),
            "sha256": sha256_file(raw),
            "raw_file_path": str(raw),
            "normalized_file_path": str(norm),
            "metadata_path": str(meta),
            "normalized_char_count": len(text),
            "pdf_downloads": pdf_records,
            **metadata,
        }
        write_json(meta, row)
        self.downloaded.append(row)

    def _write_treaty_status_seeds(self) -> None:
        path = self.data_root / "metadata" / "australia_treaty_status_seeds.json"
        rows = []
        for seed in DFAT_TREATY_SEEDS:
            rows.append({
                "agreement": seed["agreement"],
                "economy": "Australia",
                "official_status_source": seed["official_status_source"],
                "source_authority": "Department of Foreign Affairs and Trade",
                "source_type": "official_treaty_status_seed",
                "retrieved_at": utc_now(),
                "p6_i5_mapping_run": False,
            })
        write_json(path, {"generated_at": utc_now(), "seeds": rows})

    def _load_downloaded_manifest(self) -> list[dict[str, Any]]:
        path = self.data_root / "manifests" / "australia_downloaded_manifest.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def _metadata_for_register_id(self, register_id: str, fallback: dict[str, Any]) -> dict[str, Any]:
        path = self.data_root / "metadata" / "frl" / f"{register_id}.json"
        if not path.exists():
            return fallback
        try:
            return json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            return fallback

    @staticmethod
    def _document_id(register_id: str, *, is_act: bool) -> str:
        return f"au-{'act' if is_act else 'li'}-{register_id.casefold()}"

    def _extract_provision_rows(self, metadata: dict[str, Any], html_text: str, body_url: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html_text, "html.parser")
        title = str(metadata.get("title") or metadata.get("official_title") or "")
        source_url = str(metadata.get("latest_version_url") or metadata.get("source_url") or "")
        hierarchy: list[str] = []
        rows: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        body_seen = False
        seen_ids: dict[str, int] = {}

        def flush() -> None:
            nonlocal current
            if not current:
                return
            body = normalise_text("\n".join(current.pop("_body", [])))
            heading = current["article"]
            text = normalise_text(f"{heading}\n{body}") if body else heading
            if len(text) >= 20:
                current["text"] = text
                rows.append(current)
            current = None

        for p_tag in soup.find_all("p"):
            classes = set(p_tag.get("class") or [])
            text = normalise_text(p_tag.get_text(" ", strip=True))
            if not text or any(str(cls).startswith("TOC") for cls in classes) or "Header" in classes:
                continue
            if body_seen and text.casefold().startswith("endnotes"):
                break
            if classes.intersection({"ActHead1", "ActHead2", "ActHead3", "ActHead4"}):
                flush()
                if _is_hierarchy_heading(text):
                    hierarchy = _updated_hierarchy(hierarchy, text)
                continue
            if classes.intersection({"ActHead5", "ActHead6"}) and _is_provision_heading(text):
                flush()
                body_seen = True
                provision_number = _provision_number(text)
                base_id = _normalise_provision_id(provision_number or text)
                seen_ids[base_id] = seen_ids.get(base_id, 0) + 1
                provision_id = base_id if seen_ids[base_id] == 1 else f"{base_id}-{seen_ids[base_id]}"
                anchor = p_tag.get("id") or ""
                current = {
                    "economy": "Australia",
                    "law_id": str(metadata.get("register_id") or "").casefold(),
                    "official_title": title,
                    "status": metadata.get("status") or "InForce",
                    "canonical_url": source_url,
                    "provision_id": provision_id,
                    "hierarchy": list(hierarchy),
                    "article": text,
                    "anchor_url": f"{body_url or source_url}#{anchor}" if anchor else (body_url or source_url),
                    "provision_type": "section",
                    "provision_number": provision_number or provision_id,
                    "_body": [],
                }
                continue
            if not body_seen or current is None:
                continue
            if classes.intersection({"subsection", "paragraph", "subparagraph", "definition", "notetext", "notes", "item", "subitem"}) or text:
                current["_body"].append(text)
        flush()
        return rows

    def _rows_from_normalized_text(self, metadata: dict[str, Any], *, fallback_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        norm_path_value = str(metadata.get("normalized_file_path") or metadata.get("local_path") or "")
        norm_path = self._existing_path(norm_path_value)
        if norm_path is None:
            register_id = str(metadata.get("register_id") or "").strip()
            candidate = self.data_root / "normalized" / "frl" / f"{register_id}.txt"
            norm_path = candidate if candidate.exists() and candidate.stat().st_size > 0 else None
        if norm_path is None:
            return fallback_rows
        text = norm_path.read_text(encoding="utf-8", errors="replace")
        sections = self._split_normalized_text_sections(text)
        if len(sections) <= 1 and fallback_rows:
            return fallback_rows
        title = str(metadata.get("title") or metadata.get("official_title") or "")
        source_url = str(metadata.get("latest_version_url") or metadata.get("source_url") or "")
        seen_ids: dict[str, int] = {}
        rows: list[dict[str, Any]] = []
        hierarchy: list[str] = []
        for section in sections:
            heading = normalise_text(str(section.get("heading") or ""))
            if not heading:
                continue
            if _is_hierarchy_heading(heading):
                hierarchy = _updated_hierarchy(hierarchy, heading)
                continue
            body = normalise_text("\n".join(str(item) for item in section.get("body") or []))
            text_value = normalise_text(f"{heading}\n{body}") if body else heading
            if len(text_value) < 20:
                continue
            provision_number = _provision_number(heading)
            base_id = _normalise_provision_id(provision_number or heading)
            seen_ids[base_id] = seen_ids.get(base_id, 0) + 1
            provision_id = base_id if seen_ids[base_id] == 1 else f"{base_id}-{seen_ids[base_id]}"
            rows.append({
                "economy": "Australia",
                "law_id": str(metadata.get("register_id") or "").casefold(),
                "official_title": title,
                "status": metadata.get("status") or "InForce",
                "canonical_url": source_url,
                "provision_id": provision_id,
                "hierarchy": list(hierarchy),
                "article": heading,
                "anchor_url": source_url,
                "provision_type": "section",
                "provision_number": provision_number or provision_id,
                "text": text_value,
            })
        return rows or fallback_rows

    def _write_reports(self, targets: dict[str, FRLTitle]) -> dict[str, Any]:
        self.downloaded = self._dedupe_downloaded(self.downloaded)
        available_ids = {str(item.get("register_id") or item.get("official_id") or "") for item in self.downloaded if item.get("register_id") or item.get("official_id")}
        failed_ids = set(self.document_failures) - available_ids
        self.document_failures = {key: value for key, value in self.document_failures.items() if key in failed_ids}
        acts_discovered = sum(1 for item in self.full_catalogue.values() if item.collection == "Act")
        li_discovered = sum(1 for item in self.full_catalogue.values() if item.collection == "LegislativeInstrument")
        ni_discovered = sum(1 for item in self.full_catalogue.values() if item.collection == "NotifiableInstrument")
        acts_downloaded = sum(1 for item in self.downloaded if item.get("document_type") == "act")
        li_downloaded = sum(1 for item in self.downloaded if item.get("collection") == "LegislativeInstrument" or (item.get("register_id", "").startswith("F") and item.get("collection") != "NotifiableInstrument"))
        ni_downloaded = sum(1 for item in self.downloaded if item.get("collection") == "NotifiableInstrument")
        collection_coverage: dict[str, dict[str, int]] = {}
        for collection in ("Act", "LegislativeInstrument", "NotifiableInstrument"):
            discovered_ids = {item.register_id for item in self.full_catalogue.values() if item.collection == collection}
            available = discovered_ids & available_ids
            failed = discovered_ids & failed_ids
            collection_coverage[collection] = {
                "discovered": len(discovered_ids),
                "available": len(available),
                "failed": len(failed),
            }
        html_success = sum(1 for item in self.downloaded if item.get("normalized_char_count", 0) > 0)
        pdf_success = sum(1 for item in self.downloaded if item.get("pdf_download_status") in {"success", "cache_hit"} or item.get("pdf_downloads"))
        existing_inventory: set[str] = set()
        inv_path = self.project_root / "data" / "legal_inventory.jsonl"
        if inv_path.exists():
            for line in inv_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip():
                    existing_inventory.add(line.casefold())
        new_sources = [item for item in self.full_catalogue.values() if item.register_id.casefold() not in existing_inventory and item.title.casefold() not in existing_inventory]
        coverage = {
            "generated_at": utc_now(),
            "acts_discovered": acts_discovered,
            "acts_downloaded": acts_downloaded,
            "legislative_instruments_discovered": li_discovered,
            "legislative_instruments_downloaded": li_downloaded,
            "notifiable_instruments_discovered": ni_discovered,
            "notifiable_instruments_downloaded": ni_downloaded,
            "current_in_force_documents": len(self.full_catalogue),
            "documents_discovered": len(self.full_catalogue),
            "documents_downloaded": len(available_ids) - self.documents_skipped_existing if len(available_ids) >= self.documents_skipped_existing else len(available_ids),
            "documents_skipped_existing": self.documents_skipped_existing,
            "documents_failed": len(failed_ids),
            "documents_pending": max(len(self.full_catalogue) - len(available_ids) - len(failed_ids), 0),
            "failure_events": len(self.failures),
            "optional_failures": sum(1 for failure in self.failures if str(failure.get("register_id") or "") in available_ids),
            "collection_coverage": collection_coverage,
            "format_success": dict(self.format_success),
            "pending_documents": sum(len((d.get("pending_versions") or [])) for d in self.downloaded if isinstance(d, dict)),
            "repealed_documents_excluded": "not_downloaded_by_status_filter",
            "duplicate_documents": len(self.duplicates),
            "failed_documents": len(failed_ids),
            "html_normalization_success": html_success,
            "pdf_normalization_success": pdf_success,
            "authorised_instruments_discovered": sum(len(v) for v in self.authorised_instruments.values()),
            "documents_not_present_in_existing_legal_inventory": len(new_sources),
            "full_download_scope_enabled": self.download_all,
            "download_targets_this_run": len(targets),
        }
        summary = {
            "economy": "Australia",
            "zone": 1,
            "generated_at": coverage["generated_at"],
            "acts_discovered": acts_discovered,
            "legislative_instruments_discovered": li_discovered,
            "notifiable_instruments_discovered": ni_discovered,
            "documents_downloaded": len(self.downloaded),
            "documents_discovered": len(self.full_catalogue),
            "documents_skipped_existing": self.documents_skipped_existing,
            "documents_failed": len(failed_ids),
            "documents_pending": max(len(self.full_catalogue) - len(available_ids) - len(failed_ids), 0),
            "failure_events": len(self.failures),
            "optional_failures": sum(1 for failure in self.failures if str(failure.get("register_id") or "") in available_ids),
            "acts_downloaded": acts_downloaded,
            "legislative_instruments_downloaded": li_downloaded,
            "notifiable_instruments_downloaded": ni_downloaded,
            "official_guidance_downloaded": sum(1 for item in self.downloaded if item.get("source_type") in {"official_regulatory_guidance", "government_strategy"}),
            "failed_downloads": len(failed_ids),
            "collection_coverage": collection_coverage,
            "format_success": dict(self.format_success),
            "cache_hits": self.cache_hits,
            "mapper_or_reviewer_run": False,
            "singapore_outputs_modified": False,
            "data_root": str(self.data_root),
            "output_root": str(self.output_root),
        }
        report = {**summary, "coverage": coverage, "failures_by_reason": self._failure_counts()}
        downloaded_manifest = sorted(self.downloaded, key=lambda x: (str(x.get("document_type", x.get("source_type", ""))), str(x.get("title", "")).casefold()))
        write_jsonl(self.data_root / "manifests" / "australia_downloaded_manifest.jsonl", downloaded_manifest)
        write_jsonl(self.output_root / "australia_source_manifest.jsonl", downloaded_manifest)
        write_json(self.output_root / "australia_download_report.json", report)
        write_json(self.output_root / "download_report.json", report)
        write_json(self.output_root / "australia_corpus_summary.json", summary)
        write_json(self.output_root / "corpus_summary.json", summary)
        write_json(self.output_root / "australia_source_coverage_report.json", coverage)
        write_json(self.output_root / "source_coverage_report.json", coverage)
        final_failures = sorted(self.document_failures.values(), key=lambda row: str(row.get("title_id", "")))
        write_jsonl(self.output_root / "australia_failed_downloads.jsonl", final_failures)
        write_jsonl(self.output_root / "failed_downloads.jsonl", final_failures)
        write_json(self.output_root / "australia_new_sources_beyond_inventory.json", {
            "generated_at": utc_now(),
            "count": len(new_sources),
            "sources": [item.to_manifest() for item in new_sources[:1000]],
            "truncated": len(new_sources) > 1000,
        })
        write_json(self.data_root / "manifests" / "australia_authorises_manifest.json", self.authorised_instruments)
        write_json(self.data_root / "manifests" / "australia_corpus_summary.json", summary)
        return report

    def _failure_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for failure in self.failures:
            key = str(failure.get("reason") or failure.get("error_type") or failure.get("stage") or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return counts
