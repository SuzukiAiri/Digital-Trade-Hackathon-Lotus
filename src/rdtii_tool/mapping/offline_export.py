"""Offline submission export from final_rows.jsonl only."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .discovery_tags import load_legal_inventory_registry
from .submission_exporter import SUBMISSION_COLUMNS, _assert_submission_outputs_match, _write_xlsx
from .submission_rationale import sanitize_submission_row


ECONOMY_ORDER = ("singapore", "australia", "malaysia")


def export_completed_submissions(project_root: Path, economies: list[str], pillars: set[int]) -> dict:
    if pillars not in ({4}, {6, 7}):
        raise RuntimeError("unsupported pillar combination")
    scope_slug = "p4" if pillars == {4} else "p6_p7"
    ordered = [economy for economy in ECONOMY_ORDER if economy in set(economies)]
    ordered.extend(economy for economy in economies if economy not in ordered)
    final_row_paths: dict[str, Path] = {}
    for economy in ordered:
        submission_dir = Path(project_root) / "outputs" / "corpus" / economy / "submission"
        if pillars == {4}:
            submission_dir = submission_dir / "p4"
        final_rows_path = submission_dir / "final_rows.jsonl"
        if not final_rows_path.exists():
            raise RuntimeError(f"final_rows.jsonl missing; run final-audit first: {final_rows_path}")
        final_row_paths[economy] = final_rows_path

    registry = load_legal_inventory_registry(project_root)
    final_dir = Path(project_root) / "outputs" / "final_submission"
    final_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, dict] = {"baseline": registry.summary(), "economies": {}}
    all_rows: list[dict[str, str]] = []
    for economy in ordered:
        submission_dir = final_row_paths[economy].parent
        rows = [_with_discovery_tag(row, registry) for row in _load_final_rows(final_row_paths[economy])]
        _write_outputs(submission_dir, f"{economy}_{scope_slug}", rows)
        _write_outputs(final_dir, f"{economy}_{scope_slug}", rows)
        all_rows.extend(rows)
        summary["economies"][economy] = {
            "rows": len(rows),
            "known": sum(1 for row in rows if row.get("Discovery Tag") == "KNOWN"),
            "new": sum(1 for row in rows if row.get("Discovery Tag") == "NEW"),
            "source": str(submission_dir / "final_rows.jsonl"),
        }
    _write_outputs(final_dir, f"rdtii_{scope_slug}_all_economies", all_rows)
    summary["combined"] = {
        "rows": len(all_rows),
        "known": sum(1 for row in all_rows if row.get("Discovery Tag") == "KNOWN"),
        "new": sum(1 for row in all_rows if row.get("Discovery Tag") == "NEW"),
    }
    summary_name = "p4_discovery_tag_summary.json" if pillars == {4} else "discovery_tag_summary.json"
    (final_dir / summary_name).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (final_dir / f"{scope_slug}_release_validation_report.md").write_text(
        _release_report(summary, scope_slug=scope_slug), encoding="utf-8"
    )
    return summary


def _load_final_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise RuntimeError(f"final_rows.jsonl missing; run final-audit first: {path}")
    rows: list[dict[str, str]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        row = payload.get("row") if isinstance(payload, dict) else None
        if not isinstance(row, dict):
            raise RuntimeError(f"final_rows.jsonl row {line_no} missing row object: {path}")
        rows.append({column: str(row.get(column) or "") for column in SUBMISSION_COLUMNS})
    return rows


def _with_discovery_tag(row: dict[str, str], registry) -> dict[str, str]:
    out = sanitize_submission_row(row)
    match = registry.match_row(out)
    out["Discovery Tag"] = match.discovery_tag
    return out


def _write_outputs(output_dir: Path, prefix: str, rows: list[dict[str, str]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [{column: str(row.get(column) or "") for column in SUBMISSION_COLUMNS} for row in (sanitize_submission_row(row) for row in rows)]
    csv_path = output_dir / f"{prefix}.csv"
    json_path = output_dir / f"{prefix}.json"
    xlsx_path = output_dir / f"{prefix}.xlsx"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUBMISSION_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_xlsx(xlsx_path, rows=rows, methodology_rows=_methodology_rows())
    _assert_submission_outputs_match(csv_path, json_path, xlsx_path)


def _methodology_rows() -> list[dict[str, str]]:
    return [
        {"Topic": "Final authority", "Detail": "This export reads final_rows.jsonl only."},
        {"Topic": "Discovery Tag", "Detail": "KNOWN = same economy/indicator/legal measure appears in the supplied Legal Inventory baseline. NEW = not present in that baseline."},
    ]


def _release_report(summary: dict, *, scope_slug: str = "p6_p7") -> str:
    title = "P4" if scope_slug == "p4" else "P6/P7"
    lines = [
        f"# RDTII {title} release validation report",
        "",
        f"Baseline file hash: `{summary['baseline'].get('file_hash', '')}`",
        "",
        "| Economy | Rows | KNOWN | NEW |",
        "|---|---:|---:|---:|",
    ]
    for economy, item in summary.get("economies", {}).items():
        lines.append(f"| {economy} | {item['rows']} | {item['known']} | {item['new']} |")
    lines.extend(["", f"Combined rows: {summary.get('combined', {}).get('rows', 0)}"])
    return "\n".join(lines) + "\n"
