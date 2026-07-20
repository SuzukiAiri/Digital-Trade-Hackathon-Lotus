"""Defensive parser for Singapore Statutes Online HTML."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urldefrag, urlsplit

from bs4 import BeautifulSoup, Tag

from rdtii_tool.document_models import LegalDocument, LegalSection


DATE_PATTERN = r"(?:\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{4}-\d{2}-\d{2})"
EDITORIAL_ANNOTATION_CLASSES = ("pREProvNo", "amendNote")


class SSOParser:
    """Parse SSO legislation into section-level structured documents."""

    def parse_file(
        self,
        path: str | Path,
        *,
        source_url: str,
        seed_law: str = "",
        source_type: str = "",
    ) -> LegalDocument:
        html_path = Path(path)
        html = html_path.read_text(encoding="utf-8")
        return self.parse(
            html,
            source_url=source_url,
            raw_html_path=str(html_path),
            seed_law=seed_law,
            source_type=source_type,
        )

    def parse(
        self,
        html: str,
        *,
        source_url: str,
        raw_html_path: str = "",
        seed_law: str = "",
        source_type: str = "",
    ) -> LegalDocument:
        soup = BeautifulSoup(html, "html.parser")
        law_name = self._extract_title(soup) or seed_law
        status_text = self._first_text(soup, (".status-value", "[data-status]"))
        current_version_date = self._extract_labeled_date(
            status_text,
            labels=("as at", "current version date"),
        )
        version_status = self._extract_version_status(status_text)
        parsed_source_type = source_type or self._infer_source_type(source_url)
        legal_rank = self._infer_legal_rank(parsed_source_type)

        sections = self._extract_sections(
            soup,
            source_url=source_url,
            parent_law_name=law_name,
            source_type=parsed_source_type,
        )

        return LegalDocument(
            economy="Singapore",
            law_name=law_name,
            law_number_or_ref=self._extract_law_reference(
                soup,
                source_url=source_url,
                law_name=law_name,
            ),
            source_url=source_url,
            source_name="Singapore Statutes Online",
            source_type=parsed_source_type,
            legal_rank=legal_rank,
            current_version_date=current_version_date,
            last_updated_date=self._extract_metadata_date(
                soup,
                selectors=(
                    "[data-last-updated]",
                    ".last-updated",
                    ".legis-last-updated",
                ),
                labels=("last updated",),
            ),
            effective_date=self._extract_metadata_date(
                soup,
                selectors=(
                    "[data-effective-date]",
                    ".effective-date",
                    ".commencement-date",
                ),
                labels=("effective date", "commencement date"),
            ),
            version_status=version_status,
            raw_html_path=raw_html_path,
            sections=sections,
        )

    def _extract_sections(
        self,
        soup: BeautifulSoup,
        *,
        source_url: str,
        parent_law_name: str,
        source_type: str,
    ) -> list[LegalSection]:
        sections: list[LegalSection] = []
        seen: set[str] = set()
        legal_root = soup.select_one("#legis") or soup.select_one(
            "[data-legislation-content], .legislation-content, "
            ".document-content, main"
        )
        if legal_root is None:
            return sections

        for container in legal_root.select("div.prov1"):
            header = container.select_one(".prov1Hdr")
            body = container.select_one(".prov1Txt")
            self._append_section(
                sections,
                seen,
                container=container,
                header=header,
                body=body,
                source_url=source_url,
                parent_law_name=parent_law_name,
                source_type=source_type,
            )

        generic_selectors = (
            "[data-legal-section]",
            "section.legislation-section",
            "article.legislation-section",
            ".legal-section",
        )
        for container in legal_root.select(",".join(generic_selectors)):
            header = container.select_one(
                "[data-section-heading], .section-heading, h1, h2, h3, h4"
            )
            body = container.select_one(
                "[data-section-text], .section-text, .provision-text"
            )
            self._append_section(
                sections,
                seen,
                container=container,
                header=header,
                body=body,
                source_url=source_url,
                parent_law_name=parent_law_name,
                source_type=source_type,
            )

        if not sections:
            content_root = legal_root
            if content_root:
                for container in content_root.select("section, article"):
                    header = container.select_one("h1, h2, h3, h4")
                    self._append_section(
                        sections,
                        seen,
                        container=container,
                        header=header,
                        body=None,
                        source_url=source_url,
                        parent_law_name=parent_law_name,
                        source_type=source_type,
                    )

        if not sections:
            content_root = legal_root
            if content_root:
                text, editorial_annotations = self._operative_text_and_editorial_annotations(content_root)
                if text:
                    sections.append(
                        LegalSection(
                            section_id="generated-section-001",
                            heading=parent_law_name,
                            text=text,
                            url=self._section_url(
                                source_url,
                                "generated-section-001",
                            ),
                            parent_law_name=parent_law_name,
                            source_url=source_url,
                            raw_context=str(content_root),
                            editorial_annotations=editorial_annotations,
                        )
                    )

        return sections

    def _append_section(
        self,
        sections: list[LegalSection],
        seen: set[str],
        *,
        container: Tag,
        header: Tag | None,
        body: Tag | None,
        source_url: str,
        parent_law_name: str,
        source_type: str,
    ) -> None:
        body_element = body or container
        text, editorial_annotations = self._operative_text_and_editorial_annotations(body_element)
        if not text:
            return

        heading = (
            self._normalize_whitespace(header.get_text(" ", strip=True))
            if header
            else ""
        )
        anchor = self._first_attribute(
            (header, container),
            ("id",),
        )
        section_id = self._first_attribute(
            (container, header),
            ("data-section-id", "data-section-number"),
        )
        if not section_id:
            section_id = self._section_id_from_anchor(anchor)
        if not section_id:
            section_id = self._section_id_from_text(body_element, heading)
        if not section_id:
            section_id = f"generated-section-{len(sections) + 1:03d}"

        if not heading:
            heading = f"Section {section_id}"
        heading = self._remove_duplicate_section_number(heading, section_id)
        part = self._structural_context(
            container,
            class_name="part",
            label_selectors=(".partNo", ".partHdr"),
        )
        division = self._structural_context(
            container,
            class_name="division",
            label_selectors=(".divtitle", ".divisionHdr", ".divisionNo"),
        )
        schedule = self._schedule_context(container)

        location_anchor = anchor or (
            section_id
            if section_id.startswith("generated-section-")
            else f"section-{section_id}"
        )
        identity = f"{section_id}\0{heading}\0{text}"
        if identity in seen:
            return
        seen.add(identity)

        sections.append(
            LegalSection(
                section_id=section_id,
                heading=heading,
                text=text,
                url=self._section_url(source_url, location_anchor),
                parent_law_name=parent_law_name,
                source_url=source_url,
                raw_context=str(container),
                part=part,
                division=division,
                schedule=schedule,
                provision_type=self._provision_type(
                    source_type=source_type,
                    schedule=schedule,
                ),
                provision_number=section_id,
                editorial_annotations=editorial_annotations,
            )
        )

    @classmethod
    def _operative_text_and_editorial_annotations(cls, element: Tag) -> tuple[str, list[dict[str, str]]]:
        clone_soup = BeautifulSoup(str(element), "html.parser")
        root = clone_soup.find()
        if root is None:
            return cls._normalize_whitespace(element.get_text(" ", strip=True)), []
        annotations: list[dict[str, str]] = []
        selector = ",".join(f".{class_name}" for class_name in EDITORIAL_ANNOTATION_CLASSES)
        for node in list(root.select(selector)):
            classes = [str(value) for value in node.get("class") or []]
            text = cls._normalize_whitespace(node.get_text(" ", strip=True))
            if text:
                annotations.append(
                    {
                        "class": " ".join(classes),
                        "text": text,
                    }
                )
            node.decompose()
        return cls._normalize_whitespace(root.get_text(" ", strip=True)), annotations

    @classmethod
    def _structural_context(
        cls,
        container: Tag,
        *,
        class_name: str,
        label_selectors: tuple[str, ...],
    ) -> str:
        ancestor = container.find_parent(
            lambda tag: isinstance(tag, Tag)
            and class_name in (tag.get("class") or [])
        )
        if ancestor is None:
            return ""
        labels = []
        for selector in label_selectors:
            element = ancestor.select_one(selector)
            if element:
                value = cls._normalize_whitespace(
                    element.get_text(" ", strip=True)
                )
                if value and value not in labels:
                    labels.append(value)
        return " - ".join(labels)

    @classmethod
    def _schedule_context(cls, container: Tag) -> str:
        ancestor = container.find_parent(
            lambda tag: isinstance(tag, Tag)
            and any(
                "sched" in value.casefold()
                for value in (tag.get("class") or [])
            )
        )
        if ancestor is None:
            return ""
        for selector in (
            ".scheduleNo",
            ".scheduleHdr",
            ".schedNo",
            ".schedHdr",
            ".scheduleTitle",
        ):
            element = ancestor.select_one(selector)
            if element:
                return cls._normalize_whitespace(
                    element.get_text(" ", strip=True)
                )
        identifier = str(ancestor.get("id", "")).strip()
        return identifier if identifier else "Schedule"

    @staticmethod
    def _provision_type(*, source_type: str, schedule: str) -> str:
        if schedule:
            return "schedule_item"
        if source_type == "subsidiary_legislation":
            return "regulation"
        return "section"

    @classmethod
    def _extract_title(cls, soup: BeautifulSoup) -> str:
        return cls._first_text(
            soup,
            (
                ".legis-title > span",
                ".legis-title > div",
                "[data-law-title]",
                "main h1",
                "h1",
            ),
        )

    @classmethod
    def _extract_law_reference(
        cls,
        soup: BeautifulSoup,
        *,
        source_url: str,
        law_name: str,
    ) -> str:
        explicit = cls._first_text(
            soup,
            (
                "[data-law-number]",
                ".legis-number",
                ".act-number",
                ".law-reference",
            ),
        )
        if explicit:
            return explicit

        path_reference = urlsplit(source_url).path.rstrip("/").split("/")[-1]
        sl_number_match = re.search(
            r"-S(\d+)-((?:19|20)\d{2})$",
            path_reference,
            flags=re.IGNORECASE,
        )
        if sl_number_match:
            return f"S {sl_number_match.group(1)}/{sl_number_match.group(2)}"

        text = cls._normalize_whitespace(soup.get_text(" ", strip=True))
        title_years = set(re.findall(r"\b(?:19|20)\d{2}\b", law_name))
        numbered_act_matches = re.findall(
            r"\b(?:Act|No\.)\s+\d+\s+of\s+((?:19|20)\d{2})\b",
            text,
            flags=re.IGNORECASE,
        )
        if title_years and numbered_act_matches:
            act_pattern = re.compile(
                r"\b(?:Act|No\.)\s+\d+\s+of\s+((?:19|20)\d{2})\b",
                flags=re.IGNORECASE,
            )
            for match in act_pattern.finditer(text):
                if match.group(1) in title_years:
                    return cls._normalize_whitespace(match.group(0))

        return path_reference

    @classmethod
    def _extract_metadata_date(
        cls,
        soup: BeautifulSoup,
        *,
        selectors: tuple[str, ...],
        labels: tuple[str, ...],
    ) -> str:
        for selector in selectors:
            element = soup.select_one(selector)
            if not element:
                continue
            value = (
                element.get("content")
                or element.get("data-value")
                or element.get_text(" ", strip=True)
            )
            date = cls._extract_labeled_date(str(value), labels=labels)
            if date:
                return date
            match = re.search(DATE_PATTERN, str(value), flags=re.IGNORECASE)
            if match:
                return cls._normalize_whitespace(match.group(0))
        return ""

    @classmethod
    def _extract_labeled_date(
        cls,
        value: str,
        *,
        labels: tuple[str, ...],
    ) -> str:
        normalized = cls._normalize_whitespace(value)
        for label in labels:
            pattern = rf"\b{re.escape(label)}\b\s*:?\s*({DATE_PATTERN})"
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
            if match:
                return cls._normalize_whitespace(match.group(1))
        return ""

    @classmethod
    def _extract_version_status(cls, status_text: str) -> str:
        normalized = cls._normalize_whitespace(status_text)
        if not normalized:
            return ""
        match = re.match(r"(.+?)\s+as at\s+" + DATE_PATTERN, normalized, re.IGNORECASE)
        if match:
            return cls._normalize_whitespace(match.group(1))
        return normalized

    @staticmethod
    def _infer_source_type(source_url: str) -> str:
        lowered = source_url.lower()
        if "/sl/" in lowered:
            return "subsidiary_legislation"
        if "/act/" in lowered:
            return "act"
        return ""

    @staticmethod
    def _infer_legal_rank(source_type: str) -> str:
        if source_type == "act":
            return "Act"
        if source_type == "subsidiary_legislation":
            return "Subsidiary Legislation"
        return ""

    @classmethod
    def _section_id_from_anchor(cls, anchor: str) -> str:
        if not anchor:
            return ""
        match = re.match(
            r"(?:pr|sec(?:tion)?[-_]?)?([0-9]+[A-Z]?)\-?$",
            anchor,
            flags=re.IGNORECASE,
        )
        return match.group(1) if match else ""

    @classmethod
    def _section_id_from_text(cls, body: Tag, heading: str) -> str:
        strong = body.select_one("strong, .section-number")
        candidates = [
            strong.get_text(" ", strip=True) if strong else "",
            heading,
            body.get_text(" ", strip=True)[:40],
        ]
        for value in candidates:
            match = re.match(r"\s*([0-9]+[A-Z]?)\s*[\.\-—:]?", value)
            if match:
                return match.group(1)
        return ""

    @classmethod
    def _remove_duplicate_section_number(cls, heading: str, section_id: str) -> str:
        pattern = rf"^\s*{re.escape(section_id)}\s*[\.\-—:]?\s+"
        cleaned = re.sub(pattern, "", heading, count=1, flags=re.IGNORECASE)
        return cls._normalize_whitespace(cleaned) or heading

    @staticmethod
    def _first_attribute(
        elements: tuple[Tag | None, ...],
        names: tuple[str, ...],
    ) -> str:
        for element in elements:
            if not element:
                continue
            for name in names:
                value = element.get(name)
                if value:
                    return str(value).strip()
        return ""

    @classmethod
    def _first_text(
        cls,
        soup: BeautifulSoup,
        selectors: tuple[str, ...],
    ) -> str:
        for selector in selectors:
            element = soup.select_one(selector)
            if element:
                text = cls._normalize_whitespace(element.get_text(" ", strip=True))
                if text:
                    return text
        return ""

    @staticmethod
    def _section_url(source_url: str, anchor: str) -> str:
        base_url, _ = urldefrag(source_url)
        return f"{base_url}#{anchor}" if anchor else base_url

    @staticmethod
    def _normalize_whitespace(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()
