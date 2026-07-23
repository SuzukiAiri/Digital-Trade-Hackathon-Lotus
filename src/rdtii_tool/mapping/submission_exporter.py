"""Unified formal submission exporter for RDTII P6/P7 outputs."""

from __future__ import annotations

import csv
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import escape
from pathlib import Path

from .discovery_tags import load_legal_inventory_registry, validate_discovery_tag
from .economy_profiles import economy_profile
from .indicator_registry import indicator_sort_key
from .models import AtomicEvidenceRecord
from .submission_rationale import render_submission_rationale, validate_submission_rationale


SUBMISSION_COLUMNS = [
    "Economy",
    "Law Name",
    "Law Number / Ref",
    "Last Amended",
    "Indicator ID",
    "Article / Section",
    "Discovery Tag",
    "Location Reference",
    "Verbatim Snippet",
    "Mapping Rationale",
    "Source URL",
    "Confidence",
    "Notes",
]

_P6_P7_INDICATORS = {
    "P6-I1",
    "P6-I2",
    "P6-I3",
    "P6-I4",
    "P6-I5",
    "P7-I1",
    "P7-I2",
    "P7-I3",
    "P7-I4",
    "P7-I5",
}
_TREATY_DOCUMENT_IDS = {"cptpp", "rcep"}


@dataclass
class _DocumentMeta:
    document_id: str
    title: str
    official_number: str
    last_amended: str
    source_url: str
    collection: str
    provisions_path: str


@dataclass
class _ProvisionMeta:
    provision_id: str
    provision_path: str
    text: str
    article: str
    provision_number: str
    section: str
    provision_label: str
    anchor_url: str
    hierarchy: list[str]


class _SourceIndex:
    def __init__(self, project_root: Path, economy_root: Path) -> None:
        self.project_root = project_root
        self.economy_root = economy_root
        self._document_meta: dict[str, _DocumentMeta] = {}
        self._provision_cache: dict[str, dict[str, _ProvisionMeta]] = {}
        self._build_document_index()

    def document_meta(self, document_id: str) -> _DocumentMeta | None:
        return self._document_meta.get(document_id)

    def provision_meta(self, document_id: str, provision_id: str) -> _ProvisionMeta | None:
        if not document_id or not provision_id:
            return None
        if document_id not in self._provision_cache:
            self._provision_cache[document_id] = self._load_provisions(document_id)
        table = self._provision_cache[document_id]
        if provision_id in table:
            return table[provision_id]
        return table.get(_strip_trailing_subparts(provision_id))

    def _build_document_index(self) -> None:
        docs_path = self.economy_root / "zone1_documents.jsonl"
        if docs_path.exists():
            for line in docs_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                document_id = str(row.get("document_id") or "").strip()
                if not document_id:
                    continue
                self._document_meta[document_id] = _DocumentMeta(
                    document_id=document_id,
                    title=str(row.get("title") or ""),
                    official_number=_structured_official_number(row),
                    last_amended=_structured_last_amended(row),
                    source_url=str(row.get("canonical_url") or row.get("source_url") or ""),
                    collection=str(row.get("collection") or ""),
                    provisions_path="",
                )
        manifest = self.economy_root / "zone1_provisions_manifest.jsonl"
        if not manifest.exists():
            return
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            document_id = str(row.get("document_id") or "").strip()
            if not document_id:
                continue
            meta = self._document_meta.get(document_id)
            if meta is None:
                meta = _DocumentMeta(
                    document_id=document_id,
                    title=str(row.get("title") or ""),
                    official_number=_structured_official_number(row),
                    last_amended=_structured_last_amended(row),
                    source_url=str(row.get("canonical_url") or row.get("source_url") or ""),
                    collection=str(row.get("collection") or ""),
                    provisions_path="",
                )
                self._document_meta[document_id] = meta
            if not meta.provisions_path:
                meta.provisions_path = str(row.get("provisions_path") or "")

    def _load_provisions(self, document_id: str) -> dict[str, _ProvisionMeta]:
        meta = self._document_meta.get(document_id)
        if meta is None or not meta.provisions_path:
            return {}
        provision_path = Path(meta.provisions_path)
        if not provision_path.is_absolute():
            provision_path = self.project_root / provision_path
        if not provision_path.exists():
            return {}
        table: dict[str, _ProvisionMeta] = {}
        for line in provision_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            provision_id = str(row.get("provision_id") or "").strip()
            if not provision_id:
                continue
            table[provision_id] = _ProvisionMeta(
                provision_id=provision_id,
                provision_path=str(row.get("provision_path") or ""),
                text=str(row.get("text") or ""),
                article=str(row.get("article") or ""),
                provision_number=str(row.get("provision_number") or ""),
                section=str(row.get("section") or ""),
                provision_label=str(row.get("provision_label") or ""),
                anchor_url=str(row.get("anchor_url") or row.get("anchor") or ""),
                hierarchy=list(row.get("hierarchy") or []) if isinstance(row.get("hierarchy"), list) else [],
            )
        return table


def export_submission(
    economy_slug: str,
    output_dir: Path,
    records: list[AtomicEvidenceRecord],
    *,
    output_prefix: str | None = None,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    project_root = Path(__file__).resolve().parents[3]
    registry = load_legal_inventory_registry(project_root)
    registry.write_audit(output_dir / "discovery_tag_baseline_audit.json")
    economy_root = project_root / "outputs" / "corpus" / economy_slug
    source_index = _SourceIndex(project_root, economy_root)
    rows, excluded_treaty_rows = build_submission_records(records, source_index)
    rows.sort(
        key=lambda row: (
            indicator_sort_key(row.get("Indicator ID", "")),
            row.get("Law Name", ""),
            row.get("Article / Section", ""),
        )
    )

    prefix = output_prefix or f"{economy_slug}_p6_p7"
    csv_path = output_dir / f"{prefix}.csv"
    json_path = output_dir / f"{prefix}.json"
    xlsx_path = output_dir / f"{prefix}.xlsx"
    _write_csv(csv_path, rows)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_xlsx(
        xlsx_path,
        rows=rows,
        methodology_rows=_methodology_rows(
            excluded_treaty_rows=excluded_treaty_rows,
        ),
    )
    _assert_submission_outputs_match(csv_path, json_path, xlsx_path)

    return {
        "csv": str(csv_path),
        "json": str(json_path),
        "xlsx": str(xlsx_path),
        "rows": len(rows),
        "known_rows": sum(1 for row in rows if row.get("Discovery Tag") == "KNOWN"),
        "new_rows": sum(1 for row in rows if row.get("Discovery Tag") == "NEW"),
        "excluded_treaty_rows": excluded_treaty_rows,
        "registry_sources": [str(registry.source_path)],
        "registry_version": registry.registry_version,
        "registry_file_hash": registry.file_hash,
    }


def build_submission_records(records: list[AtomicEvidenceRecord], source_index: _SourceIndex) -> tuple[list[dict[str, str]], int]:
    rows: list[dict[str, str]] = []
    excluded_treaty_rows = 0
    for record in records:
        if record.decision != "accepted" or record.citation_status != "verified":
            continue
        if _exclude_from_output_data(record):
            excluded_treaty_rows += 1
            continue
        rows.append(_submission_row(record, source_index))
    return rows, excluded_treaty_rows


def _exclude_from_output_data(record: AtomicEvidenceRecord) -> bool:
    indicator = str(record.indicator_id or "").strip().upper()
    document_id = str(record.document_id or "").strip().casefold()
    law_name = str(record.law_name or "").strip().casefold()
    if indicator == "P6-I5":
        return True
    return document_id in _TREATY_DOCUMENT_IDS or law_name in _TREATY_DOCUMENT_IDS


def _submission_row(
    record: AtomicEvidenceRecord,
    source_index: _SourceIndex,
) -> dict[str, str]:
    doc_meta = source_index.document_meta(record.document_id)
    law_name = _normalize_space(record.law_name or (doc_meta.title if doc_meta else ""))
    law_name = _normalized_law_name(record.economy, law_name, record.law_number_ref)
    article_label = _normalize_space(record.article)
    location_reference = _normalize_space(record.location_reference)
    official_number = _normalize_space(record.law_number_ref or (doc_meta.official_number if doc_meta else ""))
    last_amended = _normalize_space(record.last_amended or (doc_meta.last_amended if doc_meta else ""))
    source_url = _normalize_space(record.source_url or (doc_meta.source_url if doc_meta else ""))
    notes = _normalize_space(record.notes)
    focal_quote = _normalize_space(record.focal_quote)
    rationale = render_submission_rationale(
        indicator_id=str(record.indicator_id or ""),
        law_name=law_name,
        article_section=article_label,
        verbatim_snippet=focal_quote,
        attributes=record.validated_attributes,
        notes=notes,
        existing_rationale=record.mapping_rationale,
    )
    return {
        "Economy": economy_profile(record.economy).name,
        "Law Name": law_name,
        "Law Number / Ref": official_number,
        "Last Amended": last_amended,
        "Indicator ID": str(record.indicator_id or ""),
        "Article / Section": article_label,
        "Discovery Tag": validate_discovery_tag(record.discovery_tag),
        "Location Reference": location_reference,
        "Verbatim Snippet": focal_quote,
        "Mapping Rationale": rationale,
        "Source URL": source_url,
        "Confidence": _submission_confidence(record),
        "Notes": notes,
    }


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUBMISSION_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _write_xlsx(path: Path, *, rows: list[dict[str, str]], methodology_rows: list[dict[str, str]]) -> None:
    sheets = [
        ("Output Data", SUBMISSION_COLUMNS, rows),
        ("Methodology", ["Topic", "Detail"], methodology_rows),
    ]
    _write_workbook(path, sheets)


def _write_workbook(path: Path, sheets: list[tuple[str, list[str], list[dict[str, str]]]]) -> None:
    shared: list[str] = []
    shared_index: dict[str, int] = {}

    def sst(value: object) -> int:
        text = str(value or "")
        if text not in shared_index:
            shared_index[text] = len(shared)
            shared.append(text)
        return shared_index[text]

    sheet_xml: dict[str, str] = {}
    sheet_rels: dict[str, str] = {}
    for sheet_idx, (name, columns, rows) in enumerate(sheets, start=1):
        lines = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
        ]
        hyperlinks: list[tuple[str, str]] = []
        hyperlink_rels: list[str] = []
        lines.append('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>')
        if name == "Output Data":
            widths = [14, 34, 18, 14, 12, 18, 12, 42, 72, 56, 42, 12, 40]
            lines.append("<cols>" + "".join(f'<col min="{idx}" max="{idx}" width="{width}" customWidth="1"/>' for idx, width in enumerate(widths, start=1)) + "</cols>")
        else:
            lines.append('<cols><col min="1" max="1" width="24" customWidth="1"/><col min="2" max="2" width="112" customWidth="1"/></cols>')
        lines.append("<sheetData>")
        grid = [columns] + [[row.get(column, "") for column in columns] for row in rows]
        for row_idx, row in enumerate(grid, start=1):
            row_height = "24" if row_idx == 1 else ("40" if name == "Output Data" else "32")
            lines.append(f'<row r="{row_idx}" ht="{row_height}" customHeight="1">')
            for col_idx, value in enumerate(row, start=1):
                column_name = columns[col_idx - 1]
                cell_ref = f"{_col(col_idx)}{row_idx}"
                style = _cell_style(sheet_name=name, column=column_name, row_index=row_idx, value=str(value))
                lines.append(f'<c r="{cell_ref}" s="{style}" t="s"><v>{sst(value)}</v></c>')
                if name == "Output Data" and row_idx > 1 and column_name == "Source URL" and str(value).strip():
                    rel_id = f"rIdLink{len(hyperlinks) + 1}"
                    hyperlinks.append((cell_ref, rel_id))
                    hyperlink_rels.append(
                        f'<Relationship Id="{rel_id}" '
                        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
                        f'Target="{escape(str(value), quote=True)}" TargetMode="External"/>'
                    )
            lines.append("</row>")
        lines.append("</sheetData>")
        if name == "Output Data":
            last_row = max(2, len(rows) + 1)
            lines.append(f'<autoFilter ref="A1:{_col(len(columns))}{last_row}"/>')
            if hyperlinks:
                lines.append("<hyperlinks>")
                for cell_ref, rel_id in hyperlinks:
                    lines.append(f'<hyperlink ref="{cell_ref}" r:id="{rel_id}"/>')
                lines.append("</hyperlinks>")
        lines.append("</worksheet>")
        sheet_xml[f"xl/worksheets/sheet{sheet_idx}.xml"] = "".join(lines)
        if hyperlink_rels:
            sheet_rels[f"xl/worksheets/_rels/sheet{sheet_idx}.xml.rels"] = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + "".join(hyperlink_rels)
                + "</Relationships>"
            )

    workbook_sheets = []
    workbook_rels = []
    content_types = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
    ]
    for idx, (name, _, _) in enumerate(sheets, start=1):
        workbook_sheets.append(f'<sheet name="{escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>')
        workbook_rels.append(
            f'<Relationship Id="rId{idx}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{idx}.xml"/>'
        )
        content_types.append(
            f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    for rel_path in sheet_rels:
        part_name = rel_path.replace("\\", "/")
        content_types.append(
            f'<Override PartName="/{part_name}" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        )
    content_types.append("</Types>")

    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "".join(content_types))
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets>{"".join(workbook_sheets)}</sheets></workbook>',
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(workbook_rels)
            + '<Relationship Id="rIdSharedStrings" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
            + '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            + "</Relationships>",
        )
        zf.writestr("xl/styles.xml", _styles_xml())
        zf.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared)}" uniqueCount="{len(shared)}">'
            + "".join(f"<si><t>{escape(text)}</t></si>" for text in shared)
            + "</sst>",
        )
        for name, xml in sheet_xml.items():
            zf.writestr(name, xml)
        for name, xml in sheet_rels.items():
            zf.writestr(name, xml)
    tmp.replace(path)


def _assert_submission_outputs_match(csv_path: Path, json_path: Path, xlsx_path: Path) -> None:
    json_rows = json.loads(json_path.read_text(encoding="utf-8"))
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        csv_rows = list(csv.DictReader(handle))
    xlsx_rows = _read_output_data_xlsx(xlsx_path)
    normalized = {
        "json": _sorted_submission_rows(json_rows),
        "csv": _sorted_submission_rows(csv_rows),
        "xlsx": _sorted_submission_rows(xlsx_rows),
    }
    if normalized["json"] != normalized["csv"] or normalized["json"] != normalized["xlsx"]:
        raise RuntimeError("Submission export mismatch: CSV, JSON, and XLSX do not contain identical 13-column records.")
    _assert_submission_rationales_valid(normalized["json"])


def _sorted_submission_rows(rows: list[dict]) -> list[dict[str, str]]:
    key_fields = ("Economy", "Indicator ID", "Law Name", "Article / Section", "Source URL")
    clean_rows = [{column: _cell_text(row.get(column, "")) for column in SUBMISSION_COLUMNS} for row in rows]
    return sorted(clean_rows, key=lambda row: tuple(row[field] for field in key_fields))


def _normalized_law_name(economy: str, law_name: str, law_number_ref: str = "") -> str:
    if str(economy or "").casefold() != "malaysia":
        return law_name
    token = re.sub(r"\D+", "", law_name or law_number_ref)
    if law_name.strip().isdigit() and token == "291":
        return "Patents Act 1983"
    if law_name.strip().isdigit() and token == "332":
        return "Copyright Act 1987"
    return law_name


def _assert_submission_rationales_valid(rows: list[dict]) -> None:
    failures: list[str] = []
    for index, row in enumerate(rows, start=1):
        reasons = validate_submission_rationale(str(row.get("Mapping Rationale") or ""))
        if not reasons:
            continue
        key = "|".join(
            str(row.get(column) or "")
            for column in ("Economy", "Indicator ID", "Law Name", "Article / Section")
        )
        failures.append(f"row {index} {key}: {', '.join(reasons)}")
    if failures:
        preview = "; ".join(failures[:20])
        suffix = f"; ... {len(failures) - 20} more" if len(failures) > 20 else ""
        raise RuntimeError(f"Submission Mapping Rationale validation failed: {preview}{suffix}")


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _read_output_data_xlsx(path: Path) -> list[dict[str, str]]:
    ns = {
        "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    }
    with zipfile.ZipFile(path) as zf:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        relmap = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall("rel:Relationship", ns)}
        first_sheet = workbook.find("a:sheets", ns)[0]
        rid = first_sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        target = "xl/" + relmap[rid].lstrip("/")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            shared_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for item in shared_root.findall("a:si", ns):
                shared.append("".join(t.text or "" for t in item.findall(".//a:t", ns)))
        sheet_root = ET.fromstring(zf.read(target))
        rows: list[list[str]] = []
        max_col = len(SUBMISSION_COLUMNS)
        for row in sheet_root.findall(".//a:sheetData/a:row", ns):
            values = [""] * max_col
            for cell in row.findall("a:c", ns):
                col_num = _column_number(cell.attrib.get("r", ""))
                if not 1 <= col_num <= max_col:
                    continue
                value = ""
                if cell.attrib.get("t") == "inlineStr":
                    inline = cell.find("a:is", ns)
                    if inline is not None:
                        value = "".join(t.text or "" for t in inline.findall(".//a:t", ns))
                else:
                    raw = cell.findtext("a:v", default="", namespaces=ns)
                    value = shared[int(raw)] if cell.attrib.get("t") == "s" and raw.isdigit() and int(raw) < len(shared) else raw
                values[col_num - 1] = value
            rows.append(values)
    if not rows:
        return []
    header = rows[0]
    if header != SUBMISSION_COLUMNS:
        raise RuntimeError("Submission XLSX Output Data header does not match the official 13-column order.")
    return [dict(zip(SUBMISSION_COLUMNS, row)) for row in rows[1:] if any(row)]


def _cell_style(*, sheet_name: str, column: str, row_index: int, value: str) -> int:
    if row_index == 1:
        return 1
    odd = row_index % 2 == 1
    if sheet_name != "Output Data":
        return 2 if odd else 3
    if column == "Source URL":
        return 4 if odd else 5
    if column == "Discovery Tag":
        return (6 if odd else 7) if value == "KNOWN" else (8 if odd else 9)
    if column == "Indicator ID":
        return (10 if odd else 11) if value.upper().startswith("P6-") else (12 if odd else 13)
    if column == "Confidence":
        if value == "1.00":
            return 14 if odd else 15
        if value == "0.70":
            return 16 if odd else 17
        return 18 if odd else 19
    return 2 if odd else 3


def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="4">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
        '<font><sz val="11"/><u/><color rgb="FF0563C1"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><color rgb="FF1F1F1F"/><name val="Calibri"/></font>'
        '</fonts>'
        '<fills count="12">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF8FBFF"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF1F6FB"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFE2F0D9"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFCE4D6"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFEAF2F8"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFAF3DD"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF5F9FD"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF7FBF4"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFFF8EE"/><bgColor indexed="64"/></patternFill></fill>'
        '</fills>'
        '<borders count="2">'
        '<border><left/><right/><top/><bottom/><diagonal/></border>'
        '<border><left style="thin"><color rgb="FFD0D7DE"/></left><right style="thin"><color rgb="FFD0D7DE"/></right><top style="thin"><color rgb="FFD0D7DE"/></top><bottom style="thin"><color rgb="FFD0D7DE"/></bottom><diagonal/></border>'
        '</borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="20">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"><alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center" horizontal="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="2" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" horizontal="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="9" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" horizontal="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="6" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" horizontal="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="11" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" horizontal="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="7" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" horizontal="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="9" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" horizontal="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="8" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" horizontal="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="11" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" horizontal="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" horizontal="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="10" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" horizontal="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="6" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" horizontal="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="11" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" horizontal="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="7" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" horizontal="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="9" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" horizontal="center" wrapText="1"/></xf>'
        '</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )


def _submission_confidence(record: AtomicEvidenceRecord) -> str:
    if record.decision_source == "human_review" and record.decision == "accepted" and record.citation_status == "verified":
        return "1.00"
    if record.decision != "accepted" or record.citation_status != "verified":
        return "0.00"
    try:
        numeric = float(record.confidence or 0.0)
    except Exception:
        numeric = 0.0
    if numeric < 0.9 or _has_submission_warning(record):
        return "0.70"
    return "0.90"


def _has_submission_warning(record: AtomicEvidenceRecord) -> bool:
    text = " ".join(
        [
            str(record.decision_reason or ""),
            str(record.notes or ""),
        ]
    ).casefold()
    return any(token in text for token in ("warning", "repair", "normalized", "normalised", "auto-fix", "autofix"))


def _methodology_rows(*, excluded_treaty_rows: int) -> list[dict[str, str]]:
    return [
        {
            "Topic": "Submission scope",
            "Detail": "Output Data contains only accepted domestic-law provision evidence with verified citation.",
        },
        {
            "Topic": "International agreements",
            "Detail": "International agreement evidence is used for P6-I5 status and supporting audit outputs, but treaty evidence is not exported as domestic-law provision rows in Output Data.",
        },
        {
            "Topic": "Discovery Tag",
            "Detail": "KNOWN = the same economy/indicator/legal measure appears in the supplied Legal Inventory baseline. NEW = a valid measure not present in that baseline.",
        },
        {
            "Topic": "Confidence",
            "Detail": "1.00 = accepted human decision with verified citation; 0.90 = accepted model decision with verified citation and no warning; 0.70 = accepted model decision with verified citation and non-substantive warning or repair trace.",
        },
        {
            "Topic": "Metadata fields",
            "Detail": "Law Number / Ref and Last Amended are populated only from structured local official metadata. Unknown values remain blank.",
        },
        {
            "Topic": "Extraction and validation",
            "Detail": "Zone 1 extracts official legal documents and provision metadata. Zone 2 maps accepted evidence to RDTII indicators and validates citations before export.",
        },
    ]


def _structured_official_number(row: dict) -> str:
    value = str(row.get("official_number") or "").strip()
    return value if value and not _looks_like_internal_token(value) else ""


def _structured_last_amended(row: dict) -> str:
    for key in ("last_amended", "last_amended_year", "last_updated_year", "amended_year"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _strip_trailing_subparts(provision_id: str) -> str:
    return re.sub(r"(?:\([^()]+\))+$", "", provision_id or "")


def _looks_like_internal_token(value: str) -> bool:
    text = str(value or "")
    return "::" in text or text.casefold().startswith("sg-")


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _column_number(cell_ref: str) -> int:
    col = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    total = 0
    for ch in col:
        total = total * 26 + (ord(ch) - 64)
    return total


def _col(index: int) -> str:
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out
