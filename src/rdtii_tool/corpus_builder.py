"""Build Singapore Act corpus and high-recall document candidate mappings."""

from __future__ import annotations

import json
import math
import os
import re
import io
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

from rdtii_tool.document_models import CandidateURL, IngestionInput
from rdtii_tool.downloaders.sso_html import DIRECT_HTML_HEADERS, SSOHTMLDownloader
from rdtii_tool.ingestion.parser_router import ParserRouter


TOKEN = re.compile(r"[a-z0-9]{2,}")


def _tokens(value: str) -> list[str]:
    return TOKEN.findall(value.casefold())


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    _write_jsonl(temporary, rows)
    os.replace(temporary, path)


class _RawStore:
    """Minimal DocumentStore-compatible store preserving required corpus paths."""

    def __init__(self, raw_dir: Path) -> None:
        self.raw_dir = raw_dir

    def path_for(self, candidate: CandidateURL) -> Path:
        return self.raw_dir / f"{candidate.metadata['document_id']}.html"

    def find_cached(self, candidate: CandidateURL) -> Path | None:
        path = self.path_for(candidate)
        return path if path.exists() and path.stat().st_size else None

    def save_html(self, candidate: CandidateURL, content: bytes) -> Path:
        path = self.path_for(candidate)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


@dataclass(slots=True)
class _Processed:
    manifest: dict[str, Any]
    provisions: list[dict[str, Any]]


class _BM25:
    """Small in-memory BM25 implementation; avoids a retrieval framework dependency."""

    def __init__(self, texts: list[str]) -> None:
        self.docs = [Counter(_tokens(text)) for text in texts]
        self.lengths = [sum(doc.values()) for doc in self.docs]
        self.avgdl = sum(self.lengths) / max(len(self.lengths), 1)
        df: Counter[str] = Counter()
        for doc in self.docs:
            df.update(doc.keys())
        count = len(self.docs)
        self.idf = {term: math.log(1 + (count - value + 0.5) / (value + 0.5)) for term, value in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = _tokens(query)
        scores = [0.0] * len(self.docs)
        for index, doc in enumerate(self.docs):
            norm = 1.5 * (1 - 0.75 + 0.75 * self.lengths[index] / max(self.avgdl, 1))
            for term in terms:
                frequency = doc.get(term, 0)
                if frequency:
                    scores[index] += self.idf.get(term, 0.0) * frequency * 2.5 / (frequency + norm)
        return scores


class SingaporeCorpusBuilder:
    """Zone 1 corpus build for all current SSO Acts and Subsidiary Legislation."""

    def __init__(self, output_root: str | Path = "outputs/corpus/singapore", *, force: bool = False) -> None:
        self.root = Path(output_root)
        self.force = force
        self.router = ParserRouter()
        self.failures: list[dict[str, str]] = []
        self._sso_network_gate = threading.Semaphore(2)
        self._sso_backoff_lock = threading.Lock()
        self._sso_backoff_until = 0.0

    def build(self, pillars: set[int]) -> dict[str, int]:
        from rdtii_tool.sources.singapore.sso_adapter import SingaporeSSOAdapter
        from rdtii_tool.zone1.engine import Zone1CorpusEngine

        adapter = SingaporeSSOAdapter(self.root.parents[2], force=self.force)
        Zone1CorpusEngine(adapter=adapter, project_root=self.root.parents[2], workers=8, force=self.force).run()
        summary_path = self.root / "manifests" / "build_summary.json"
        return json.loads(summary_path.read_text(encoding="utf-8"))

    def _build_subsidiary(self, selected: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        manifest_path = self.root / "manifests" / "subsidiary_manifest.jsonl"
        prior = {item["document_id"]: item for item in _read_jsonl(manifest_path)} if manifest_path.exists() else {}
        session = requests.Session(); session.headers.update(DIRECT_HTML_HEADERS)
        downloaded = parsed = partial = failed = blocked = provisions = complete = 0
        stopped = False
        for record in selected:
            document_id = self._document_id(record, "subsidiary_legislation")
            existing = prior.get(document_id)
            path = self.root / "subsidiary_legislation" / f"{document_id}.jsonl"
            if not self.force and existing and existing.get("document_completeness") == "complete" and path.exists() and path.stat().st_size:
                complete += 1; provisions += int(existing.get("provision_count", 0)); continue
            result = self._download_parse_subsidiary(record, session)
            prior[document_id] = result.manifest
            _write_jsonl_atomic(manifest_path, sorted(prior.values(), key=lambda item: item["document_id"]))
            if result.manifest["download_status"] in {"success", "cache_hit"}: downloaded += 1
            if result.manifest["parse_status"] == "success": parsed += 1; provisions += result.manifest["provision_count"]
            elif result.manifest.get("document_completeness") == "partial_document": partial += 1
            else: failed += 1
            if result.manifest["download_status"] == "blocked_467": blocked += 1; stopped = True; break
            time.sleep(1)
        summary = {"subsidiary_catalogued": 5794, "subsidiary_selected": len(selected), "selected_by_parent_relation": sum("parent_act" in item["selection_reasons"] for item in selected), "selected_by_title_bm25": sum(any(reason.startswith("title_bm25") for reason in item["selection_reasons"]) for item in selected), "subsidiary_already_complete": complete, "subsidiary_downloaded_this_run": downloaded, "subsidiary_parsed_successfully": parsed, "subsidiary_partial": partial, "subsidiary_failed": failed, "subsidiary_blocked_467": blocked, "total_subsidiary_provisions": provisions, "stopped_because_of_site_block": stopped}
        path = self.root / "manifests" / "subsidiary_build_summary.json"; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return sorted(prior.values(), key=lambda item: item["document_id"]), summary

    def _download_parse_subsidiary(self, record: dict[str, Any], session: requests.Session) -> _Processed:
        document_id = self._document_id(record, "subsidiary_legislation")
        raw = self.root / "raw" / "subsidiary_legislation" / f"{document_id}.html"
        output = self.root / "subsidiary_legislation" / f"{document_id}.jsonl"
        candidate = CandidateURL(url=record["canonical_url"], normalized_url=record["canonical_url"], title=record["official_title"], country="SG", source_name="Singapore Statutes Online", source_type="legislation", document_type="subsidiary_legislation", metadata={"document_id":document_id})
        result = None
        for attempt in range(3):
            result = SSOHTMLDownloader(_RawStore(raw.parent), session=session, timeout=60.0, fragment_delay=0.1, prefer_cache=False).download(candidate)
            if result.status == "blocked" and result.http_status == 467:
                if attempt == 0: time.sleep(60); continue
                break
            if result.error_type not in {"timeout", "network_error", "content_validation_failed"} or attempt == 2:
                break
            time.sleep((10, 30)[attempt])
        if result.status not in {"success", "cache_hit"}:
            status = "blocked_467" if result.http_status == 467 else "failed"
            return _Processed(self._sl_manifest(record, raw, output, 0, status, "failed", "", result.error_type, result.error_message, result.direct_html_url), [])
        outcome = self.router.route(IngestionInput(input_path=str(raw), source_url=record["canonical_url"], source_name="Singapore Statutes Online", source_type="legislation", document_type="subsidiary_legislation", content_type="text/html", title=record["official_title"]))
        if outcome.document is None or not outcome.document.sections:
            return _Processed(self._sl_manifest(record, raw, output, 0, result.status, "failed", "", "parse_error", outcome.parser_log.error_message, result.direct_html_url), [])
        rows = [self._provision_row(record, "subsidiary_legislation", document_id, section) for section in outcome.document.sections]
        _write_jsonl(output, rows)
        return _Processed(self._sl_manifest(record, raw, output, len(rows), result.status, "success", "complete", "", "", result.direct_html_url), rows)

    def _sl_manifest(self, record: dict[str, Any], raw: Path, output: Path, count: int, download: str, parse: str, completeness: str, error_type: str, error: str, final_url: str) -> dict[str, Any]:
        manifest = self._manifest(record, "subsidiary_legislation", raw, output, count, download, parse, error)
        manifest.update({"document_completeness": completeness, "error_type": error_type, "final_response_url": final_url})
        return manifest

    def build_acts_only(self) -> dict[str, Any]:
        """Acquire full Act print documents sequentially; do not map or touch SL."""
        acts = _read_jsonl(self.root.parent / "sources" / "singapore_sso_current_acts.jsonl")
        manifest_path = self.root / "manifests" / "acts_manifest.jsonl"
        prior = {item["document_id"]: item for item in _read_jsonl(manifest_path)} if manifest_path.exists() else {}
        manifest_by_id = dict(prior)
        session = requests.Session()
        session.headers.update(DIRECT_HTML_HEADERS)
        downloaded = parsed = partial = failed = blocked = provisions = already_complete = retried = 0
        stopped = False
        stopped_by_site_block = False
        abnormal_html = 0
        for record in acts:
            document_id = self._document_id(record, "act")
            existing = prior.get(document_id)
            jsonl_path = self.root / "acts" / f"{document_id}.jsonl"
            if (not self.force and existing and existing.get("document_completeness") == "complete" and jsonl_path.exists() and jsonl_path.stat().st_size):
                already_complete += 1; provisions += int(existing.get("provision_count", 0)); continue
            result = self._download_parse_direct(record, session)
            retried += 1
            manifest_by_id[result.manifest["document_id"]] = result.manifest
            _write_jsonl_atomic(
                manifest_path,
                sorted(manifest_by_id.values(), key=lambda item: item["document_id"]),
            )
            status = result.manifest["download_status"]
            if status in {"success", "cache_hit"}: downloaded += 1
            if result.manifest["parse_status"] == "success": parsed += 1; provisions += result.manifest["provision_count"]
            elif result.manifest.get("document_completeness") == "partial_document": partial += 1
            else: failed += 1
            if status == "blocked_467":
                blocked += 1; stopped = True; stopped_by_site_block = True; break
            if result.manifest.get("error_type") == "abnormal_html":
                abnormal_html += 1
                if abnormal_html >= 2: stopped = True; break
            else: abnormal_html = 0
            time.sleep(1.0)
        summary = {"acts_catalogued":len(acts), "acts_already_complete":already_complete, "acts_retried_this_run":retried, "acts_downloaded_this_run":downloaded, "acts_parsed_successfully":parsed, "acts_partial":partial, "acts_failed":failed, "acts_blocked_467":blocked, "total_provisions":provisions, "stopped_because_of_site_block":stopped_by_site_block, "stopped_early":stopped}
        summary_path = self.root / "manifests" / "acts_build_summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return summary

    def _download_parse_direct(self, record: dict[str, Any], session: requests.Session) -> _Processed:
        document_id = self._document_id(record, "act")
        raw = self.root / "raw" / "acts" / f"{document_id}.html"
        output = self.root / "acts" / f"{document_id}.jsonl"
        candidate = CandidateURL(
            url=record["canonical_url"], normalized_url=record["canonical_url"],
            title=record["official_title"], country="SG",
            source_name="Singapore Statutes Online", source_type="legislation",
            document_type="act", metadata={"document_id": document_id},
        )
        result = None
        for attempt in range(3):
            result = SSOHTMLDownloader(
                _RawStore(raw.parent), session=session, timeout=60.0,
                fragment_delay=0.1, prefer_cache=False,
            ).download(candidate)
            if result.status == "blocked" and result.http_status == 467:
                if attempt == 0:
                    time.sleep(60)
                    continue
                break
            retryable = result.error_type in {
                "timeout", "network_error", "content_validation_failed",
            }
            access_restriction = (
                result.error_type == "content_validation_failed"
                and "access restriction" in result.error_message.casefold()
            )
            if not retryable or attempt == 2:
                break
            if access_restriction:
                time.sleep(30)
                session = requests.Session()
                session.headers.update(DIRECT_HTML_HEADERS)
            else:
                time.sleep((10, 30)[attempt])
        if result is None or result.status not in {"success", "cache_hit"}:
            status = "blocked_467" if result and result.http_status == 467 else "failed"
            return _Processed(self._acts_manifest(record, raw, output, 0, status, "failed", "", result.error_type if result else "request_failed", result.error_message if result else "No download result", result.direct_html_url if result else record["canonical_url"]), [])
        outcome = self.router.route(IngestionInput(input_path=str(raw), source_url=record["canonical_url"], source_name="Singapore Statutes Online", source_type="legislation", document_type="act", content_type="text/html", title=record["official_title"]))
        if outcome.document is None or not outcome.document.sections:
            return _Processed(self._acts_manifest(record, raw, output, 0, result.status, "failed", "", "parse_error", outcome.parser_log.error_message or "No provisions parsed", result.direct_html_url), [])
        parsed_count = len(outcome.document.sections)
        provisions = [self._provision_row(record, "act", document_id, section) for section in outcome.document.sections]
        _write_jsonl(output, provisions)
        return _Processed(self._acts_manifest(record, raw, output, parsed_count, result.status, "success", "complete", "", "", result.direct_html_url), provisions)

    def _acts_manifest(self, record: dict[str, Any], raw: Path, output: Path, count: int, download: str, parse: str, completeness: str, error_type: str, error: str, final_url: str) -> dict[str, Any]:
        manifest = self._manifest(record, "act", raw, output, count, download, parse, error)
        manifest.update({"download_url": record["canonical_url"], "final_response_url": final_url, "document_completeness": completeness, "error_type": error_type})
        return manifest

    def _save_debug_html(self, document_id: str, response: requests.Response, body: bytes, html: str, title: str, url: str) -> None:
        path = self.root / "debug" / f"{document_id}_response.html"; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(body)
        details = (
            f"status_code={response.status_code}\nfinal_url={url}\n"
            f"content_type={response.headers.get('Content-Type', '')}\n"
            f"content_encoding={response.headers.get('Content-Encoding', '')}\n"
            f"content_length={response.headers.get('Content-Length', '')}\n"
            f"apparent_encoding={response.apparent_encoding}\nbody_prefix_hex={body[:32].hex()}\n"
            f"title={title}\ndecoded_preview={html[:300]}\n"
        )
        (self.root / "debug" / f"{document_id}_response.txt").write_text(details, encoding="utf-8")

    @staticmethod
    def _document_id(record: dict[str, Any], kind: str) -> str:
        prefix = "sg-act" if kind == "act" else "sg-sl"
        return f"{prefix}-{str(record['law_id']).casefold()}"

    def _jsonl_dirs(self, kind: str) -> list[Path]:
        if kind == "act":
            return [self.root / "acts"]
        return [self.root / "subsidiary_legislation", self.root / "subsidiary"]

    def _raw_dirs(self, kind: str) -> list[Path]:
        if kind == "act":
            return [self.root / "raw" / "acts"]
        return [self.root / "raw" / "subsidiary_legislation", self.root / "raw" / "subsidiary"]

    def _canonical_jsonl_dir(self, kind: str) -> Path:
        return self._jsonl_dirs(kind)[0]

    def _canonical_raw_dir(self, kind: str) -> Path:
        return self._raw_dirs(kind)[0]

    def _existing_jsonl(self, document_id: str, kind: str) -> Path | None:
        for directory in self._jsonl_dirs(kind):
            path = directory / f"{document_id}.jsonl"
            if path.exists() and path.stat().st_size > 0:
                return path
        return None

    def _existing_raw(self, document_id: str, kind: str) -> Path | None:
        for directory in self._raw_dirs(kind):
            for extension in (".html", ".pdf"):
                path = directory / f"{document_id}{extension}"
                if path.exists() and path.stat().st_size > 0:
                    return path
        return None

    def _process_documents(self, records: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
        manifests: list[dict[str, Any]] = []
        tasks = []
        session = requests.Session()
        session.headers.update(DIRECT_HTML_HEADERS)
        with ThreadPoolExecutor(max_workers=8) as pool:
            for record in records:
                document_id = self._document_id(record, kind)
                output = self._existing_jsonl(document_id, kind)
                if not self.force and output is not None:
                    manifests.append(self._existing_manifest(record, kind, output))
                    continue
                tasks.append(pool.submit(self._download_parse, record, kind, session))
            for task in as_completed(tasks):
                result = task.result()
                manifests.append(result.manifest)
                if result.manifest["error"]:
                    self.failures.append({"document_id": result.manifest["document_id"], "stage": "download_or_parse", "error": result.manifest["error"]})
        manifests.sort(key=lambda item: item["document_id"])
        name = "acts_manifest.jsonl" if kind == "act" else "subsidiary_manifest.jsonl"
        _write_jsonl(self.root / "manifests" / name, manifests)
        return manifests

    def _existing_manifest(self, record: dict[str, Any], kind: str, output: Path) -> dict[str, Any]:
        count = sum(1 for line in output.read_text(encoding="utf-8").splitlines() if line.strip())
        document_id = self._document_id(record, kind)
        raw = self._existing_raw(document_id, kind) or self._canonical_raw_dir(kind) / f"{document_id}.html"
        return self._manifest(record, kind, raw, output, count, "cache_hit", "success", "")

    def _download_parse(self, record: dict[str, Any], kind: str, session: requests.Session) -> _Processed:
        document_id = self._document_id(record, kind)
        raw_dir = self._canonical_raw_dir(kind)
        output = self._canonical_jsonl_dir(kind) / f"{document_id}.jsonl"

        existing_raw = self._existing_raw(document_id, kind)
        if existing_raw is not None:
            parsed = self._parse_existing_raw(record, kind, document_id, existing_raw, output)
            if parsed is not None:
                return parsed

        pdf_result = self._download_parse_pdf(record, kind, document_id, session, output)
        if pdf_result is not None:
            return pdf_result

        candidate = CandidateURL(url=record["canonical_url"], normalized_url=record["canonical_url"], title=record["official_title"], country="SG", source_name="Singapore Statutes Online", source_type="legislation", document_type=kind, metadata={"document_id": document_id})
        result = None
        for attempt in range(3):
            result = SSOHTMLDownloader(
                _RawStore(raw_dir), session=session, timeout=30.0, fragment_delay=0.1
            ).download(candidate)
            temporary = result.error_type in {"timeout", "network_error", "rate_limited_429"}
            if result.status in {"success", "cache_hit"} or not temporary or attempt == 2:
                break
        raw = raw_dir / f"{document_id}.html"
        if result is None or result.status not in {"success", "cache_hit"}:
            return _Processed(self._manifest(record, kind, raw, output, 0, result.status, "failed", result.error_message or result.error_type), [])
        outcome = self.router.route(IngestionInput(input_path=str(raw), source_url=record["canonical_url"], source_name="Singapore Statutes Online", source_type="legislation", document_type=kind, content_type="text/html", title=record["official_title"]))
        if outcome.document is None or outcome.parser_log.parser_status != "success":
            return _Processed(self._manifest(record, kind, raw, output, 0, result.status, outcome.parser_log.parser_status, outcome.parser_log.error_message), [])
        provisions = [self._provision_row(record, kind, document_id, section) for section in outcome.document.sections]
        _write_jsonl(output, provisions)
        return _Processed(self._manifest(record, kind, raw, output, len(provisions), result.status, "success", ""), provisions)

    def _parse_existing_raw(self, record: dict[str, Any], kind: str, document_id: str, raw: Path, output: Path) -> _Processed | None:
        if raw.suffix.casefold() == ".pdf":
            rows = [self._pdf_document_row(record, kind, document_id, raw, record["canonical_url"])]
            _write_jsonl(output, rows)
            manifest = self._manifest(record, kind, raw, output, len(rows), "parsed_from_existing_raw", "success", "")
            manifest.update({"source_format": "pdf", "raw_pdf_path": str(raw)})
            return _Processed(manifest, rows)

        outcome = self.router.route(IngestionInput(input_path=str(raw), source_url=record["canonical_url"], source_name="Singapore Statutes Online", source_type="legislation", document_type=kind, content_type="text/html", title=record["official_title"]))
        if outcome.document is None or outcome.parser_log.parser_status != "success":
            return None
        provisions = [self._provision_row(record, kind, document_id, section) for section in outcome.document.sections]
        _write_jsonl(output, provisions)
        return _Processed(self._manifest(record, kind, raw, output, len(provisions), "parsed_from_existing_raw", "success", ""), provisions)

    def _download_parse_pdf(self, record: dict[str, Any], kind: str, document_id: str, session: requests.Session, output: Path) -> _Processed | None:
        raw_pdf = self._canonical_raw_dir(kind) / f"{document_id}.pdf"
        attempts: list[str] = []
        for pdf_url in self._pdf_url_candidates(record, kind):
            result = self._download_pdf(pdf_url, record["canonical_url"], raw_pdf, session)
            if result["status"] != "success":
                attempts.append(f"{pdf_url}: {result.get('error') or result.get('http_status') or result['status']}")
                if result.get("http_status") in {400, 403, 404}:
                    continue
                continue
            rows = [self._pdf_document_row(record, kind, document_id, raw_pdf, pdf_url)]
            _write_jsonl(output, rows)
            manifest = self._manifest(record, kind, raw_pdf, output, len(rows), "success", "success", "")
            manifest.update({"download_url": pdf_url, "final_response_url": pdf_url, "source_format": "pdf"})
            return _Processed(manifest, rows)
        if attempts:
            self.failures.append({"document_id": document_id, "stage": "pdf_download", "error": "; ".join(attempts[:3])})
        return None

    def _pdf_url_candidates(self, record: dict[str, Any], kind: str) -> list[str]:
        urls: list[str] = []
        for key in ("pdf_url", "download_pdf_url", "download_url"):
            value = str(record.get(key) or "").strip()
            if value:
                urls.append(urljoin(record["canonical_url"], value))
        canonical = str(record["canonical_url"])
        parsed = urlsplit(canonical)
        slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        collection = "Act" if kind == "act" else "SL"
        query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.casefold() != "viewtype"]
        query.append(("ViewType", "Pdf"))
        urls.append(urlunsplit(("https", "sso.agc.gov.sg", f"/{collection}/{slug}", urlencode(query), "")))
        seen: set[str] = set()
        out: list[str] = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                out.append(url)
        return out

    def _download_pdf(self, url: str, referer: str, path: Path, session: requests.Session) -> dict[str, Any]:
        if not self.force and path.exists() and path.stat().st_size > 0:
            return {"status": "success", "path": str(path), "cache_hit": True}
        path.parent.mkdir(parents=True, exist_ok=True)
        headers = dict(DIRECT_HTML_HEADERS)
        headers.update({"Accept": "application/pdf,*/*", "Referer": referer})
        last_error = ""
        for attempt in range(3):
            self._wait_for_sso_backoff()
            with self._sso_network_gate:
                try:
                    response = session.get(url, timeout=60.0, headers=headers, allow_redirects=True)
                except requests.RequestException as exc:
                    last_error = str(exc)
                    response = None
            if response is None:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                return {"status": "failed", "error": last_error or "network_error"}
            status = int(response.status_code)
            if status in {429, 467} or status >= 500:
                self._set_sso_backoff(30 if status == 467 else 5 * (attempt + 1))
                if attempt < 2:
                    continue
            if status in {400, 403, 404} or status != 200:
                return {"status": "failed", "http_status": status, "error": f"HTTP {status}"}
            content = bytes(response.content)
            if not content.startswith(b"%PDF") or b"<html" in content[:500].lower():
                return {"status": "failed", "http_status": status, "error": "invalid_pdf"}
            path.write_bytes(content)
            return {"status": "success", "path": str(path), "http_status": status}
        return {"status": "failed", "error": last_error or "retry_exhausted"}

    def _wait_for_sso_backoff(self) -> None:
        while True:
            with self._sso_backoff_lock:
                wait = self._sso_backoff_until - time.monotonic()
            if wait <= 0:
                return
            time.sleep(min(wait, 5.0))

    def _set_sso_backoff(self, seconds: float) -> None:
        with self._sso_backoff_lock:
            self._sso_backoff_until = max(self._sso_backoff_until, time.monotonic() + seconds)

    @staticmethod
    def _extract_pdf_text(content: bytes) -> str:
        try:
            reader = PdfReader(io.BytesIO(content))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return ""
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _pdf_document_row(self, record: dict[str, Any], kind: str, document_id: str, raw_pdf: Path, source_url: str) -> dict[str, Any]:
        return {
            "record_type": "pdf_document",
            "document_id": document_id,
            "economy": "singapore",
            "collection": "Act" if kind == "act" else "SubsidiaryLegislation",
            "title": record["official_title"],
            "official_number": str(record["law_id"]).casefold(),
            "year": "",
            "language": "en",
            "source_format": "pdf",
            "source_url": source_url,
            "raw_path": str(raw_pdf),
            "prefilter_status": "uncertain",
        }

    def _manifest(self, record: dict[str, Any], kind: str, raw: Path, output: Path, count: int, download: str, parse: str, error: str) -> dict[str, Any]:
        collection = "Act" if kind == "act" else "Subsidiary Legislation"
        return {
            "country": "Singapore",
            "collection": collection,
            "instrument_type": kind,
            "title": record["official_title"],
            "official_title": record["official_title"],
            "official_id": str(record["law_id"]).casefold(),
            "document_id": self._document_id(record, kind),
            "status": record.get("status", "current"),
            "version_id": "current",
            "effective_date": "",
            "source_url": record["canonical_url"],
            "canonical_url": record["canonical_url"],
            "authorising_act": record.get("parent_act", ""),
            "local_path": str(output),
            "raw_html_path": str(raw),
            "jsonl_path": str(output),
            "provision_count": count,
            "download_status": download,
            "parse_status": parse,
            "error": error,
        }

    @staticmethod
    def _provision_row(record: dict[str, Any], kind: str, document_id: str, section: Any) -> dict[str, Any]:
        hierarchy = [value for value in (section.part, section.division, section.schedule) if value]
        return {"economy":"Singapore", "document_id":document_id, "law_id":str(record["law_id"]).casefold(), "official_title":record["official_title"], "instrument_type":kind, "status":"current", "canonical_url":record["canonical_url"], "provision_id":section.section_id, "hierarchy":hierarchy, "article":section.heading, "text":section.text, "anchor_url":section.url, "provision_type":section.provision_type, "provision_number":section.provision_number}

    def _load_provisions(self, manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for item in manifest:
            path = Path(item["jsonl_path"])
            if item["parse_status"] == "success" and path.exists(): rows.extend(_read_jsonl(path))
        return rows

    def _map_acts(self, provisions: list[dict[str, Any]], profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not provisions: return []
        index = _BM25([f"{row['article']} {row['text']}" for row in provisions])
        by_document: dict[str, dict[str, Any]] = {}
        for profile in profiles:
            scores = index.scores(" ".join(profile["retrieval_query"]))
            top = sorted(range(len(scores)), key=scores.__getitem__, reverse=True)[:200]
            grouped: dict[str, list[tuple[float, str]]] = defaultdict(list)
            for pos in top:
                if scores[pos] > 0: grouped[provisions[pos]["document_id"]].append((scores[pos], provisions[pos]["provision_id"]))
            for doc_id, hits in sorted(grouped.items(), key=lambda pair: max(x[0] for x in pair[1]), reverse=True)[:30]:
                row = provisions[next(i for i, value in enumerate(provisions) if value["document_id"] == doc_id)]
                target = by_document.setdefault(doc_id, {"document_id":doc_id, "official_title":row["official_title"], "candidate_pillars":set(), "candidate_indicator_ids":set(), "indicator_scores":{}, "matched_provision_ids":{}, "mapping_stage":"document_candidate", "final_mapping":False})
                target["candidate_pillars"].add(profile["pillar_id"]); target["candidate_indicator_ids"].add(profile["indicator_id"]); target["indicator_scores"][profile["indicator_id"]] = max(x[0] for x in hits); target["matched_provision_ids"][profile["indicator_id"]] = [x[1] for x in hits]
        result=[]
        for row in by_document.values(): row["candidate_pillars"]=sorted(row["candidate_pillars"]); row["candidate_indicator_ids"]=sorted(row["candidate_indicator_ids"]); result.append(row)
        return sorted(result, key=lambda item:item["document_id"])

    def _select_sl(self, mappings: list[dict[str, Any]], catalogue: list[dict[str, Any]], profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        catalogue = [
            item for item in catalogue
            if item.get("instrument_type") == "subsidiary_legislation"
            and item.get("status") == "current"
        ]
        selected: dict[str, dict[str, Any]] = {}
        candidate_law_ids = {item["document_id"].removeprefix("sg-act-") for item in mappings}
        for act_id in candidate_law_ids:
            for sl_id in self._parent_sl_ids(act_id):
                record = next((x for x in catalogue if str(x["law_id"]).casefold() == sl_id), None)
                if record: self._add_sl(selected, record, act_id, "parent_act", None)
        index = _BM25([row["official_title"] for row in catalogue])
        for profile in profiles:
            scores=index.scores(" ".join(profile["retrieval_query"]))
            for pos in sorted(range(len(scores)), key=scores.__getitem__, reverse=True)[:20]:
                if scores[pos] > 0: self._add_sl(selected, catalogue[pos], None, f"title_bm25:{profile['indicator_id']}", profile)
        return sorted(selected.values(), key=lambda item:item["document_id"])

    def _parent_sl_ids(self, act_id: str) -> set[str]:
        url=f"https://sso.agc.gov.sg/act/{act_id}?ViewType=Sl"
        try:
            session=requests.Session(); session.headers.update(DIRECT_HTML_HEADERS)
            response=session.get(url, timeout=30, allow_redirects=True)
            if not response.ok: return set()
            return {urlsplit(urljoin(str(response.url), str(a["href"]))).path.rstrip("/").rsplit("/",1)[-1].casefold() for a in BeautifulSoup(response.text,"html.parser").find_all("a", href=True) if urlsplit(urljoin(str(response.url), str(a["href"]))).path.casefold().startswith("/sl/") and urlsplit(urljoin(str(response.url), str(a["href"]))).path.count("/")==2}
        except requests.RequestException: return set()

    def _add_sl(self, selected: dict[str, dict[str, Any]], record: dict[str, Any], parent: str | None, reason: str, profile: dict[str, Any] | None) -> None:
        doc_id=self._document_id(record,"subsidiary_legislation")
        item=selected.setdefault(doc_id,{"document_id":doc_id,"law_id":record["law_id"],"official_title":record["official_title"],"canonical_url":record["canonical_url"],"parent_act_ids":[],"selection_reasons":[],"candidate_pillars":[],"candidate_indicator_ids":[]})
        if parent and parent not in item["parent_act_ids"]: item["parent_act_ids"].append(parent)
        if reason not in item["selection_reasons"]: item["selection_reasons"].append(reason)
        if profile:
            if profile["pillar_id"] not in item["candidate_pillars"]: item["candidate_pillars"].append(profile["pillar_id"])
            if profile["indicator_id"] not in item["candidate_indicator_ids"]: item["candidate_indicator_ids"].append(profile["indicator_id"])
