"""Discover current Singapore legislation from the public SSO Browse catalogue."""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

InstrumentType = Literal["act", "subsidiary_legislation"]
_WHITESPACE = re.compile(r"\s+")
_DIRECT_HTML_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Referer": "https://sso.agc.gov.sg/",
}


class _LinkParser(HTMLParser):
    """Small, dependency-free extractor for public catalogue links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "a":
            attributes = dict(attrs)
            self._href = attributes.get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._text)))
            self._href = None
            self._text = []


class CatalogueStructureError(RuntimeError):
    """Raised when a public SSO catalogue entry cannot be interpreted."""


@dataclass(slots=True)
class _FetchedPage:
    requested_url: str
    final_url: str
    status_code: int | None
    content_type: str
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class CatalogueRecord:
    economy: str
    source_portal: str
    source_id: str
    document_id: str
    law_id: str
    official_title: str
    instrument_type: InstrumentType
    status: str
    language: str
    canonical_url: str
    catalogue_url: str

    def to_json_dict(self) -> dict[str, str]:
        return asdict(self)


class SSOCatalogueAdapter:
    """Traverse SSO's public current-legislation Browse listings.

    This deliberately follows only Browse links belonging to the selected
    current catalogue.  It is not a general website crawler and never derives
    legislation URLs from guessed identifiers.
    """

    BASE_URL = "https://sso.agc.gov.sg"
    ENTRY_URLS = {
        "act": f"{BASE_URL}/Browse/Act/Current/All?PageSize=500&SortBy=Title&SortOrder=ASC",
        "subsidiary_legislation": f"{BASE_URL}/Browse/SL/Current/All?PageSize=500&SortBy=Title&SortOrder=ASC",
    }
    _PATH_PREFIXES = {"act": "/act/", "subsidiary_legislation": "/sl/"}

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        retries: int = 2,
        debug_dir: str | Path | None = None,
        max_pages: int = 200,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = max(0, retries)
        self.debug_dir = Path(debug_dir) if debug_dir is not None else None
        self.max_pages = max(1, max_pages)
        self.pages_visited: list[str] = []
        self.failed_catalogue_pages: list[str] = []
        self.duplicate_records_removed = 0
        self.session.headers.update(_DIRECT_HTML_HEADERS)
        self.session.headers.setdefault("X-Requested-With", "XMLHttpRequest")

    def discover_current_acts(self) -> list[CatalogueRecord]:
        return self._discover("act")

    def discover_current_subsidiary_legislation(self) -> list[CatalogueRecord]:
        return self._discover("subsidiary_legislation")

    def discover_all_current_legislation(self) -> list[CatalogueRecord]:
        acts = self.discover_current_acts()
        subsidiary = self.discover_current_subsidiary_legislation()
        return self._deduplicate_and_sort([*acts, *subsidiary])

    def _discover(self, instrument_type: InstrumentType) -> list[CatalogueRecord]:
        pending = deque([self.ENTRY_URLS[instrument_type]])
        seen_pages: set[str] = set()
        records: list[CatalogueRecord] = []
        last_entry_response: _FetchedPage | None = None

        while pending and len(seen_pages) < self.max_pages:
            page_url = pending.popleft()
            page_key = self._catalogue_page_key(page_url)
            if page_key in seen_pages:
                continue
            seen_pages.add(page_key)

            response = self._fetch(page_url)
            if response is None or not (200 <= (response.status_code or 0) < 300):
                self.failed_catalogue_pages.append(page_url)
                continue
            if page_url == self.ENTRY_URLS[instrument_type]:
                last_entry_response = response
            self.pages_visited.append(page_url)
            parser = _LinkParser()
            parser.feed(response.text)
            page_records = self._extract_records(parser.links, page_url, instrument_type)
            child_pages = self._catalogue_links(parser.links, page_url, instrument_type)
            if self._is_access_denied(response.text):
                self.failed_catalogue_pages.append(page_url)
                continue
            if not page_records and page_url == self.ENTRY_URLS[instrument_type]:
                self._save_debug_response(instrument_type, response)
                raise CatalogueStructureError(self._diagnostic(response))
            before = len({record.document_id for record in records})
            records.extend(page_records)
            after = len({record.document_id for record in records})
            if page_url != self.ENTRY_URLS[instrument_type] and after == before:
                break
            for child_page in child_pages:
                if self._catalogue_page_key(child_page) not in seen_pages:
                    pending.append(child_page)

        if not records:
            if last_entry_response is not None:
                self._save_debug_response(instrument_type, last_entry_response)
            raise CatalogueStructureError(
                f"SSO {instrument_type} current catalogue produced no legislation records"
            )
        return self._deduplicate_and_sort(records)

    def _fetch(self, url: str) -> _FetchedPage | None:
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            except requests.RequestException:
                if attempt < self.retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return None
            status = int(response.status_code)
            if status == 404:
                return None
            if 200 <= status < 300:
                return _FetchedPage(
                    requested_url=url,
                    final_url=str(response.url),
                    status_code=status,
                    content_type=str(response.headers.get("Content-Type", "")),
                    content=bytes(response.content),
                )
            if status in {408, 425, 429} or status >= 500:
                if attempt < self.retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
            return None
        return None

    def _save_debug_response(
        self, instrument_type: InstrumentType, response: _FetchedPage
    ) -> None:
        if self.debug_dir is None:
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        filename = (
            "sso_current_acts_response.html"
            if instrument_type == "act"
            else "sso_current_sl_response.html"
        )
        (self.debug_dir / filename).write_bytes(response.content)

    @staticmethod
    def _is_access_denied(html: str) -> bool:
        text = html.casefold()
        return "access denied" in text or "request blocked" in text

    @staticmethod
    def _diagnostic(response: _FetchedPage) -> str:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", response.text, re.I | re.S)
        title = _WHITESPACE.sub(" ", title_match.group(1)).strip() if title_match else ""
        preview = _WHITESPACE.sub(" ", re.sub(r"<[^>]+>", " ", response.text)).strip()[:300]
        kind = "access denied" if SSOCatalogueAdapter._is_access_denied(response.text) else "normal/changed-or-empty page"
        return (
            "SSO catalogue did not contain usable legislation links "
            f"({kind}); requested_url={response.requested_url}; "
            f"final_url={response.final_url}; status={response.status_code}; "
            f"content_type={response.content_type}; bytes={len(response.content)}; "
            f"title={title!r}; text_preview={preview!r}"
        )

    def _extract_records(
        self, links: list[tuple[str, str]], catalogue_url: str, instrument_type: InstrumentType
    ) -> list[CatalogueRecord]:
        prefix = self._PATH_PREFIXES[instrument_type]
        records = []
        for href, anchor_text in links:
            absolute = urljoin(catalogue_url, href)
            parsed = urlsplit(absolute)
            if (parsed.hostname or "").casefold() != "sso.agc.gov.sg":
                continue
            path = parsed.path.casefold().rstrip("/")
            if not path.startswith(prefix) or path.count("/") != 2:
                continue
            law_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            title = _WHITESPACE.sub(" ", anchor_text).strip()
            title = re.sub(r"\s+(?:actions|download pdf)\s*$", "", title, flags=re.I)
            if not law_id or not title or self._is_excluded(title, path):
                continue
            canonical_url = self._canonical_legal_url(absolute)
            records.append(
                CatalogueRecord(
                    economy="Singapore",
                    source_portal="Singapore Statutes Online",
                    source_id="sg_sso",
                    document_id=(
                        f"sg_sso:{instrument_type}:{law_id.casefold()}"
                    ),
                    law_id=law_id,
                    official_title=title,
                    instrument_type=instrument_type,
                    status="current",
                    language="en",
                    canonical_url=canonical_url,
                    catalogue_url=self._catalogue_page_key(catalogue_url),
                )
            )
        return records

    @staticmethod
    def _is_excluded(title: str, path: str) -> bool:
        text = f"{title} {path}".casefold()
        return any(term in text for term in ("repealed", "revoked", "historical", "/bill/"))

    def _catalogue_links(
        self, links: list[tuple[str, str]], base_url: str, instrument_type: InstrumentType
    ) -> list[str]:
        prefix = (
            f"/browse/{'act' if instrument_type == 'act' else 'sl'}/current/all"
        )
        catalogue_links = []
        for href, _ in links:
            absolute = urljoin(base_url, href)
            parsed = urlsplit(absolute)
            if (parsed.hostname or "").casefold() != "sso.agc.gov.sg":
                continue
            path = parsed.path.casefold().rstrip("/")
            if not path.startswith(prefix):
                continue
            suffix = path[len(prefix):].strip("/")
            has_page_query = any(
                key.casefold() in {"page", "pagenumber"}
                for key in re.findall(r"(?:^|&)([^=&]+)=", parsed.query)
            )
            if suffix and not suffix.isdigit():
                continue
            if not suffix and not has_page_query:
                continue
            catalogue_links.append(self._catalogue_page_key(absolute))
        return sorted(set(catalogue_links))

    @classmethod
    def _catalogue_page_key(cls, url: str) -> str:
        parsed = urlsplit(url)
        return urlunsplit(("https", "sso.agc.gov.sg", parsed.path, parsed.query, ""))

    @staticmethod
    def _has_browse_marker(html: str) -> bool:
        return "browse" in html.casefold()

    @staticmethod
    def _canonical_legal_url(url: str) -> str:
        """Use the established SSO direct-HTML lower-case canonical form."""
        parsed = urlsplit(url)
        return urlunsplit(
            ("https", "sso.agc.gov.sg", (parsed.path or "/").casefold(), "", "")
        )

    def _deduplicate_and_sort(
        self, records: list[CatalogueRecord]
    ) -> list[CatalogueRecord]:
        unique: dict[tuple[str, str], CatalogueRecord] = {}
        for record in records:
            key = (record.instrument_type, record.law_id.casefold())
            if key in unique:
                self.duplicate_records_removed += 1
                continue
            unique[key] = record
        return sorted(
            unique.values(),
            key=lambda item: (item.instrument_type, item.official_title.casefold(), item.law_id),
        )
