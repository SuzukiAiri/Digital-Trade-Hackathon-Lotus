"""Shared helpers for Malaysia Zone 1 official-source acquisition."""

from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from pypdf import PdfReader


LOM_BASE = "https://lom.agc.gov.my/"
PDP_BASE = "https://www.pdp.gov.my/ppdpv1/en/akta709/"
USER_AGENT = "RDTII-Malaysia-Zone1/0.1 (official-document downloader; research use)"


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def safe_token(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-").casefold()
    return text or "document"


def short_hash(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:length]


def extract_href_links(html: Any, *, base_url: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    soup = BeautifulSoup(str(html or ""), "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        links.append((urljoin(base_url, href), anchor.get_text(" ", strip=True)))
    return links


def extract_pdf_links_from_record(record: dict[str, Any], *, base_url: str) -> list[tuple[str, str, str]]:
    """Return (url, text, source_field) tuples for official PDF links in a LOM/PDP record."""

    links: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for key, value in record.items():
        if value is None:
            continue
        if isinstance(value, str) and ("href" in value.casefold() or ".pdf" in value.casefold()):
            for url, label in extract_href_links(value, base_url=base_url):
                if ".pdf" not in url.casefold():
                    continue
                if url not in seen:
                    seen.add(url)
                    links.append((url, label, key))
        elif isinstance(value, str) and value.strip().casefold().endswith(".pdf"):
            url = urljoin(base_url, value.strip())
            if url not in seen:
                seen.add(url)
                links.append((url, "", key))
    return links


def datatables_payload(*, start: int, length: int, columns: int = 10) -> dict[str, str]:
    payload: dict[str, str] = {
        "draw": "1",
        "start": str(start),
        "length": str(length),
        "search[value]": "",
        "search[regex]": "false",
        "searchValue": "",
        "searchColumns[]": "",
        "order[0][column]": "0",
        "order[0][dir]": "desc",
    }
    for index in range(columns):
        payload[f"columns[{index}][data]"] = str(index)
        payload[f"columns[{index}][name]"] = ""
        payload[f"columns[{index}][searchable]"] = "true"
        payload[f"columns[{index}][orderable]"] = "true"
        payload[f"columns[{index}][search][value]"] = ""
        payload[f"columns[{index}][search][regex]"] = "false"
    return payload


def language_from_link(url: str, source_field: str = "", fallback: str = "") -> str:
    folded = f"{url} {source_field} {fallback}".casefold()
    if "_bm" in folded or "pdf-ms" in folded or "malay" in folded or "bahasa" in folded:
        return "ms"
    if "_bi" in folded or "pdf-en" in folded or "english" in folded:
        return "en"
    if fallback.casefold() in {"bm", "ms"}:
        return "ms"
    if fallback.casefold() in {"bi", "en"}:
        return "en"
    return "bilingual"


def pdf_text(content: bytes) -> tuple[str, bool]:
    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception:
        return "", False
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(page.strip() for page in pages if page.strip())).strip()
    return text, not bool(text)


def html_text(content: bytes) -> str:
    soup = BeautifulSoup(content.decode("utf-8", errors="replace"), "html.parser")
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True)).strip()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

