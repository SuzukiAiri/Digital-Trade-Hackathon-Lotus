"""Write per-source provision corpora, parser logs, and manifests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from rdtii_tool.cache import safe_filename
from rdtii_tool.document_models import (
    CorpusSection,
    ParserLogEntry,
)
from rdtii_tool.ingestion.parser_router import ParseOutcome
from rdtii_tool.output_writer import write_json_records


class SectionCorpusWriter:
    """Materialize parser outcomes into source-owned JSONL files."""

    def __init__(self, output_dir: str | Path, *, country: str = "SG") -> None:
        self.output_dir = Path(output_dir)
        self.country = country.upper()
        country_code = self.country.lower()
        self.sources_dir = self.output_dir / "corpus" / "sources"
        self.manifest_path = (
            self.output_dir / "corpus" / f"{country_code}_corpus_manifest.json"
        )
        self.parser_log_path = self.output_dir / "parser_log.jsonl"
        self.summary_path = self.output_dir / "corpus_summary.json"
        self.sso_documents_path = (
            self.output_dir / f"{country_code}_sso_documents.json"
        )
        self.granularity_report_path = (
            self.output_dir / "direct_html_parser_granularity_report.md"
        )

    def write(
        self,
        outcomes: Iterable[ParseOutcome],
        *,
        started_at: str,
        finished_at: str | None = None,
    ) -> tuple[list[CorpusSection], Path, Path, Path]:
        materialized = list(outcomes)
        rows_by_source: dict[str, list[CorpusSection]] = {}
        for outcome in materialized:
            source_key = self._source_key(outcome)
            rows_by_source.setdefault(source_key, []).extend(
                self._corpus_sections(outcome, source_key=source_key)
            )

        corpus_sections = [
            row
            for rows in rows_by_source.values()
            for row in rows
        ]
        parser_logs = [outcome.parser_log for outcome in materialized]
        sso_documents = [
            outcome.document
            for outcome in materialized
            if outcome.document is not None
        ]

        self._prune_stale_source_corpora(set(rows_by_source))
        output_paths = self._write_source_corpora(rows_by_source)
        self._write_jsonl(self.parser_log_path, parser_logs)
        write_json_records(sso_documents, self.sso_documents_path)
        manifest = self._write_manifest(
            materialized,
            rows_by_source=rows_by_source,
            output_paths=output_paths,
            started_at=started_at,
            finished_at=finished_at,
        )
        self._write_summary(
            parser_logs,
            rows_by_source=rows_by_source,
            output_paths=output_paths,
            started_at=started_at,
            finished_at=finished_at,
        )
        self._write_granularity_report(
            rows_by_source,
            output_paths=output_paths,
            manifest=manifest,
        )
        return (
            corpus_sections,
            self.manifest_path,
            self.parser_log_path,
            self.summary_path,
        )

    def _corpus_sections(
        self,
        outcome: ParseOutcome,
        *,
        source_key: str,
    ) -> list[CorpusSection]:
        document = outcome.document
        if document is None:
            return []

        record = outcome.input_record
        review_flag = (
            not bool(record.source_url)
            or outcome.parser_log.parser_status != "success"
        )
        notes = " ".join(
            part.strip()
            for part in (
                record.notes,
                document.lifecycle_notes,
                (
                    "Original official URL is missing; confirm provenance before "
                    "using this text as evidence."
                    if not record.source_url
                    else ""
                ),
            )
            if part and part.strip()
        )
        canonical_url = str(
            record.metadata.get("canonical_url", "")
        ).strip()
        successful_url = record.source_url or document.source_url
        source_url = canonical_url or successful_url
        source_type = self._source_type(document.source_type)
        rows = []
        for section in document.sections:
            rows.append(
                CorpusSection(
                    country=self.country,
                    economy=document.economy or "Singapore",
                    source_key=source_key,
                    law_title=document.law_name or record.title,
                    source_url=source_url,
                    successful_url=successful_url,
                    source_type=source_type,
                    legal_rank=document.legal_rank or record.legal_rank,
                    version_status=document.version_status or "unknown",
                    current_version_date=document.current_version_date,
                    part=section.part,
                    division=section.division,
                    schedule=section.schedule,
                    provision_type=section.provision_type,
                    provision_number=(
                        section.provision_number or section.section_id
                    ),
                    heading=section.heading,
                    text=section.text,
                    word_count=len(section.text.split()),
                    char_count=len(section.text),
                    raw_file_path=self._display_path(record.input_path),
                    parser="sso_direct_html",
                    parser_status=outcome.parser_log.parser_status,
                    review_flag=review_flag,
                    notes=notes,
                )
            )
        return rows

    def _write_source_corpora(
        self,
        rows_by_source: dict[str, list[CorpusSection]],
    ) -> dict[str, Path]:
        output_paths = {}
        for source_key, rows in sorted(rows_by_source.items()):
            path = self.sources_dir / source_key / "sg_sections.jsonl"
            self._write_jsonl(path, rows)
            output_paths[source_key] = path
        return output_paths

    def _write_manifest(
        self,
        outcomes: list[ParseOutcome],
        *,
        rows_by_source: dict[str, list[CorpusSection]],
        output_paths: dict[str, Path],
        started_at: str,
        finished_at: str | None,
    ) -> dict:
        sources = []
        for outcome in outcomes:
            source_key = self._source_key(outcome)
            rows = rows_by_source.get(source_key, [])
            record = outcome.input_record
            document = outcome.document
            sources.append(
                {
                    "source_key": source_key,
                    "law_title": (
                        document.law_name
                        if document is not None
                        else record.title
                    ),
                    "source_url": str(
                        record.metadata.get("canonical_url", "")
                    ).strip() or record.source_url,
                    "successful_url": record.source_url,
                    "source_type": (
                        self._source_type(document.source_type)
                        if document is not None
                        else record.document_type
                    ),
                    "legal_rank": (
                        document.legal_rank
                        if document is not None
                        else record.legal_rank
                    ),
                    "parser_status": outcome.parser_log.parser_status,
                    "provisions_extracted": len(rows),
                    "corpus_file": self._display_path(
                        output_paths[source_key]
                    ),
                    "raw_file_path": self._display_path(record.input_path),
                    "error_type": outcome.parser_log.error_type,
                    "error_message": outcome.parser_log.error_message,
                }
            )

        manifest = {
            "country": self.country,
            "economy": "Singapore",
            "generated_at": finished_at
            or datetime.now(timezone.utc).isoformat(),
            "started_at": started_at,
            "total_sources": len(sources),
            "total_documents": len(outcomes),
            "total_provisions_extracted": sum(
                len(rows) for rows in rows_by_source.values()
            ),
            "combined_full_text_file_written": False,
            "sources": sources,
        }
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest

    def _write_summary(
        self,
        logs: list[ParserLogEntry],
        *,
        rows_by_source: dict[str, list[CorpusSection]],
        output_paths: dict[str, Path],
        started_at: str,
        finished_at: str | None,
    ) -> None:
        total = sum(len(rows) for rows in rows_by_source.values())
        summary = {
            "country": self.country,
            "documents_seen": len(logs),
            "documents_parsed": sum(
                log.parser_status in {"success", "partial"} for log in logs
            ),
            "documents_failed": sum(
                log.parser_status == "failed" for log in logs
            ),
            "total_provisions_extracted": total,
            "combined_full_text_file_written": False,
            "manifest_path": self._display_path(self.manifest_path),
            "outputs_by_source": {
                source_key: {
                    "provisions_extracted": len(
                        rows_by_source.get(source_key, [])
                    ),
                    "path": self._display_path(path),
                }
                for source_key, path in sorted(output_paths.items())
            },
            "started_at": started_at,
            "finished_at": finished_at
            or datetime.now(timezone.utc).isoformat(),
        }
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_granularity_report(
        self,
        rows_by_source: dict[str, list[CorpusSection]],
        *,
        output_paths: dict[str, Path],
        manifest: dict,
    ) -> None:
        total = int(manifest["total_provisions_extracted"])
        lines = [
            "# Direct HTML Parser Granularity Report",
            "",
            "- Previous top-level record count: 34",
            f"- New provision-level record count: {total}",
            "- Extraction root: `#legis`",
            "- Table of Contents rows included: no",
            "- Combined full-text corpus written: no",
            f"- Corpus manifest: `{self._display_path(self.manifest_path)}`",
            "",
            "## Provisions By Source",
            "",
            "| Source key | Provisions | Output |",
            "|---|---:|---|",
        ]
        for source_key, path in sorted(output_paths.items()):
            lines.append(
                f"| `{source_key}` | "
                f"{len(rows_by_source.get(source_key, []))} | "
                f"`{self._display_path(path)}` |"
            )

        examples = (
            ("pdpa_2012", "Personal Data Protection Act"),
            ("pdp_regulations_2021", "Personal Data Protection Regulations"),
            ("payment_services_act_2019", "Payment Services Act"),
            ("banking_act_1970", "Banking Act"),
        )
        lines.extend(["", "## Provision Examples", ""])
        for source_key, label in examples:
            lines.append(f"### {label}")
            rows = rows_by_source.get(source_key, [])[:3]
            if not rows:
                lines.append("- No provision records extracted.")
                continue
            for row in rows:
                snippet = self._markdown_text(row.text[:180])
                lines.append(
                    f"- {row.provision_type} {row.provision_number}: "
                    f"**{self._markdown_text(row.heading)}** - {snippet}"
                )

        lines.extend(
            [
                "",
                "## Notes And Limitations",
                "",
                (
                    "- SSO canonical responses lazy-load later Parts. The direct "
                    "downloader assembles the official fragments exposed by "
                    "`/Details/GetLazyLoadContent` before parsing."
                ),
                (
                    "- Provision extraction is based on SSO `div.prov1` "
                    "containers. Part, Division, and Schedule labels are retained "
                    "when present in the ancestor structure."
                ),
                (
                    "- Whitespace is normalized, but provision wording is not "
                    "summarized, classified, scored, or mapped to RDTII indicators."
                ),
            ]
        )
        self.granularity_report_path.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )

    def _prune_stale_source_corpora(
        self,
        current_source_keys: set[str],
    ) -> None:
        if not self.sources_dir.exists():
            return
        for directory in self.sources_dir.iterdir():
            if not directory.is_dir() or directory.name in current_source_keys:
                continue
            generated_file = directory / "sg_sections.jsonl"
            generated_file.unlink(missing_ok=True)
            try:
                directory.rmdir()
            except OSError:
                pass

    def _display_path(self, value: str | Path) -> str:
        path = Path(value)
        if not path.is_absolute():
            return str(path)
        try:
            return str(path.relative_to(self.output_dir.parent))
        except ValueError:
            return str(path)

    @staticmethod
    def _source_key(outcome: ParseOutcome) -> str:
        value = str(
            outcome.input_record.metadata.get("registry_key", "")
        ).strip()
        if value:
            return Path(safe_filename(value, suffix=".json")).stem
        title = (
            outcome.input_record.title
            or Path(outcome.input_record.input_path).stem
            or "unknown_source"
        )
        return Path(safe_filename(title, suffix=".json")).stem

    @staticmethod
    def _source_type(value: str) -> str:
        lowered = value.casefold()
        if lowered == "act":
            return "Act"
        if "subsidiary" in lowered:
            return "Subsidiary Legislation"
        return value

    @staticmethod
    def _write_jsonl(
        path: Path,
        records: Iterable[CorpusSection | ParserLogEntry],
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(
                    json.dumps(record.to_json_dict(), ensure_ascii=False)
                )
                handle.write("\n")
        return path

    @staticmethod
    def _markdown_text(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ").strip()
