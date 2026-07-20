#!/usr/bin/env python3
"""Validate the GitHub release package without network or model calls."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORMAL_COLUMNS = [
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
ECONOMIES = ("singapore", "australia", "malaysia")
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "demo_output", "demo_pdf_output"}


def main() -> int:
    errors: list[str] = []
    summary = {
        "final_submit": validate_final_submit(errors),
        "layout": validate_layout(errors),
        "secrets": validate_secret_scan(errors),
        "sizes": validate_file_sizes(errors),
    }
    if errors:
        print("Release verification: FAIL")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Release verification: PASS")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def validate_final_submit(errors: list[str]) -> dict:
    manifest_path = ROOT / "FINAL_SUBMIT_SHA256.json"
    if not manifest_path.exists():
        errors.append("FINAL_SUBMIT_SHA256.json missing")
        manifest = {}
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary: dict[str, dict] = {}
    for economy in ECONOMIES:
        directory = ROOT / "outputs" / "corpus" / economy / "final_submit"
        if not directory.exists():
            errors.append(f"missing final_submit directory: {directory.relative_to(ROOT)}")
            continue
        csv_files = sorted(directory.glob("*.csv"))
        json_files = sorted(directory.glob("*.json"))
        if len(csv_files) != 1 or len(json_files) != 1:
            errors.append(f"{economy} final_submit must contain one CSV and one JSON")
            continue
        csv_path, json_path = csv_files[0], json_files[0]
        rows = read_csv(csv_path, errors)
        json_rows = read_json_rows(json_path, errors)
        if len(rows) != len(json_rows):
            errors.append(f"{economy} CSV/JSON row mismatch: {len(rows)} != {len(json_rows)}")
        validate_rows(economy, rows, errors)
        for path in (csv_path, json_path):
            rel = path.relative_to(ROOT).as_posix()
            expected = manifest.get(rel, {}).get("sha256")
            actual = sha256(path)
            if expected and expected != actual:
                errors.append(f"final_submit SHA mismatch: {rel}")
            if not expected:
                errors.append(f"final_submit hash missing from manifest: {rel}")
        summary[economy] = {
            "csv": csv_path.relative_to(ROOT).as_posix(),
            "json": json_path.relative_to(ROOT).as_posix(),
            "rows": len(rows),
            "csv_sha256": sha256(csv_path),
            "json_sha256": sha256(json_path),
        }
    return summary


def read_csv(path: Path, errors: list[str]) -> list[dict]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            if reader.fieldnames != FORMAL_COLUMNS:
                errors.append(f"formal columns mismatch: {path.relative_to(ROOT)}")
            return rows
    except Exception as exc:
        errors.append(f"CSV parse failed: {path.relative_to(ROOT)}:{type(exc).__name__}")
        return []


def read_json_rows(path: Path, errors: list[str]) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            rows = data.get("rows") or data.get("data")
            if isinstance(rows, list):
                return rows
        errors.append(f"JSON rows not found: {path.relative_to(ROOT)}")
    except Exception as exc:
        errors.append(f"JSON parse failed: {path.relative_to(ROOT)}:{type(exc).__name__}")
    return []


def validate_rows(economy: str, rows: list[dict], errors: list[str]) -> None:
    tags = {row.get("Discovery Tag", "") for row in rows}
    if not tags <= {"NEW", "KNOWN"}:
        errors.append(f"{economy} invalid Discovery Tag values: {sorted(tags)}")
    seen: set[str] = set()
    for index, row in enumerate(rows, start=2):
        for column in ("Indicator ID", "Article / Section", "Verbatim Snippet", "Source URL"):
            if not str(row.get(column) or "").strip():
                errors.append(f"{economy} row {index} has empty {column}")
        identity = "|".join(
            [
                str(row.get("Economy") or ""),
                str(row.get("Indicator ID") or ""),
                str(row.get("Law Name") or "").casefold(),
                str(row.get("Article / Section") or ""),
                hashlib.sha1(str(row.get("Verbatim Snippet") or "").encode("utf-8")).hexdigest(),
            ]
        )
        if identity in seen:
            errors.append(f"{economy} duplicate canonical record at row {index}")
        seen.add(identity)


def validate_layout(errors: list[str]) -> dict:
    forbidden = [
        ROOT / "outputs" / "final_submission",
        *[ROOT / "outputs" / "corpus" / economy / "submission" for economy in ECONOMIES],
    ]
    for path in forbidden:
        if path.exists():
            errors.append(f"forbidden release directory present: {path.relative_to(ROOT)}")
    for economy in ECONOMIES:
        corpus = ROOT / "outputs" / "corpus" / economy
        if not corpus.exists():
            continue
        for child in corpus.iterdir():
            if child.name != "final_submit":
                errors.append(f"full corpus artifact present: {child.relative_to(ROOT)}")
    return {"forbidden_directories_checked": len(forbidden)}


def validate_secret_scan(errors: list[str]) -> dict:
    secret_patterns = [
        ("openai_key", re.compile("s" + r"k-[A-Za-z0-9_-]{20,}")),
        ("github_token", re.compile("github" + "_pat_")),
        ("authorization_header", re.compile("Author" + r"ization\s*[:=]\s*(Bearer|Basic|Token)\b", re.IGNORECASE)),
        ("password_assignment", re.compile("password" + "=")),
        ("home_path", re.compile("/" + "home/")),
        ("windows_user_path", re.compile("/mnt/c/" + "Users/")),
        ("windows_user_path", re.compile("C:" + r"\\Users\\")),
    ]
    hits = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or _skip(path):
            continue
        if path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for label, pattern in secret_patterns:
            if pattern.search(text):
                rel = path.relative_to(ROOT).as_posix()
                hits.append(f"{label}:{rel}")
                break
    if hits:
        errors.extend([f"secret/local-path risk: {hit}" for hit in sorted(set(hits))])
    return {"hits": len(set(hits))}


def validate_file_sizes(errors: list[str]) -> dict:
    max_size = 0
    max_path = ""
    for path in ROOT.rglob("*"):
        if not path.is_file() or _skip(path):
            continue
        size = path.stat().st_size
        if size > max_size:
            max_size = size
            max_path = path.relative_to(ROOT).as_posix()
        if size > 50 * 1024 * 1024:
            errors.append(f"file exceeds 50 MB: {path.relative_to(ROOT)}")
    return {"max_file": max_path, "max_size": max_size}


def _skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
