"""Direct HTML acquisition for Singapore Statutes Online."""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from rdtii_tool.document_models import CandidateURL, DownloadResult
from rdtii_tool.downloaders.base import Downloader, sha256_bytes, utc_now


DIRECT_HTML_HEADERS = {
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


class SSOHTMLDownloader(Downloader):
    """Download and assemble an official SSO legal document."""

    method_name = "sso_direct_html"

    def __init__(
        self,
        *args,
        fragment_delay: float = 0.1,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.fragment_delay = max(0.0, fragment_delay)
        if hasattr(self.session, "headers"):
            self.session.headers.update(DIRECT_HTML_HEADERS)
            self.session.headers.setdefault(
                "X-Requested-With",
                "XMLHttpRequest",
            )

    def download(self, candidate: CandidateURL) -> DownloadResult:
        direct_url = self.canonical_url(candidate.url)
        cached_path = self.document_store.find_cached(candidate)
        if cached_path is not None and self.prefer_cache:
            cached_html = cached_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
            valid, signals, _ = self.validate_legal_html(
                cached_html,
                expected_title=candidate.title,
            )
            if valid and self._is_complete_document(cached_html):
                result = self._cached_result(candidate, cached_path)
                result.source_url = direct_url
                result.original_url = candidate.url
                result.direct_html_url = direct_url
                result.content_validation_passed = True
                result.content_validation_signals = signals
                return result

        try:
            response = self.session.get(
                direct_url,
                timeout=self.timeout,
                allow_redirects=True,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            content = self._response_content(response)
            content_type = self._content_type(response) or "text/html"

            if status_code in {403, 429, 467}:
                error_type = {
                    403: "forbidden_403",
                    429: "rate_limited_429",
                    467: "access_restricted_467",
                }[status_code]
                return self._result(
                    candidate,
                    direct_url=direct_url,
                    status="blocked",
                    http_status=status_code,
                    error_type=error_type,
                    error_message=(
                        f"Direct lower-case SSO HTML request returned "
                        f"HTTP {status_code}"
                    ),
                )
            if status_code < 200 or status_code >= 300:
                return self._result(
                    candidate,
                    direct_url=direct_url,
                    status="failed",
                    http_status=status_code or None,
                    error_type="network_error",
                    error_message=(
                        f"Direct lower-case SSO HTML request returned "
                        f"HTTP {status_code}"
                    ),
                )
            if "html" not in content_type.casefold():
                return self._result(
                    candidate,
                    direct_url=direct_url,
                    status="failed",
                    http_status=status_code,
                    error_type="unexpected_content_type",
                    error_message=f"Expected HTML but received {content_type}",
                )

            html = self._decode_html(response, content)
            html, fragment_signals = self._hydrate_lazy_fragments(
                html,
                page_url=str(getattr(response, "url", "") or direct_url),
            )
            valid, signals, reason = self.validate_legal_html(
                html,
                expected_title=candidate.title,
            )
            signals.extend(fragment_signals)
            if not valid:
                return self._result(
                    candidate,
                    direct_url=direct_url,
                    status="failed",
                    http_status=status_code,
                    error_type="content_validation_failed",
                    error_message=reason,
                    validation_signals=signals,
                )

            encoded = html.encode("utf-8")
            saved_path = self.document_store.save_html(candidate, encoded)
            return DownloadResult(
                candidate_url=candidate.url,
                normalized_url=candidate.normalized_url,
                source_name=candidate.source_name,
                source_url=direct_url,
                method=self.method_name,
                file_type="html",
                status="success",
                http_status=status_code,
                saved_path=str(saved_path),
                content_type="text/html",
                sha256=sha256_bytes(encoded),
                file_size=len(encoded),
                timestamp=utc_now(),
                original_url=candidate.url,
                direct_html_url=direct_url,
                content_validation_passed=True,
                content_validation_signals=signals,
            )
        except _SSOFragmentError as exc:
            return self._result(
                candidate,
                direct_url=direct_url,
                status=(
                    "blocked"
                    if exc.status_code in {403, 429, 467}
                    else "failed"
                ),
                http_status=exc.status_code,
                error_type=exc.error_type,
                error_message=str(exc),
            )
        except Exception as exc:
            error_type, message = self._exception_error(exc)
            return self._result(
                candidate,
                direct_url=direct_url,
                status="failed",
                error_type=error_type,
                error_message=message,
            )

    def _hydrate_lazy_fragments(
        self,
        html: str,
        *,
        page_url: str,
    ) -> tuple[str, list[str]]:
        soup = BeautifulSoup(html, "html.parser")
        placeholders = list(
            soup.select("#legis div.dms[data-field='seriesId']")
        )
        if not placeholders:
            return html, ["complete_document_html"]

        global_data = self._legislation_globals(soup)
        lazy_filter = global_data.get("lazyLoadFilter")
        lazy_url = str(global_data.get("lazyLoadContentUrl", "")).strip()
        fragments = global_data.get("fragments")
        has_direct_fragment_urls = all(
            any(
                str(item.get(attribute, "")).strip()
                and not str(item.get(attribute, "")).strip().casefold().startswith(("javascript:", "#"))
                for attribute in ("data-url", "data-href", "href")
            )
            for item in placeholders
        )
        if not has_direct_fragment_urls and (
            not isinstance(lazy_filter, dict)
            or not lazy_url
            or not isinstance(fragments, dict)
        ):
            raise _SSOFragmentError(
                "SSO page contains lazy fragments but no usable metadata",
                error_type="fragment_metadata_missing",
            )

        endpoint = urljoin(page_url, lazy_url)
        loaded = 0
        empty = 0
        for placeholder in placeholders:
            series_id = str(placeholder.get("data-term", "")).strip()
            fragment_url = next(
                (
                    str(placeholder.get(attribute, "")).strip()
                    for attribute in ("data-url", "data-href", "href")
                    if str(placeholder.get(attribute, "")).strip()
                    and not str(placeholder.get(attribute, "")).strip().casefold().startswith(("javascript:", "#"))
                ),
                "",
            )
            fragment_info = fragments.get(series_id) if isinstance(fragments, dict) else None
            if not series_id or (not fragment_url and not isinstance(fragment_info, dict)):
                raise _SSOFragmentError(
                    f"Missing SSO fragment mapping for series {series_id!r}",
                    error_type="fragment_metadata_missing",
                )

            params = None
            request_url = urljoin(page_url, fragment_url) if fragment_url else endpoint
            if not fragment_url:
                params = {
                    key: value
                    for key, value in lazy_filter.items()
                    if value is not None
                }
                params["SeriesId"] = series_id
                params["FragSysId"] = fragment_info.get("Item1", "")
                params["_"] = fragment_info.get("Item2", "")
            response = self.session.get(
                request_url,
                params=params,
                timeout=self.timeout,
                allow_redirects=True,
            )
            fragment_status = int(
                getattr(response, "status_code", 0) or 0
            )
            if fragment_status < 200 or fragment_status >= 300:
                if fragment_status == 404:
                    placeholder.decompose()
                    empty += 1
                    continue
                error_type = {
                    403: "forbidden_403",
                    429: "rate_limited_429",
                    467: "access_restricted_467",
                }.get(fragment_status, "fragment_download_failed")
                raise _SSOFragmentError(
                    (
                        "Official SSO lazy fragment request returned "
                        f"HTTP {fragment_status}"
                    ),
                    status_code=fragment_status,
                    error_type=error_type,
                )

            fragment_html = self._decode_html(
                response,
                self._response_content(response),
            )
            fragment_soup = BeautifulSoup(fragment_html, "html.parser")
            fragment_nodes = list(fragment_soup.contents)
            if not fragment_nodes:
                empty += 1
            else:
                for node in fragment_nodes:
                    placeholder.insert_before(node.extract())
            placeholder.decompose()
            loaded += 1
            if self.fragment_delay:
                time.sleep(self.fragment_delay)

        signals = [
            "complete_document_html",
            f"official_lazy_fragments:{loaded}",
        ]
        if empty:
            signals.append(f"empty_lazy_fragments:{empty}")
        return str(soup), signals

    def _result(
        self,
        candidate: CandidateURL,
        *,
        direct_url: str,
        status: str,
        http_status: int | None = None,
        error_type: str = "",
        error_message: str = "",
        validation_signals: list[str] | None = None,
    ) -> DownloadResult:
        return DownloadResult(
            candidate_url=candidate.url,
            normalized_url=candidate.normalized_url,
            source_name=candidate.source_name,
            source_url=direct_url,
            method=self.method_name,
            status=status,
            http_status=http_status,
            error_type=error_type,
            error_message=error_message,
            timestamp=utc_now(),
            original_url=candidate.url,
            direct_html_url=direct_url,
            content_validation_passed=False,
            content_validation_signals=validation_signals or [],
        )

    @staticmethod
    def canonical_url(url: str) -> str:
        parsed = urlsplit(url)
        return urlunsplit(
            (
                "https",
                "sso.agc.gov.sg",
                (parsed.path or "/").casefold(),
                "",
                "",
            )
        )

    @staticmethod
    def _decode_html(response: object, content: bytes) -> str:
        encoding = str(
            getattr(response, "encoding", "")
            or getattr(response, "apparent_encoding", "")
            or "utf-8"
        )
        return content.decode(encoding, errors="replace")

    @staticmethod
    def _legislation_globals(soup: BeautifulSoup) -> dict:
        for element in soup.select(".global-vars[data-json]"):
            try:
                payload = json.loads(str(element.get("data-json", "")))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and "lazyLoadContentUrl" in payload:
                return payload
        return {}

    @staticmethod
    def _is_complete_document(html: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        return not bool(
            soup.select_one("#legis div.dms[data-field='seriesId']")
        )

    @staticmethod
    def validate_legal_html(
        html: str,
        *,
        expected_title: str,
    ) -> tuple[bool, list[str], str]:
        if not html.strip():
            return False, [], "Direct response contained an empty HTML page"

        soup = BeautifulSoup(html, "html.parser")
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
        lowered = text.casefold()
        blocked_markers = (
            "access denied",
            "request blocked",
            "the request could not be satisfied",
            "cloudfront",
            "captcha",
            "verify you are human",
            "http error 467",
        )
        if any(marker in lowered for marker in blocked_markers):
            return False, [], "Direct response contained an access restriction page"

        signals: list[str] = []
        title_words = [
            word.casefold()
            for word in re.findall(r"[A-Za-z0-9]+", expected_title)
            if len(word) >= 3
        ]
        if title_words and all(word in lowered for word in title_words):
            signals.append("expected_title")
        if re.search(r"\bstatus\s*:", text, re.IGNORECASE):
            signals.append("status")
        if "current version" in lowered:
            signals.append("current_version")
        if "table of contents" in lowered:
            signals.append("table_of_contents")
        if soup.select_one("#legis, #legisContent, .legis-content"):
            signals.append("sso_legislation_root")
        if (
            "singapore statutes online" in lowered
            or "sso.agc.gov.sg" in html.casefold()
        ):
            signals.append("official_sso_page")
        if soup.select_one(
            "div.prov1, .prov1Txt, [data-legal-section], "
            ".legislation-section, [data-legislation-content]"
        ):
            signals.append("sso_section_structure")
        if len(text) >= 1000 and re.search(
            r"\b\d+[A-Za-z]?\.\s+[A-Z]",
            text,
        ):
            signals.append("numbered_provisions")

        title_ok = "expected_title" in signals
        structure_ok = any(
            signal in signals
            for signal in (
                "sso_section_structure",
                "numbered_provisions",
            )
        )
        metadata_ok = any(
            signal in signals
            for signal in (
                "status",
                "current_version",
                "table_of_contents",
            )
        )
        sso_short_document_ok = (
            "sso_legislation_root" in signals
            and "official_sso_page" in signals
            and metadata_ok
            and len(text) >= 250
        )
        valid = title_ok and (structure_ok or sso_short_document_ok) and metadata_ok
        if valid:
            return True, signals, ""
        return (
            False,
            signals,
            "HTML did not contain enough SSO legal-document signals",
        )


class _SSOFragmentError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str = "fragment_download_failed",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
