"""Official treaty source library for P6-I5 external-status checks."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


OFFICIAL_DOMAINS = (
    "mfat.govt.nz",
    "mti.gov.sg",
    "rcepsec.org",
    "asean.org",
    "dfat.gov.au",
    "international.gc.ca",
    "trade.govt.nz",
    "metij.go.jp",
    "commerce.govt.nz",
)

AGREEMENT_SEEDS = {
    "CPTPP": (
        "https://www.mfat.govt.nz/en/trade/free-trade-agreements/free-trade-agreements-in-force/cptpp",
        "https://www.mfat.govt.nz/en/trade/free-trade-agreements/free-trade-agreements-in-force/cptpp/comprehensive-and-progressive-agreement-for-trans-pacific-partnership-text-and-resources",
        "https://www.mti.gov.sg/Trade/Free-Trade-Agreements/CPTPP",
        "https://www.mfat.govt.nz/assets/Trade-agreements/TPP/Text-ENGLISH/14.-Electronic-Commerce-Chapter.pdf",
    ),
    "RCEP": (
        "https://rcepsec.org/legal-text/",
        "https://asean.org/wp-content/uploads/2024/10/Regional-Comprehensive-Economic-Partnership-RCEP-Agreement-Full-Text.pdf",
        "https://www.mti.gov.sg/trade-international-economic-relations/agreements/free-trade-agreements-fta/rcep",
    ),
}

LEGAL_LINK_TERMS = (
    "agreement",
    "legal",
    "text",
    "chapter",
    "annex",
    "schedule",
    "reservation",
    "non-conforming",
    "side letter",
    "protocol",
    "accession",
    "decision",
    "commission",
    "suspended",
    "entry into force",
    "in force",
    "ratification",
    "electronic commerce",
    "cross-border",
    "data",
)

DATA_FLOW_TERMS = (
    "cross-border transfer of information by electronic means",
    "cross border transfer of information by electronic means",
    "location of computing facilities",
    "data flows",
    "electronic commerce",
)


@dataclass(frozen=True)
class DownloadedDocument:
    agreement: str
    title: str
    url: str
    raw_path: Path
    normalized_path: Path
    metadata_path: Path
    sha256: str
    content_type: str
    document_type: str
    status_code: int
    size: int
    warning: str | None = None


def ensure_treaty_library(
    project_root: Path,
    *,
    report_dir: Path | None = None,
    timeout: int = 20,
) -> dict:
    root = project_root / "data" / "legal_sources" / "international_agreements"
    report = {
        "generated_at": _now(),
        "root": str(root),
        "downloaded": [],
        "reused": [],
        "failed": [],
        "registry_path": str(root / "treaty_registry.json"),
    }
    root.mkdir(parents=True, exist_ok=True)
    for agreement in ("CPTPP", "RCEP"):
        for child in ("raw", "normalized", "metadata"):
            (root / agreement / child).mkdir(parents=True, exist_ok=True)
        documents = _download_agreement_documents(root, agreement, timeout=timeout, report=report)
        _write_manifest(root, agreement, documents, report)
    registry = _build_treaty_registry(root, report)
    _write_json(root / "treaty_registry.json", registry)
    report["registry"] = registry
    _write_json(root / "treaty_download_report.json", report)
    if report_dir is not None:
        _write_json(report_dir / "treaty_download_report.json", report)
    return report


def load_treaty_registry(project_root: Path) -> dict | None:
    path = project_root / "data" / "legal_sources" / "international_agreements" / "treaty_registry.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _download_agreement_documents(root: Path, agreement: str, *, timeout: int, report: dict) -> list[DownloadedDocument]:
    session = requests.Session()
    session.headers.update({"User-Agent": "rdtii-tool/1.0 official-source-archiver"})
    queue = list(AGREEMENT_SEEDS[agreement])
    seen: set[str] = set()
    documents: list[DownloadedDocument] = []
    sha_seen: dict[str, DownloadedDocument] = {}
    max_urls = 12
    while queue and len(seen) < max_urls:
        url = queue.pop(0)
        if url in seen or not _official_url(url):
            continue
        seen.add(url)
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True, stream=True)
        except requests.RequestException as exc:
            report["failed"].append({"agreement": agreement, "url": url, "reason": str(exc)})
            continue
        final_url = response.url
        if response.status_code >= 400:
            report["failed"].append({"agreement": agreement, "url": url, "status_code": response.status_code, "reason": "http_error"})
            continue
        try:
            content = _read_response_content(response)
        except RuntimeError as exc:
            report["failed"].append({"agreement": agreement, "url": final_url, "status_code": response.status_code, "reason": str(exc)})
            continue
        content_type = response.headers.get("content-type", "").split(";")[0].strip().casefold()
        kind = _detect_kind(content, content_type, final_url)
        if kind == "invalid":
            report["failed"].append({"agreement": agreement, "url": final_url, "reason": "invalid_file_type_or_empty_response", "content_type": content_type, "size": len(content)})
            continue
        if kind == "html":
            for link in _extract_legal_links(final_url, content, agreement):
                if len(seen) + len(queue) >= max_urls:
                    break
                if link not in seen and link not in queue:
                    queue.append(link)
        title = _title_from_response(final_url, content, kind)
        doc = _store_document(root, agreement, final_url, title, content, content_type, kind, response.status_code)
        if doc.sha256 in sha_seen:
            report["reused"].append({"agreement": agreement, "url": final_url, "duplicate_of": str(sha_seen[doc.sha256].raw_path), "sha256": doc.sha256})
            continue
        sha_seen[doc.sha256] = doc
        documents.append(doc)
        report["downloaded"].append({"agreement": agreement, "title": title, "url": final_url, "file_path": str(doc.raw_path), "sha256": doc.sha256, "document_type": doc.document_type, "warning": doc.warning})
    if queue:
        report["failed"].append({"agreement": agreement, "reason": "official_link_scan_limited", "remaining_url_count": len(queue), "limit": max_urls})
    return documents


def _read_response_content(response: requests.Response, *, max_bytes: int = 80_000_000) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=256_000):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise RuntimeError("file_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _official_url(url: str) -> bool:
    host = urlparse(url).netloc.casefold()
    return any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_DOMAINS)


def _extract_legal_links(base_url: str, content: bytes, agreement: str) -> list[str]:
    soup = BeautifulSoup(content, "html.parser")
    out: list[str] = []
    for anchor in soup.find_all("a", href=True):
        label = " ".join(anchor.get_text(" ").split()).casefold()
        href = str(anchor["href"])
        url = urljoin(base_url, href)
        lower = f"{url} {label}".casefold()
        if agreement.casefold() not in lower and ("tpp" not in lower if agreement == "CPTPP" else "rcep" not in lower):
            continue
        if not any(term in lower for term in LEGAL_LINK_TERMS):
            continue
        if _official_url(url):
            out.append(url)
    return out[:120]


def _detect_kind(content: bytes, content_type: str, url: str) -> str:
    if len(content) < 64:
        return "invalid"
    lower_url = url.casefold()
    head = content[:1024].lstrip().lower()
    if content.startswith(b"%PDF"):
        return "pdf"
    if content.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"<!doctype html") or head.startswith(b"<html") or "text/html" in content_type:
        return "html"
    if "application/pdf" in content_type or lower_url.endswith(".pdf"):
        return "invalid"
    if lower_url.endswith((".txt", ".csv")) or content_type.startswith("text/"):
        return "text"
    if lower_url.endswith((".doc", ".docx", ".xlsx", ".xls")):
        return "binary"
    return "binary"


def _store_document(
    root: Path,
    agreement: str,
    url: str,
    title: str,
    content: bytes,
    content_type: str,
    kind: str,
    status_code: int,
) -> DownloadedDocument:
    sha = hashlib.sha256(content).hexdigest()
    stem = _safe_name(title or Path(urlparse(url).path).name or sha[:12])
    suffix = _suffix_for_kind(kind, url)
    raw_path = root / agreement / "raw" / f"{stem}-{sha[:12]}{suffix}"
    normalized_path = root / agreement / "normalized" / f"{stem}-{sha[:12]}.txt"
    metadata_path = root / agreement / "metadata" / f"{stem}-{sha[:12]}.json"
    if not raw_path.exists():
        raw_path.write_bytes(content)
    normalized_text, warning = _normalise_document_text(content, kind, url, title)
    normalized_path.write_text(normalized_text, encoding="utf-8")
    metadata = {
        "agreement": agreement,
        "title": title,
        "document_type": _document_type(title, url, kind),
        "chapter": _extract_chapter(title, url),
        "annex": _extract_annex(title, url),
        "economy": _extract_economy(title, url),
        "language": "en",
        "official_source_url": url,
        "retrieved_at": _now(),
        "signature_date": None,
        "effective_date": _extract_effective_date(normalized_text),
        "party_status": _party_status_from_text(normalized_text),
        "sha256": sha,
        "file_path": str(raw_path),
        "normalized_text_path": str(normalized_path),
        "http_status": status_code,
        "content_type": content_type,
        "size_bytes": len(content),
        "source_fallback": None,
        "normalization_warning": warning,
    }
    _write_json(metadata_path, metadata)
    return DownloadedDocument(agreement, title, url, raw_path, normalized_path, metadata_path, sha, content_type, metadata["document_type"], status_code, len(content), warning)


def _normalise_document_text(content: bytes, kind: str, url: str, title: str) -> tuple[str, str | None]:
    warning = None
    if kind == "html":
        soup = BeautifulSoup(content, "html.parser")
        text = soup.get_text("\n")
    elif kind == "text":
        text = content.decode("utf-8", errors="replace")
    elif kind == "zip":
        names = []
        try:
            with zipfile.ZipFile(__import__("io").BytesIO(content)) as archive:
                names = archive.namelist()
                chunks = []
                for name in names[:80]:
                    if name.casefold().endswith((".txt", ".csv", ".xml", ".html", ".htm")):
                        chunks.append(archive.read(name).decode("utf-8", errors="replace"))
                text = "\n\n".join(chunks) if chunks else "\n".join(names)
        except Exception as exc:
            text = ""
            warning = f"zip_normalization_failed: {exc}"
    elif kind == "pdf":
        text, warning = _extract_pdf_text(content)
    else:
        text = ""
        warning = "binary_text_extraction_unavailable"
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    header = f"Title: {title}\nOfficial source: {url}\n"
    if warning:
        header += f"Normalization warning: {warning}\n"
    return header + text + "\n", warning


def _extract_pdf_text(content: bytes) -> tuple[str, str | None]:
    if len(content) <= 25_000_000:
        try:
            from pypdf import PdfReader  # type: ignore
            import io

            logging.getLogger("pypdf").setLevel(logging.ERROR)
            reader = PdfReader(io.BytesIO(content))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            if text.strip():
                return text, None
        except Exception:
            pass
    # Keep large treaty archiving fast and non-blocking. The raw PDF is the
    # legal source of record; normalized text is a lightweight searchable extract.
    fallback = content[:2_000_000].decode("latin-1", errors="ignore")
    strings = re.findall(r"[\x20-\x7E]{20,}", fallback)
    text = "\n".join(strings[:2000])
    return text, "pdf_text_extraction_limited"


def _write_manifest(root: Path, agreement: str, documents: list[DownloadedDocument], report: dict) -> None:
    all_documents: list[dict] = []
    for metadata_path in sorted((root / agreement / "metadata").glob("*.json")):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        all_documents.append(
            {
                "title": metadata.get("title"),
                "document_type": metadata.get("document_type"),
                "official_source_url": metadata.get("official_source_url"),
                "raw_path": metadata.get("file_path"),
                "normalized_path": metadata.get("normalized_text_path"),
                "metadata_path": str(metadata_path),
                "sha256": metadata.get("sha256"),
                "content_type": metadata.get("content_type"),
                "size_bytes": metadata.get("size_bytes"),
                "warning": metadata.get("normalization_warning"),
            }
        )
    manifest = {
        "agreement": agreement,
        "generated_at": _now(),
        "documents": all_documents,
        "document_count": len(all_documents),
        "documents_downloaded_this_run": len(documents),
    }
    _write_json(root / agreement / "manifest.json", manifest)
    if not documents:
        report["failed"].append({"agreement": agreement, "reason": "no_official_documents_downloaded"})


def _build_treaty_registry(root: Path, report: dict | None = None) -> dict:
    registry = {"generated_at": _now(), "agreements": {}}
    for agreement in ("CPTPP", "RCEP"):
        texts = []
        status_sources = []
        document_types = []
        for meta_path in sorted((root / agreement / "metadata").glob("*.json")):
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            document_types.append(str(metadata.get("document_type") or ""))
            norm_path = Path(metadata.get("normalized_text_path", ""))
            if norm_path.exists():
                text = norm_path.read_text(encoding="utf-8")
                texts.append(text)
                status_sources.append(metadata.get("official_source_url"))
        combined = "\n".join(texts)
        data_flow = _data_flow_commitments(combined, agreement)
        source_failures = _source_failures_for_agreement(report, agreement)
        core_complete = bool(data_flow) and _has_core_agreement_text(combined, agreement, document_types)
        supplementary_incomplete = bool(source_failures) or not _has_supplementary_material(document_types)
        status_by_economy = {
            economy: _economy_status_from_text(
                combined,
                agreement,
                economy,
                next((src for src in status_sources if src), None),
                data_flow,
            )
            for economy in ("Singapore", "Australia", "Malaysia")
        }
        status_complete = all(row["status_complete"] for row in status_by_economy.values())
        registry["agreements"][agreement] = {
            "core_complete": core_complete,
            "status_complete": status_complete,
            "supplementary_incomplete": supplementary_incomplete,
            "source_failed": bool(source_failures),
            "source_failures": source_failures,
            "fallback_source": next((src for src in status_sources if src), None) if source_failures else None,
            "data_flow_commitments": data_flow,
            **status_by_economy,
        }
    return registry


def _economy_status_from_text(
    text: str,
    agreement: str,
    economy: str,
    status_source: str | None,
    data_flow: list[dict],
) -> dict:
    folded = text.casefold()
    economy_folded = economy.casefold()
    signatory = economy_folded in folded
    effective_date = _extract_economy_effective_date(text, agreement, economy) or _agreement_default_effective_date(text, agreement, economy)
    in_force = _economy_in_force(text, agreement, economy, effective_date)
    ratified = in_force or bool(
        re.search(rf"{re.escape(economy)}[^.]{{0,160}}ratif|ratif[^.]{{0,160}}{re.escape(economy)}", text, re.I)
    )
    status_complete = bool(signatory and ratified and in_force and effective_date and status_source)
    confidence = "high" if status_complete else ("medium" if signatory and in_force else "low")
    return {
        "signatory": signatory,
        "ratified": bool(ratified),
        "in_force": bool(in_force),
        "effective_date": effective_date,
        "accession_pending": False if in_force else None,
        "official_status_source": status_source,
        "status_confidence": confidence,
        "status_complete": status_complete,
        "data_flow_commitments": data_flow,
    }


def _economy_in_force(text: str, agreement: str, economy: str, effective_date: str | None) -> bool:
    folded = text.casefold()
    if economy.casefold() not in folded:
        return False
    if effective_date:
        return True
    economy_folded = economy.casefold()
    if agreement == "CPTPP":
        return any(
            term in folded
            for term in (
                f"cptpp entered into force for {economy_folded}",
                "cptpp has entered into force",
                f"in force for {economy_folded}",
                f"{economy_folded} ratified",
            )
        )
    return any(
        term in folded
        for term in (
            f"entered into force for {economy_folded}",
            "rcep agreement entered into force",
            f"{economy_folded} ratified",
            f"in force for {economy_folded}",
        )
    )


def _extract_singapore_effective_date(text: str, agreement: str) -> str | None:
    return _extract_economy_effective_date(text, agreement, "Singapore")


def _extract_economy_effective_date(text: str, agreement: str, economy: str) -> str | None:
    economy_re = re.escape(economy)
    patterns = [
        rf"entered into force on (\d{{1,2}}\s+[A-Z][a-z]+\s+\d{{4}}) for [^.]*{economy_re}",
        rf"entered into force for {economy_re}[^.]*?(\d{{1,2}}\s+[A-Z][a-z]+\s+\d{{4}})",
        rf"in force for {economy_re}[^.]*?(\d{{1,2}}\s+[A-Z][a-z]+\s+\d{{4}})",
        rf"{economy_re}\s*\((\d{{1,2}}\s+[A-Z][a-z]+(?:\s+\d{{4}})?)\)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = match.group(1)
            if not re.search(r"\d{4}", value):
                value = f"{value} 2022" if agreement == "RCEP" else value
            return value
    return None


def _agreement_default_effective_date(text: str, agreement: str, economy: str) -> str | None:
    folded = text.casefold()
    economy_folded = economy.casefold()
    if agreement == "RCEP" and economy in {"Singapore", "Australia"} and re.search(r"RCEP Agreement entered into force on 1 January 2022", text, re.I):
        return "1 January 2022"
    if agreement == "RCEP" and economy == "Malaysia" and "malaysia (18 march" in folded:
        return "18 March 2022"
    if agreement == "CPTPP":
        if economy in {"Singapore", "Australia"} and "entered into force on 30 december 2018" in folded and economy_folded in folded:
            return "30 December 2018"
        if economy == "Malaysia" and "malaysia" in folded and any(term in folded for term in ("entered into force for malaysia", "in force for malaysia", "malaysia ratified")):
            return _extract_effective_date(text)
    return None


def _has_core_agreement_text(text: str, agreement: str, document_types: list[str]) -> bool:
    folded = text.casefold()
    if agreement == "CPTPP":
        return ("comprehensive and progressive agreement for trans-pacific partnership" in folded or "trans-pacific partnership" in folded) and bool(document_types)
    return ("regional comprehensive economic partnership" in folded or "rcep agreement" in folded) and bool(document_types)


def _has_supplementary_material(document_types: list[str]) -> bool:
    return any(value in {"annex_or_schedule", "side_letter", "accession_or_protocol", "commission_decision"} for value in document_types)


def _source_failures_for_agreement(report: dict | None, agreement: str) -> list[dict]:
    if not report:
        return []
    failures = []
    for item in report.get("failed", []):
        if item.get("agreement") == agreement:
            failures.append(
                {
                    "url": item.get("url"),
                    "status_code": item.get("status_code"),
                    "reason": item.get("reason"),
                    "fallback_available": True,
                }
            )
    return failures


def _data_flow_commitments(text: str, agreement: str) -> list[dict]:
    folded = text.casefold()
    commitments = []
    for term in DATA_FLOW_TERMS:
        idx = folded.find(term)
        if idx >= 0:
            snippet = text[max(0, idx - 500): idx + 1000]
            commitments.append({
                "article_hint": "CPTPP Article 14.11/14.13" if agreement == "CPTPP" else "RCEP Article 12.14/12.15",
                "term": term,
                "binding_language_present": " shall " in f" {snippet.casefold()} ",
                "snippet": snippet[:1200],
            })
    return commitments


def _title_from_response(url: str, content: bytes, kind: str) -> str:
    if kind == "html":
        soup = BeautifulSoup(content, "html.parser")
        if soup.title and soup.title.string:
            return " ".join(soup.title.string.split())[:180]
    name = Path(urlparse(url).path).name
    return name or hashlib.sha256(content).hexdigest()[:16]


def _document_type(title: str, url: str, kind: str) -> str:
    text = f"{title} {url}".casefold()
    if "side" in text and "letter" in text:
        return "side_letter"
    if "annex" in text or "schedule" in text or "reservation" in text:
        return "annex_or_schedule"
    if "protocol" in text or "accession" in text:
        return "accession_or_protocol"
    if "decision" in text or "commission" in text:
        return "commission_decision"
    if "status" in text or "ratif" in text or "in-force" in text or "in force" in text:
        return "status_evidence"
    if "chapter" in text or re.search(r"/\d{1,2}[.-]", text):
        return "chapter"
    if kind == "html":
        return "official_web_page"
    return "agreement_text"


def _extract_chapter(title: str, url: str) -> str | None:
    match = re.search(r"(?:chapter|/)(\d{1,2})(?:[.\-_/ ]|$)", f"{title} {url}", re.I)
    return match.group(1) if match else None


def _extract_annex(title: str, url: str) -> str | None:
    match = re.search(r"(annex [A-Z0-9.\-]+|schedule [A-Z0-9.\-]+)", f"{title} {url}", re.I)
    return match.group(1) if match else None


def _extract_economy(title: str, url: str) -> str | None:
    economies = ("Singapore", "New Zealand", "Australia", "Canada", "Japan", "Malaysia", "Viet Nam", "Vietnam", "Brunei", "Chile", "Mexico", "Peru", "United Kingdom", "ASEAN")
    text = f"{title} {url}".casefold()
    for economy in economies:
        if economy.casefold() in text:
            return economy
    return None


def _extract_effective_date(text: str) -> str | None:
    match = re.search(r"(?:entered into force|in force|effective)(?:[^0-9]{0,80})(\d{1,2}\s+[A-Z][a-z]+\s+\d{4}|\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else None


def _party_status_from_text(text: str) -> str | None:
    folded = text.casefold()
    if "entered into force" in folded or "in force" in folded:
        return "in_force"
    if "ratified" in folded:
        return "ratified"
    if "signed" in folded:
        return "signed"
    return None


def _suffix_for_kind(kind: str, url: str) -> str:
    if kind == "pdf":
        return ".pdf"
    if kind == "zip":
        return ".zip"
    if kind == "html":
        return ".html"
    suffix = Path(urlparse(url).path).suffix
    return suffix if suffix else ".bin"


def _safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return (text[:120] or "document")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
