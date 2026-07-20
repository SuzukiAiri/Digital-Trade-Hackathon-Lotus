"""Shared Zone 1 storage and cache helpers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from rdtii_tool.zone1.models import DocumentRef, DownloadCandidate


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return text or "document"


class Zone1Storage:
    def __init__(self, project_root: Path, economy: str) -> None:
        self.project_root = project_root
        self.economy = economy.casefold()
        self.data_root = project_root / "data" / "legal_sources" / self.economy
        self.output_root = project_root / "outputs" / "corpus" / self.economy
        for path in [
            self.data_root / "raw",
            self.data_root / "normalized",
            self.data_root / "metadata",
            self.data_root / "manifests",
            self.output_root / "manifests",
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def collection_key(self, document: DocumentRef) -> str:
        return safe_name(str(document.metadata.get("storage_collection") or document.collection)).casefold()

    def raw_path(self, document: DocumentRef, candidate: DownloadCandidate) -> Path:
        ext = str(candidate.metadata.get("extension") or self._extension(candidate.format))
        if not ext.startswith("."):
            ext = f".{ext}"
        filename = safe_name(str(candidate.metadata.get("filename") or f"{document.document_id}{ext}"))
        return self.data_root / "raw" / self.collection_key(document) / filename

    def normalized_path(self, document: DocumentRef) -> Path:
        return self.data_root / "normalized" / self.collection_key(document) / f"{safe_name(document.document_id)}.txt"

    def metadata_path(self, document: DocumentRef) -> Path:
        return self.data_root / "metadata" / self.collection_key(document) / f"{safe_name(document.document_id)}.json"

    def manifest_path(self, name: str = "zone1_manifest.jsonl") -> Path:
        return self.data_root / "manifests" / name

    def legacy_manifest_rows(self, names: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name in names:
            rows.extend(read_jsonl(self.data_root / "manifests" / name))
            rows.extend(read_jsonl(self.output_root / name))
        return rows

    def existing_normalized(self, document: DocumentRef) -> Path | None:
        candidates = [self.normalized_path(document)]
        for value in document.metadata.get("legacy_normalized_paths", []) or []:
            path = self.resolve_existing_path(str(value))
            if path:
                candidates.append(path)
        for path in candidates:
            if path.exists() and path.stat().st_size > 0:
                return path
        return None

    def existing_raw(self, document: DocumentRef) -> Path | None:
        for value in document.metadata.get("legacy_raw_paths", []) or []:
            path = self.resolve_existing_path(str(value))
            if path:
                return path
        root = self.data_root / "raw" / self.collection_key(document)
        if root.exists():
            for path in root.glob(f"{safe_name(document.document_id)}*"):
                if path.is_file() and path.stat().st_size > 0:
                    return path
        return None

    def resolve_existing_path(self, value: str) -> Path | None:
        if not value:
            return None
        normalized = value.replace("\\", "/")
        candidates = [Path(value)]
        if not Path(value).is_absolute():
            candidates.append(self.project_root / value)
        for marker in ("data/legal_sources/", "outputs/corpus/"):
            if marker in normalized:
                candidates.append(self.project_root / normalized[normalized.index(marker):])
        for path in candidates:
            if path.exists() and path.stat().st_size > 0:
                return path
        return None

    def save_raw(self, document: DocumentRef, candidate: DownloadCandidate, content: bytes) -> Path:
        path = self.raw_path(document, candidate)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def save_normalized(self, document: DocumentRef, text: str) -> Path:
        path = self.normalized_path(document)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        return path

    def save_metadata(self, document: DocumentRef, row: dict[str, Any]) -> Path:
        path = self.metadata_path(document)
        write_json(path, row)
        return path

    @staticmethod
    def _extension(fmt: str) -> str:
        return {
            "html": ".html",
            "epub": ".epub",
            "word": ".docx",
            "docx": ".docx",
            "rtf": ".rtf",
            "doc": ".doc",
            "pdf": ".pdf",
            "text": ".txt",
        }.get(fmt.casefold(), ".bin")

