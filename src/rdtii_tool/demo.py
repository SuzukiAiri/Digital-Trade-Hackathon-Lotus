"""Offline demo runner for the GitHub release package."""

from __future__ import annotations

import csv
import importlib.metadata
import json
import re
from importlib import resources
from pathlib import Path


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


def run_demo(*, mode: str, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    if mode == "offline":
        return _run_offline_demo(output_dir)
    if mode == "pdf":
        return _run_pdf_demo(output_dir)
    raise ValueError(f"Unsupported demo mode: {mode}")


def _run_offline_demo(output_dir: Path) -> dict:
    fixture = _load_fixture("offline_demo.json")
    rows = []
    for item in fixture["records"]:
        source_text = _normalise_text(item["source_text"])
        snippet = _normalise_text(item["verbatim_snippet"])
        if snippet not in source_text:
            raise RuntimeError(f"Demo citation validation failed for {item['indicator_id']} {item['article']}")
        rows.append(
            {
                "Economy": item["economy"],
                "Law Name": item["law_name"],
                "Law Number / Ref": item["law_number"],
                "Last Amended": item["last_amended"],
                "Indicator ID": item["indicator_id"],
                "Article / Section": item["article"],
                "Discovery Tag": item["discovery_tag"],
                "Location Reference": item["location_reference"],
                "Verbatim Snippet": item["verbatim_snippet"],
                "Mapping Rationale": item["mapping_rationale"],
                "Source URL": item["source_url"],
                "Confidence": item["confidence"],
                "Notes": item["notes"],
            }
        )
    csv_path = output_dir / "rdtii_demo.csv"
    json_path = output_dir / "rdtii_demo.json"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUBMISSION_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "mode": "offline",
        "records": len(rows),
        "csv": str(csv_path),
        "json": str(json_path),
        "checks": {
            "canonical_normalization": "pass",
            "mapping_schema": "pass",
            "citation_validation": "pass",
            "csv_json_export": "pass",
        },
    }
    (output_dir / "demo_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def _run_pdf_demo(output_dir: Path) -> dict:
    try:
        docling_version = importlib.metadata.version("docling")
        import docling  # noqa: F401
    except Exception as exc:
        raise RuntimeError(
            "PDF processing requires the optional dependency:\n"
            "pip install -e \".[pdf]\""
        ) from exc
    pdf_path = output_dir / "synthetic_rdtii_fixture.pdf"
    pdf_path.write_bytes(_minimal_pdf_bytes())
    summary = {
        "mode": "pdf",
        "docling_import": "pass",
        "docling_version": docling_version,
        "fixture_pdf": str(pdf_path),
        "note": "The PDF demo verifies the optional Docling installation without network, OCR, or model calls.",
    }
    (output_dir / "pdf_demo_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def _load_fixture(name: str) -> dict:
    text = resources.files("rdtii_tool").joinpath("demo_fixtures", name).read_text(encoding="utf-8")
    return json.loads(text)


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _minimal_pdf_bytes() -> bytes:
    return b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length 74 >>
stream
BT /F1 12 Tf 40 90 Td (RDTII synthetic PDF fixture for release verification.) Tj ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000241 00000 n 
0000000365 00000 n 
trailer
<< /Size 6 /Root 1 0 R >>
startxref
435
%%EOF
"""
