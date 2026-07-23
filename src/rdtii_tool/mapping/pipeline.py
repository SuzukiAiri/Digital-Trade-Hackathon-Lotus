"""Production RDTII P6/P7 mapping pipeline."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

from .aggregator import (
    AGGREGATION_VERSION,
    P4_AGGREGATION_VERSION,
    aggregate_p4_framework_conclusions,
    aggregate_provision_measures,
)
from .candidate_router import P4_ROUTING_VERSION, ROUTING_VERSION, build_routing_package
from .economy_profiles import domestic_terms
from .discovery_tags import KnownEvidenceRegistry, load_legal_inventory_registry
from .citation_validator import (
    normalized_snippet_hash,
    validate_docling_page_citation,
    validate_external_citation,
    validate_provision_citation,
)
from .indicator_specs import (
    FRAMEWORK_REVIEW_ELEMENTS,
    FRAMEWORK_REVIEW_EXCLUSIONS,
    FRAMEWORK_REVIEWER_CONTRACT_VERSION,
    canonical_framework_element,
    framework_element_code,
    specs_for_group,
    INDICATOR_SPECS,
    INDICATOR_SPEC_VERSION,
    P4_INDICATOR_SPEC_VERSION,
    P6_REVIEW_ELEMENTS,
    P6_REVIEW_EXCLUSIONS,
    P7_REVIEW_ELEMENTS_BY_GROUP,
    P7_REVIEW_EXCLUSIONS_BY_GROUP,
    P4_REVIEW_ELEMENTS_BY_GROUP,
    P4_REVIEW_EXCLUSIONS_BY_GROUP,
    source_family_hit,
)
from .human_review import (
    evidence_hash_for_task,
    import_completed_reviews,
    load_active_decisions,
    review_key_for_unbound_review,
    review_key_for_task,
    source_fingerprint_for_task,
    sync_human_review_workbook,
)
from .measure_extractor import (
    REVIEWER_CACHE_CONTRACT_VERSION,
    REVIEWER_PROMPT_VERSION,
    REVIEWER_SCHEMA_VERSION,
    P4_REVIEWER_SCHEMA_VERSION,
    all_prompt_versions,
    evidence_catalog_for_task,
    extract_decision,
    extract_reviewer_decision,
    mapper_model_name,
    prompt_version_for_task,
    review_model_name,
    reviewer_prompt_version_for_task,
)
from .model_config import api_key_available, pdf_mapper_model_name
from .economy_profiles import economy_profile
from .model_policy import (
    assert_model_allowed,
)
from .malaysia_p4_overrides import (
    apply_malaysia_p4_override,
    malaysia_p4_override_fingerprint,
)
from .models import (
    CandidateTask,
    FocalIntegrityAssessment,
    IndicatorMatch,
    MappingDecision,
    ProvisionContext,
    ReviewerDecision,
    ReviewerElementAssessment,
    ReviewerExclusionAssessment,
    ReviewerOptionalCheck,
    ReviewerAttributes,
    RequiredElementReview,
    AdjacentIndicatorReason,
    ValidatedTaskResult,
    AtomicEvidenceRecord,
    PDFDocumentTask,
    PDFEvidenceClaim,
    PDFMappingDecision,
    P4TreatyStatus,
)
from .p6_i5_status import check_agreements
from .p4_treaty_status import (
    P4_TREATY_REGISTRY_VERSION,
    load_p4_treaty_status,
    p4_treaty_status_row,
    validate_p4_treaty_status,
)
from .pdf_mapper import (
    PDF_CITATION_PROMPT_VERSION,
    PDF_OUTPUT_SCHEMA_VERSION,
    PDF_PROMPT_VERSION,
    P4_PDF_OUTPUT_SCHEMA_VERSION,
    P4_PDF_PROMPT_VERSION,
    extract_pdf_mapping_decision,
    pdf_prompt_version_for_task,
)
from rdtii_tool.parsers.docling_pdf import (
    DoclingPdfError,
    document_text as docling_document_text,
    extract_docling_pdf_artifact,
    docling_max_pages,
    docling_worker_count,
    load_docling_artifact,
    pdf_page_count,
    sha256_path as docling_sha256_path,
)
from .submission_exporter import SUBMISSION_COLUMNS, export_submission
from .submission_rationale import validate_submission_rationale
from .validators import P4_VALIDATION_VERSION, P6_TREATY_REVIEW_ELEMENTS, P6_TREATY_REVIEW_EXCLUSIONS, VALIDATION_VERSION, apply_reviewer_decisions, normalize_reviewer_decision, reviewer_retry_instructions, reviewer_schema_retry_needed, validate_decision


MAPPING_SCHEMA_VERSION = "rdtii-mapping-schema-v5-semantic-validation"
MAPPER_CACHE_SCHEMA_VERSION = "rdtii-mapper-cache-v4-executable-contracts"
P4_MAPPER_CACHE_SCHEMA_VERSION = "rdtii-p4-mapper-cache-v2-boundary-contracts"
RESOLVER_VERSION = "rdtii-resolver-v8-full-contracts"
FRAMEWORK_PIPELINE_VERSION = "rdtii-framework-pipeline-v3-status-only"
CITATION_VALIDATOR_VERSION = "rdtii-citation-validator-v2-exact"
OUTPUT_SCHEMA_VERSION = "rdtii-output-schema-v2-atomic-submission"
LEGACY_ROUTING_CACHE_VERSIONS = ("rdtii-routing-v3-indicator-spec", "rdtii-routing-v4-cost-guarded-recall")
DEFAULT_REVIEWER_REFRESH_GROUPS = {"P6_LOCATION", "P7_RETENTION", "P7_ACCOUNTABILITY"}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str))
        handle.write("\n")
        handle.flush()


def append_jsonl_atomic(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    payload = existing + json.dumps(row, ensure_ascii=False, default=str) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows), encoding="utf-8")
    tmp.replace(path)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


class MappingPipeline:
    def __init__(self, root: str | Path, *, economy: str = "Singapore", pillars: set[int] | None = None, live: bool = True) -> None:
        self.root = Path(root)
        self.project_root = self.root.parent.parent.parent
        self.economy = economy
        self.pillars = pillars or {6, 7}
        self.live = live
        self.economy_slug = self.economy.casefold().replace(" ", "_")
        self.scope_slug = "p4" if self.pillars == {4} else "p6_p7"
        self.output_prefix = f"{self.economy_slug}_{self.scope_slug}"
        self.mappings_dir = self.root / "mappings" if self.pillars == {6, 7} else self.root / "mappings" / "p4"
        self.output_dir = self.mappings_dir
        self.task_results_path = self.output_dir / f"{self.output_prefix}_task_results.jsonl"
        self.routing_audit_path = self.output_dir / f"{self.output_prefix}_routing_audit.jsonl"
        self.cache_path = self.mappings_dir / (
            "p6_p7_measure_cache.jsonl" if self.pillars == {6, 7} else "p4_measure_cache.jsonl"
        )
        self.summary_path = self.output_dir / f"{self.output_prefix}_mapping_summary.json"
        self.submission_dir = self.mappings_dir / "submission"
        self.pdf_direct_stats: dict[str, object] = {}
        self.cache_lock = threading.Lock()
        self.results_lock = threading.Lock()
        self.summary_lock = threading.Lock()
        self.human_review_import_report: dict = {}
        self.human_decisions: dict[str, dict] = {}
        self.persistent_human_decisions: dict[str, dict] = {}
        self._canonical_doc_meta_cache: dict[str, dict] | None = None
        self._canonical_provision_meta_cache: dict[str, dict[str, dict]] = {}

    def initialize(self) -> dict:
        routing = build_routing_package(self.iter_contexts(), self.economy, self.pillars)
        cache = self._load_cache()
        return {
            "provisions_loaded": routing.stats["provisions_scanned"],
            "provisions_scanned": routing.stats["provisions_scanned"],
            "primary_router_tasks": routing.stats["primary_router_tasks"],
            "audit_hits": routing.stats["audit_hits"],
            "audit_only_tasks_promoted": routing.stats["audit_only_tasks_promoted"],
            "candidate_tasks_created": len(routing.provision_tasks),
            "candidate_tasks_after_deduplication": len(routing.provision_tasks),
            "framework_element_tasks_created": int(routing.stats.get("framework_element_tasks") or 0),
            "cache_entries": len(cache),
            "mapper_model": mapper_model_name(),
            "reviewer_model": review_model_name(mapper_model_name()),
            "routing_version": P4_ROUTING_VERSION if self.pillars == {4} else ROUTING_VERSION,
            "prompt_versions": all_prompt_versions(),
            "validation_version": P4_VALIDATION_VERSION if self.pillars == {4} else VALIDATION_VERSION,
            "aggregation_version": AGGREGATION_VERSION,
        }

    def _set_output_dir(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.task_results_path = self.output_dir / f"{self.output_prefix}_task_results.jsonl"
        self.routing_audit_path = self.output_dir / f"{self.output_prefix}_routing_audit.jsonl"
        self.summary_path = self.output_dir / f"{self.output_prefix}_mapping_summary.json"
        self.submission_dir = self.output_dir / "submission"

    @staticmethod
    def _prepare_staging_dir(staging_dir: Path) -> None:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> dict:
        if self.pillars not in ({4}, {6, 7}):
            raise RuntimeError("unsupported pillar combination")
        if self.economy_slug not in {"singapore", "australia", "malaysia"}:
            raise RuntimeError("map-rdtii currently supports --economy singapore, australia, or malaysia")
        model = mapper_model_name()
        review_model = review_model_name(model)
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        staging_dir = self.mappings_dir / "staging" / run_id
        self._set_output_dir(staging_dir)
        self._prepare_staging_dir(staging_dir)
        started = time.time()
        routing = build_routing_package(self.iter_contexts(), self.economy, self.pillars)
        pdf_tasks = self.load_pdf_document_tasks()
        provisions_loaded = int(routing.stats["provisions_scanned"])
        provision_tasks = routing.provision_tasks
        self.human_review_import_report = import_completed_reviews(
            self.project_root, self.economy_slug, scope=self.scope_slug
        )
        self.human_decisions = load_active_decisions(
            self.project_root, self.economy_slug, scope=self.scope_slug
        )
        self.persistent_human_decisions = self._load_persistent_human_decisions()
        cache = self._load_cache()
        reviewer_migration = self._migrate_reviewer_cache_to_stable_keys(provision_tasks, cache, model, review_model)
        reviewer_plan = self._reviewer_cache_plan(provision_tasks, cache, model, review_model)
        self._typed_reviewer_contract_preflight(provision_tasks, cache, model, review_model)
        coverage = self._mapper_replay_coverage(provision_tasks, model, cache)
        pdf_coverage = self._pdf_replay_coverage(pdf_tasks, cache)
        skipped_missing_mapper_cache = 0
        before_snapshot = self._read_stable_current_snapshot()
        key_available = api_key_available()
        print("Mapper replay mode: cache-preferred; stale or missing tasks are processed automatically", flush=True)
        print(f"Mapper/Reviewer LLM calls available: {'yes' if self.live and key_available else 'no'}", flush=True)
        print("Output rebuild mode: full", flush=True)
        print("Reviewer schema mode: v3 atomic evidence", flush=True)
        print(f"Mapper cache entries found: {coverage['mapper_hits']}", flush=True)
        print(f"Candidate tasks: {len(provision_tasks)}", flush=True)
        print(f"Framework element tasks: {routing.stats.get('framework_element_tasks', 0)}", flush=True)
        self._print_pdf_direct_plan(pdf_tasks, pdf_coverage)
        print(
            f"Reviewer cache plan | total reviewer tasks: {reviewer_plan['total_reviewer_tasks']} | "
            f"cache hits: {reviewer_plan['cache_hits']} | cache misses: {reviewer_plan['cache_misses']} | "
            f"expected reviewer API calls: {reviewer_plan['expected_reviewer_api_calls']}",
            flush=True,
        )
        print(f"Reviewer cache misses by route: {json.dumps(reviewer_plan['misses_by_route'], ensure_ascii=False, sort_keys=True)}", flush=True)
        print(f"Reviewer cache misses by indicator: {json.dumps(reviewer_plan['misses_by_indicator'], ensure_ascii=False, sort_keys=True)}", flush=True)
        self._write_cache_compatibility_report(coverage, len(provision_tasks), cache)
        write_json(self.output_dir / "reviewer_cache_migration_report.json", reviewer_migration)
        write_json(self.output_dir / "reviewer_cache_plan.json", reviewer_plan)
        if self.live and not key_available and (coverage["missing_task_ids"] or pdf_coverage["missing_pdf_mapper_cache"]):
            missing_count = len(coverage["missing_task_ids"]) + int(pdf_coverage["missing_pdf_mapper_cache"])
            raise RuntimeError(
                "RDTII mapping needs model calls for stale or missing cache entries "
                f"({missing_count} tasks). Set OPENAI_API_KEY, then rerun the same command."
            )
        if not self.live:
            print("Cache-only compatibility mode: online Mapper/Reviewer calls are disabled.", flush=True)
        elif not key_available:
            print("No API key found; run can complete only if all Mapper, Reviewer, and PDF tasks have current compatible cache.", flush=True)
        stats = Counter()
        provision_results: list[ValidatedTaskResult] = []
        pdf_results: list[dict] = []
        max_workers = max(1, int(os.environ.get("RDTII_MAX_WORKERS", "5")))

        total_units = len(provision_tasks) + len(pdf_tasks)
        print(f"Provisions scanned: {routing.stats['provisions_scanned']}", flush=True)
        print(f"Primary router tasks: {routing.stats['primary_router_tasks']}", flush=True)
        print(f"Recall audit hits: {routing.stats['audit_hits']}", flush=True)
        print(f"Audit-only tasks promoted: {routing.stats['audit_only_tasks_promoted']}", flush=True)
        print(f"Framework instruments identified: {routing.stats['framework_instruments_identified']}", flush=True)
        print(f"Candidate tasks after deduplication: {routing.stats['candidate_tasks_after_deduplication']}", flush=True)
        print(
            f"RDTII mapping started | Provisions: {provisions_loaded} | Evidence tasks: {len(provision_tasks)} | "
            f"PDF documents: {len(pdf_tasks)} | Cache entries: {len(cache)} | Workers: {max_workers} | "
            f"Mapper model: {model} | Reviewer model: {review_model} | PDF mapper model: {pdf_mapper_model_name()}",
            flush=True,
        )
        self._write_progress_summary(provisions_loaded, routing.stats, len(provision_tasks), 0, provision_results, stats, started, pdf_tasks=pdf_tasks, pdf_results=pdf_results)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for task in provision_tasks:
                futures[executor.submit(self._process_provision_task, task, model, review_model, cache, False)] = ("provision", task)
            for task in pdf_tasks:
                futures[executor.submit(self._process_pdf_document_task, task, review_model, cache)] = ("pdf", task)
            for future in as_completed(futures):
                kind, task = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = self._pdf_error_result(task, str(exc)) if kind == "pdf" else self._error_result(task, model, str(exc), kind)
                with self.results_lock:
                    append_jsonl(self.task_results_path, result if isinstance(result, dict) else result.model_dump())
                    if isinstance(result, dict):
                        pdf_results.append(result)
                    else:
                        provision_results.append(result)
                if isinstance(result, dict):
                    self._update_pdf_stats(stats, result)
                else:
                    self._update_stats(stats, result)
                completed = len(provision_results) + len(pdf_results)
                self._print_progress(result, total_units, completed, stats, started, max_workers)
                if completed % 10 == 0:
                    self._write_progress_summary(provisions_loaded, routing.stats, len(provision_tasks), completed, provision_results, stats, started, pdf_tasks=pdf_tasks, pdf_results=pdf_results)

        self._write_progress_summary(provisions_loaded, routing.stats, len(provision_tasks), total_units, provision_results, stats, started, pdf_tasks=pdf_tasks, pdf_results=pdf_results)
        self._assert_terminal_state_reconciliation(provision_tasks, provision_results, pdf_tasks, pdf_results)
        summary = self._export_final_outputs(provisions_loaded, routing.stats, provision_tasks, provision_results, stats, started, before_snapshot, pdf_tasks=pdf_tasks, pdf_results=pdf_results)
        summary["skipped_missing_mapper_cache"] = skipped_missing_mapper_cache
        write_json(self.summary_path, summary)
        write_json(self.output_dir / "mapping_summary.json", summary)
        validation = self._validate_rebuild_run(summary, provision_results, coverage, pdf_coverage)
        write_json(self.output_dir / "run_validation_report.json", validation)
        if not validation["passed"]:
            write_json(self.output_dir / "run_validation_failure.json", validation)
            failed_dir = self._move_staging_to_failed(staging_dir, f"{run_id}_validation_failed")
            raise RuntimeError(f"Run validation failed; staging moved to {failed_dir}")
        self._publish_current(staging_dir)
        return summary

    @staticmethod
    def _assert_terminal_state_reconciliation(
        provision_tasks: list[CandidateTask],
        provision_results: list[ValidatedTaskResult],
        direct_tasks: list[PDFDocumentTask],
        direct_results: list[dict],
    ) -> None:
        """Every scheduled task must have exactly one explicit terminal state."""
        terminal = {"accepted", "rejected", "supporting_only", "human_legal_review", "technical_repair", "prefilter_rejected"}
        expected = {task.task_id for task in provision_tasks} | {task.task_id for task in direct_tasks}
        observed: list[str] = [result.task_id for result in provision_results] + [str(result.get("task_id") or "") for result in direct_results]
        unknown = [task_id for task_id in observed if task_id not in expected]
        duplicates = {task_id for task_id in observed if task_id and observed.count(task_id) > 1}
        missing = expected - set(observed)
        invalid = [result.task_id for result in provision_results if result.status not in terminal]
        invalid += [str(result.get("task_id") or "") for result in direct_results if result.get("status") not in terminal]
        if missing or duplicates or unknown or invalid:
            raise RuntimeError(
                "Terminal-state reconciliation failed: "
                f"missing={len(missing)} multiple={len(duplicates)} unknown={len(unknown)} invalid={len(invalid)}"
            )

    def _typed_reviewer_contract_preflight(
        self,
        provision_tasks: list[CandidateTask],
        cache: dict[str, dict],
        model: str,
        review_model: str,
    ) -> None:
        if not provision_tasks:
            return
        previous_live = self.live
        self.live = False
        try:
            schema_failures: list[str] = []
            for task in provision_tasks:
                result = self._process_provision_task(task, model, review_model, cache)
                text = " ".join(
                    str(value or "")
                    for value in (
                        result.result_code,
                        result.technical_detail,
                        " ".join(result.failure_codes or []),
                    )
                )
                if "REVIEWER_SCHEMA_ERROR" in text or "reviewer_schema_error" in text:
                    schema_failures.append(task.task_id)
            if schema_failures:
                sample = ", ".join(schema_failures[:5])
                raise RuntimeError(f"Typed reviewer contract preflight failed before API calls; sample task_ids: {sample}")
        finally:
            self.live = previous_live

    def _migrate_reviewer_cache_to_stable_keys(
        self,
        provision_tasks: list[CandidateTask],
        cache: dict[str, dict],
        model: str,
        review_model: str,
    ) -> dict:
        rows_by_task: dict[str, list[dict]] = {}
        for row in read_jsonl(self.cache_path):
            if row.get("task_type") != "reviewer" or not row.get("task_id") or not isinstance(row.get("decision"), dict):
                continue
            rows_by_task.setdefault(str(row["task_id"]), []).append(row)
        migrated = 0
        ordinary = 0
        framework = 0
        stale_reasons: Counter[str] = Counter()
        already_current = 0
        missing_rows = 0
        for task in provision_tasks:
            material = self._review_material_from_mapper_cache(task, model, cache)
            if material is None:
                continue
            review_task, review_match, stable_key = material
            try:
                cached = cache.get(stable_key)
                if cached is not None:
                    parsed = _parse_cached_reviewer_decision(cached)
                    parsed = _complete_cached_reviewer(review_task, parsed, review_match)
                    _assert_cached_reviewer_compatible(review_task, parsed, review_match)
                    already_current += 1
                    continue
            except Exception:
                pass
            compatible_row = None
            for row in sorted(rows_by_task.get(task.task_id, []), key=_reviewer_cache_row_score, reverse=True):
                try:
                    parsed = _parse_cached_reviewer_decision(row["decision"])
                    parsed = _complete_cached_reviewer(review_task, parsed, review_match)
                    _assert_cached_reviewer_compatible(review_task, parsed, review_match)
                except Exception as exc:
                    stale_reasons[str(exc)] += 1
                    continue
                compatible_row = row
                break
            if compatible_row is None:
                if rows_by_task.get(task.task_id):
                    continue
                missing_rows += 1
                continue
            decision = compatible_row["decision"]
            prompt_version = reviewer_prompt_version_for_task(review_task, evidence_catalog=True)
            model_name = str(compatible_row.get("model_name") or review_model)
            self._append_cache(stable_key, prompt_version, model_name, task.task_id, "reviewer", decision, cache)
            migrated += 1
            if task.route_topic in {"P7_DATA_PROTECTION_FRAMEWORK", "P7_CYBERSECURITY_FRAMEWORK"}:
                framework += 1
            else:
                ordinary += 1
        return {
            "migrated_total": migrated,
            "migrated_ordinary": ordinary,
            "migrated_framework": framework,
            "already_current": already_current,
            "missing_rows": missing_rows,
            "stale_reasons": dict(stale_reasons),
            "stable_key_contract": REVIEWER_CACHE_CONTRACT_VERSION,
        }

    def _review_material_from_mapper_cache(
        self,
        task: CandidateTask,
        model: str,
        cache: dict[str, dict],
    ) -> tuple[CandidateTask, IndicatorMatch, str] | None:
        if task.task_kind == "treaty_provision":
            return None
        prompt_version = prompt_version_for_task(task)
        cached_key = next((candidate for candidate in mapper_cache_lookup_keys(task, model, prompt_version) if candidate in cache), None)
        if not cached_key:
            return None
        try:
            decision = _parse_cached_mapping_decision(cache[cached_key])
            result = validate_decision(task, decision, model_name=model, prompt_version=prompt_version, cache_key=cached_key, llm_call=False, cache_hit=True, retries=0)
        except Exception:
            return None
        if not result.decision or not (result.accepted_matches or result.review_matches):
            return None
        review_context_task, _ = self._resolve_review_context(task, result)
        for record in [*result.accepted_matches, *result.review_matches]:
            match = _find_match(result.decision, record)
            if not match:
                continue
            review_task, evidence_id_map = self._build_evidence_catalog_task(review_context_task)
            review_match = _remap_match_evidence_ids(match, evidence_id_map)
            key = reviewer_cache_key(
                review_task,
                review_match.indicator or "",
                review_model_name(model),
                evidence_catalog=True,
                required_elements=_review_required_elements(review_task, review_match),
            )
            return review_task, review_match, key
        return None

    def _reviewer_cache_plan(
        self,
        provision_tasks: list[CandidateTask],
        cache: dict[str, dict],
        model: str,
        review_model: str,
    ) -> dict:
        hits = 0
        misses = 0
        misses_by_route: Counter[str] = Counter()
        misses_by_indicator: Counter[str] = Counter()
        miss_reasons: Counter[str] = Counter()
        total = 0
        framework_hits = 0
        ordinary_hits = 0
        for task in provision_tasks:
            material = self._review_material_from_mapper_cache(task, model, cache)
            if material is None:
                continue
            total += 1
            review_task, review_match, key = material
            try:
                parsed = _parse_cached_reviewer_decision(cache.get(key))
                parsed = _complete_cached_reviewer(review_task, parsed, review_match)
                _assert_cached_reviewer_compatible(review_task, parsed, review_match)
                hits += 1
                if task.route_topic in {"P7_DATA_PROTECTION_FRAMEWORK", "P7_CYBERSECURITY_FRAMEWORK"}:
                    framework_hits += 1
                else:
                    ordinary_hits += 1
                continue
            except Exception as exc:
                misses += 1
                misses_by_route[task.route_topic] += 1
                indicator = str(review_match.indicator or task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else ""))
                misses_by_indicator[indicator] += 1
                miss_reasons[str(exc)] += 1
        return {
            "total_reviewer_tasks": total,
            "cache_hits": hits,
            "cache_misses": misses,
            "framework_cache_hits": framework_hits,
            "ordinary_cache_hits": ordinary_hits,
            "misses_by_route": dict(misses_by_route),
            "misses_by_indicator": dict(misses_by_indicator),
            "miss_reasons": dict(miss_reasons),
            "expected_reviewer_api_calls": misses,
        }

    def _process_provision_task(self, task: CandidateTask, model: str, review_model: str, cache: dict[str, dict], refresh_reviewer: bool = False) -> ValidatedTaskResult:
        persistent_result = self._persistent_human_decision_result_for_task(task, model)
        if persistent_result is not None:
            return persistent_result
        human_result = self._human_review_result_for_task(task, model)
        if human_result is not None:
            return human_result
        if task.task_kind == "treaty_provision":
            return self._process_deterministic_evidence_task(task, model, review_model)
        prompt_version = prompt_version_for_task(task)
        key = cache_key(task, model, prompt_version)
        cached_key = next((candidate for candidate in mapper_cache_lookup_keys(task, model, prompt_version) if candidate in cache), None)
        if cached_key:
            try:
                decision = _parse_cached_mapping_decision(cache[cached_key])
            except Exception:
                pass
            else:
                try:
                    result = validate_decision(task, decision, model_name=model, prompt_version=prompt_version, cache_key=cached_key, llm_call=False, cache_hit=True, retries=0)
                    return self._review_if_needed(task, result, review_model, cache, refresh_reviewer=refresh_reviewer)
                except Exception as exc:
                    return self._error_result(task, model, f"Cached decision validation failed: {exc}", "provision").model_copy(update={"cache_hit": True})
        if task.task_kind == "framework_element" and not self.live:
            return self._cache_miss_review_result(task, model, "framework_mapper_cache_missing")
        if not self.live:
            return self._error_result(task, model, f"Mapper cache missing for task {task.task_id}; offline re-resolve cannot call LLM", "provision")
        try:
            assert_model_allowed("mapper", model, input_type="provision")
            decision, retries = extract_decision(task, model)
            self._append_cache(key, prompt_version, model, task.task_id, "provision", decision.model_dump(), cache)
            result = validate_decision(task, decision, model_name=model, prompt_version=prompt_version, cache_key=key, llm_call=True, cache_hit=False, retries=retries)
            return self._review_if_needed(task, result, review_model, cache, refresh_reviewer=refresh_reviewer)
        except Exception as exc:
            return self._error_result(task, model, f"Mapper LLM failed: {exc}", "provision")

    def _load_persistent_human_decisions(self) -> dict[str, dict]:
        submission_path = self.submission_dir / "human_decisions.jsonl"
        legacy_path = self.project_root / "data" / "human_decisions" / f"{self.economy_slug}.jsonl"
        candidate_paths = (
            (submission_path, legacy_path)
            if self.pillars == {6, 7}
            else (submission_path,)
        )
        paths = [path for path in candidate_paths if path.exists()]
        if not paths:
            return {}
        decisions: dict[str, dict] = {}
        for path in paths:
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                row = json.loads(line)
                key = str(row.get("review_key") or "").strip()
                evidence_hash = str(row.get("evidence_hash") or row.get("source_fingerprint") or "").strip()
                decision = str(row.get("decision") or "").strip().casefold()
                if not key:
                    raise RuntimeError(f"Persistent human decision missing review_key: {path}:{line_no}")
                if not evidence_hash:
                    raise RuntimeError(f"Persistent human decision missing evidence_hash: {path}:{line_no}")
                if decision not in {"accept", "reject"}:
                    raise RuntimeError(f"Persistent human decision has unsupported decision={decision!r}: {path}:{line_no}")
                overrides = row.get("overrides") or {}
                if not isinstance(overrides, dict):
                    raise RuntimeError(f"Persistent human decision overrides must be an object: {path}:{line_no}")
                normalized = {**row, "review_key": key, "evidence_hash": evidence_hash, "decision": decision, "overrides": overrides}
                existing = decisions.get(key)
                if existing:
                    if str(existing.get("decision")) != decision:
                        raise RuntimeError(f"Conflicting persistent human decisions for review_key={key}: {path}:{line_no}")
                    if str(existing.get("evidence_hash") or "") != evidence_hash:
                        raise RuntimeError(f"Conflicting evidence_hash for persistent human decision review_key={key}: {path}:{line_no}")
                    if path == submission_path:
                        decisions[key] = normalized
                    continue
                decisions[key] = normalized
        if decisions:
            self.submission_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = submission_path.with_suffix(".jsonl.tmp")
            tmp_path.write_text(
                "\n".join(json.dumps(decisions[key], ensure_ascii=False, sort_keys=True) for key in sorted(decisions)) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(submission_path)
        return decisions

    def _persistent_human_decision_result_for_task(self, task: CandidateTask, model: str) -> ValidatedTaskResult | None:
        if not self.persistent_human_decisions:
            return None
        indicators = [task.indicator_id] if task.indicator_id else list(task.candidate_indicators)
        indicators = [str(indicator) for indicator in indicators if indicator]
        if not indicators:
            return None
        for indicator in indicators:
            key = review_key_for_task(task, indicator)
            current_hash = source_fingerprint_for_task(task, indicator)
            decision = self.persistent_human_decisions.get(key)
            if decision is None:
                stale = next((row for row in self.persistent_human_decisions.values() if row.get("evidence_hash") != current_hash and str(row.get("review_key") or "") == key), None)
                if stale:
                    return self._persistent_stale_result(task, model, stale, current_hash)
                continue
            if str(decision.get("evidence_hash") or "") != current_hash:
                return self._persistent_stale_result(task, model, decision, current_hash)
            indicator = str(decision.get("indicator") or decision.get("corrected_indicator") or indicator)
            if decision["decision"] == "reject":
                return self._persistent_reject_result(task, model, decision, indicator, current_hash)
            return self._persistent_accept_result(task, model, decision, indicator, current_hash)
        return None

    def _persistent_reject_result(self, task: CandidateTask, model: str, decision: dict, indicator: str, evidence_hash: str) -> ValidatedTaskResult:
        return ValidatedTaskResult(
            task_id=task.task_id,
            economy=task.economy,
            document_id=task.document_id,
            law_title=task.law_title,
            instrument_type=task.instrument_type,
            source_url=task.source_url,
            focal_provision_id=task.focal_provision_id,
            route_topic=task.route_topic,
            candidate_indicators=task.candidate_indicators,
            status="rejected",
            queue_type="none",
            result_code="NO_MATCH",
            indicator=indicator,
            decision=None,
            failure_codes=["NO_MATCH"],
            review_reasons=[str(decision.get("reason") or "persistent_human_reject")],
            rationale=str(decision.get("reason") or "Persistent human decision rejected this task."),
            accepted_matches=[],
            review_matches=[],
            prompt_version="persistent-human-decision-v1",
            validation_version=P4_VALIDATION_VERSION if task.route_topic.startswith("P4_") else VALIDATION_VERSION,
            model_name="human_verified",
            cache_key=f"persistent_human:{decision['review_key']}",
            llm_call=False,
            cache_hit=True,
            retries=0,
            reviewer_model_name="human_verified",
            reviewer_cache_key=f"persistent_human:{decision['review_key']}",
            reviewer_llm_call=False,
            reviewer_cache_hit=True,
            review_resolution_attempted=False,
            review_resolution_completed=True,
            review_resolution_notes=["persistent_human_decision_reject"],
            error=None,
            decision_source="human_review",
            human_review_id=decision["review_key"],
            human_validated_attributes=dict(decision.get("overrides") or {}),
        )

    def _persistent_accept_result(self, task: CandidateTask, model: str, decision: dict, indicator: str, evidence_hash: str) -> ValidatedTaskResult:
        overrides = dict(decision.get("overrides") or {})
        quote = str(decision.get("corrected_focal_quote") or task.focal_quote or task.focal_text)
        focal_provision_id = str(decision.get("corrected_focal_provision") or task.focal_provision_id)
        match = {
            "indicator": indicator,
            "status": "accepted",
            "quote": quote,
            "failure_codes": [],
            "actor": "",
            "action": "",
            "regulated_object": "",
            "geographic_nexus": "",
            "duration": None,
            "conditions": [],
            "rationale": str(decision.get("reason") or "Persistent human decision accepted this task."),
            "evidence_ids": ["S1"],
            "required_element_codes": [],
            "result_code": None,
            "focal_role": "operative",
            "reviewer_attributes": overrides,
        }
        return ValidatedTaskResult(
            task_id=task.task_id,
            economy=task.economy,
            document_id=task.document_id,
            law_title=task.law_title,
            instrument_type=task.instrument_type,
            source_url=task.source_url,
            focal_provision_id=focal_provision_id,
            route_topic=task.route_topic,
            candidate_indicators=task.candidate_indicators,
            status="accepted",
            queue_type="none",
            result_code=None,
            indicator=indicator,
            decision=None,
            failure_codes=[],
            review_reasons=[],
            rationale=str(decision.get("reason") or "Persistent human decision accepted this task."),
            accepted_matches=[match],
            review_matches=[],
            prompt_version="persistent-human-decision-v1",
            validation_version=P4_VALIDATION_VERSION if task.route_topic.startswith("P4_") else VALIDATION_VERSION,
            model_name="human_verified",
            cache_key=f"persistent_human:{decision['review_key']}",
            llm_call=False,
            cache_hit=True,
            retries=0,
            reviewer_model_name="human_verified",
            reviewer_cache_key=f"persistent_human:{decision['review_key']}",
            reviewer_llm_call=False,
            reviewer_cache_hit=True,
            review_resolution_attempted=False,
            review_resolution_completed=True,
            review_resolution_notes=["persistent_human_decision_accept"],
            error=None,
            warnings=[],
            decision_source="human_review",
            human_review_id=decision["review_key"],
            reviewed_by="persistent_human_decision",
            human_validated_attributes=overrides,
        )

    def _persistent_stale_result(self, task: CandidateTask, model: str, decision: dict, current_hash: str) -> ValidatedTaskResult:
        result = self._error_result(task, model, "STALE_HUMAN_DECISION", "provision")
        return result.model_copy(update={
            "status": "human_legal_review",
            "queue_type": "human_legal_review",
            "result_code": "LEGAL_UNCERTAINTY",
            "failure_codes": ["LEGAL_UNCERTAINTY"],
            "review_reasons": ["STALE_HUMAN_DECISION"],
            "uncertain_elements": ["STALE_HUMAN_DECISION"],
            "technical_detail": None,
            "expected_repair_action": "refresh_persistent_human_decision_evidence_hash",
            "indicator": task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else None),
            "human_review_id": decision.get("review_key"),
            "review_resolution_attempted": False,
            "review_resolution_completed": True,
            "review_resolution_notes": [f"persistent evidence_hash mismatch: stored={decision.get('evidence_hash')} current={current_hash}"],
        })

    def _cache_miss_review_result(self, task: CandidateTask, model: str, reason: str) -> ValidatedTaskResult:
        result = self._error_result(task, model, reason, "provision")
        return result.model_copy(update={
            "status": "human_legal_review",
            "queue_type": "human_legal_review",
            "result_code": "LEGAL_UNCERTAINTY",
            "failure_codes": ["LEGAL_UNCERTAINTY"],
            "review_reasons": [reason],
            "uncertain_elements": [reason.upper()],
            "technical_detail": None,
            "expected_repair_action": "run_standard_mapping_with_api_key_to_create_current_framework_mapper_and_reviewer_cache",
            "indicator": task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else None),
        })

    def _human_review_result_for_task(self, task: CandidateTask, model: str) -> ValidatedTaskResult | None:
        if not self.human_decisions:
            return None
        indicator = task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else None)
        if not indicator:
            return None
        key = review_key_for_task(task, indicator)
        decision = self.human_decisions.get(key)
        if not decision:
            stale = self._stale_human_decision_for_task(task, indicator)
            if stale:
                return self._stale_human_decision_result(task, model, stale)
            return None
        if str(decision.get("source_fingerprint") or "") != source_fingerprint_for_task(task, indicator):
            return self._stale_human_decision_result(task, model, decision, "stale_human_decision_source_changed")
        if str(decision.get("contract_version") or "") != str(task.contract_version or INDICATOR_SPEC_VERSION):
            return self._stale_human_decision_result(task, model, decision, "stale_human_decision_contract_changed")
        human_decision = str(decision.get("human_decision") or "").strip()
        corrected_indicator = str(decision.get("corrected_indicator_id") or "").strip() or indicator
        corrected_article = str(decision.get("corrected_focal_provision_id") or "").strip() or task.focal_provision_id
        corrected_quote = str(decision.get("corrected_focal_quote") or "").strip() or task.focal_quote or task.focal_text
        attrs = _parse_human_validated_attributes(decision)
        if human_decision == "technical_repair":
            return self._error_result(task, model, "human_review_marked_technical_repair", "provision").model_copy(update={
                "status": "technical_repair",
                "queue_type": "technical_repair",
                "result_code": "TECHNICAL_INPUT_ERROR",
                "technical_detail": str(decision.get("human_rationale") or "human_review_marked_technical_repair"),
                "decision_source": "human_review",
                "human_review_id": decision.get("review_id"),
                "reviewed_by": decision.get("reviewer_name"),
                "reviewed_at": decision.get("reviewed_at"),
                "human_validated_attributes": attrs,
            })
        mapping_decision = _human_mapping_decision(task, corrected_indicator, corrected_quote, human_decision, decision)
        result = validate_decision(
            task,
            mapping_decision,
            model_name="human_review",
            prompt_version=prompt_version_for_task(task),
            cache_key=f"human_review:{key}",
            llm_call=False,
            cache_hit=True,
            retries=0,
        ).model_copy(update={"focal_provision_id": corrected_article})
        reviewer = _human_reviewer_decision(task, corrected_indicator, human_decision, attrs, decision)
        resolved = apply_reviewer_decisions(
            result,
            [reviewer],
            reviewer_model_name="human_review",
            reviewer_cache_key=f"human_review:{decision.get('review_id')}",
            reviewer_llm_call=False,
            reviewer_cache_hit=True,
            task=task,
        )
        return resolved.model_copy(update={
            "decision_source": "human_review",
            "human_review_id": decision.get("review_id"),
            "reviewed_by": decision.get("reviewer_name"),
            "reviewed_at": decision.get("reviewed_at"),
            "human_validated_attributes": attrs,
        })

    def _stale_human_decision_for_task(self, task: CandidateTask, indicator: str) -> dict | None:
        for decision in self.human_decisions.values():
            if str(decision.get("economy") or "").casefold() != task.economy.casefold():
                continue
            if str(decision.get("indicator_id") or "") != indicator:
                continue
            if str(decision.get("document_id") or "") != task.document_id:
                continue
            if str(decision.get("focal_provision_id") or "") != task.focal_provision_id:
                continue
            return decision
        return None

    def _stale_human_decision_result(self, task: CandidateTask, model: str, decision: dict, reason: str = "stale_human_decision") -> ValidatedTaskResult:
        result = self._error_result(task, model, reason, "provision")
        return result.model_copy(update={
            "status": "human_legal_review",
            "queue_type": "human_legal_review",
            "result_code": "LEGAL_UNCERTAINTY",
            "failure_codes": ["LEGAL_UNCERTAINTY"],
            "review_reasons": [reason],
            "uncertain_elements": ["STALE_HUMAN_DECISION"],
            "technical_detail": None,
            "expected_repair_action": "review_stale_human_decision_against_current_source_or_contract",
            "indicator": task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else None),
            "human_review_id": decision.get("review_id"),
            "reviewed_by": decision.get("reviewer_name"),
            "reviewed_at": decision.get("reviewed_at"),
        })

    def _process_deterministic_evidence_task(self, task: CandidateTask, model: str, review_model: str) -> ValidatedTaskResult:
        if task.task_kind == "framework_element":
            return self._cache_miss_review_result(task, model, "framework_deterministic_review_disabled")
        indicator = task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else None)
        if not indicator:
            return self._error_result(task, model, "deterministic evidence task missing indicator", "provision")
        decision = _deterministic_mapping_decision(task, indicator)
        result = validate_decision(
            task,
            decision,
            model_name="deterministic",
            prompt_version=prompt_version_for_task(task),
            cache_key=_deterministic_task_cache_key(task),
            llm_call=False,
            cache_hit=True,
            retries=0,
        )
        match = decision.matches[0] if decision.matches else None
        if match is None:
            return result
        reviewer = _deterministic_typed_review(task, match)
        return apply_reviewer_decisions(
            result,
            [reviewer],
            reviewer_model_name="deterministic",
            reviewer_cache_key=_deterministic_reviewer_cache_key(task),
            reviewer_llm_call=False,
            reviewer_cache_hit=True,
            task=task,
        )

    def _review_if_needed(self, task: CandidateTask, result: ValidatedTaskResult, review_model: str, cache: dict[str, dict], *, refresh_reviewer: bool = False) -> ValidatedTaskResult:
        if result.status in {"review", "human_legal_review"} and not (result.accepted_matches or result.review_matches):
            return result.model_copy(update={
                "status": "technical_repair",
                "queue_type": "technical_repair",
                "result_code": "TECHNICAL_INPUT_ERROR",
                "failure_codes": ["TECHNICAL_INPUT_ERROR"],
                "review_reasons": [],
                "technical_detail": "accepted_or_review_without_match_records",
                "affected_evidence_ids": [],
                "expected_repair_action": "inspect_mapper_output_schema",
                "review_resolution_attempted": False,
                "review_resolution_completed": False,
            })
        if result.status not in {"accepted", "review", "human_legal_review"} or not result.decision or not (result.accepted_matches or result.review_matches):
            return result
        task, resolution_notes = self._resolve_review_context(task, result)
        reviewer_decisions: list[ReviewerDecision] = []
        reviewer_llm_call = False
        reviewer_cache_hit = False
        last_key = None
        reviewer_failed = False
        reviewer_error_code = "reviewer_cache_missing"
        reviewer_error_detail = "compatible reviewer cache missing"
        resolver_task = task
        stale_framework_cache = False
        for record in [*result.accepted_matches, *result.review_matches]:
            match = _find_match(result.decision, record)
            if not match:
                continue
            review_task = task
            review_match = match
            use_evidence_catalog = bool(refresh_reviewer)
            if use_evidence_catalog:
                review_task, evidence_id_map = self._build_evidence_catalog_task(task)
                review_match = _remap_match_evidence_ids(match, evidence_id_map)
                resolver_task = review_task
            key = reviewer_cache_key(
                review_task,
                review_match.indicator or "",
                review_model,
                evidence_catalog=use_evidence_catalog,
                required_elements=_review_required_elements(review_task, review_match),
            )
            last_key = key
            if key in cache:
                try:
                    cached_reviewer = _parse_cached_reviewer_decision(cache[key])
                    cached_reviewer = _complete_cached_reviewer(review_task, cached_reviewer, review_match)
                    _assert_cached_reviewer_compatible(review_task, cached_reviewer, review_match)
                    reviewer_decisions.append(cached_reviewer)
                    reviewer_cache_hit = True
                    continue
                except Exception:
                    stale_framework_cache = stale_framework_cache or review_task.task_kind == "framework_element"
                    pass
            if not use_evidence_catalog:
                review_task, evidence_id_map = self._build_evidence_catalog_task(task)
                review_match = _remap_match_evidence_ids(match, evidence_id_map)
                resolver_task = review_task
                key = reviewer_cache_key(
                    review_task,
                    review_match.indicator or "",
                    review_model,
                    evidence_catalog=True,
                    required_elements=_review_required_elements(review_task, review_match),
                )
                last_key = key
                if key in cache:
                    try:
                        cached_reviewer = _parse_cached_reviewer_decision(cache[key])
                        cached_reviewer = _complete_cached_reviewer(review_task, cached_reviewer, review_match)
                        _assert_cached_reviewer_compatible(review_task, cached_reviewer, review_match)
                        reviewer_decisions.append(cached_reviewer)
                        reviewer_cache_hit = True
                        continue
                    except Exception:
                        stale_framework_cache = stale_framework_cache or review_task.task_kind == "framework_element"
                        pass
                use_evidence_catalog = True
            if not self.live:
                reviewer_failed = False
                continue
            if not api_key_available():
                reviewer_failed = True
                continue
            try:
                reviewer, _retries, used_review_model = self._extract_reviewer_with_schema_repair(review_task, review_match, review_model)
                self._append_cache(key, reviewer_prompt_version_for_task(review_task, evidence_catalog=use_evidence_catalog), used_review_model, task.task_id, "reviewer", reviewer.model_dump(), cache)
                reviewer_decisions.append(reviewer)
                reviewer_llm_call = True
                review_model = used_review_model
            except Exception as exc:
                reviewer_failed = True
                reviewer_error_code, reviewer_error_detail = _classify_reviewer_exception(exc)
                resolution_notes.append(f"{reviewer_error_code}: {reviewer_error_detail}")
        if not reviewer_decisions:
            if task.task_kind == "framework_element" and not self.live:
                reason = "framework_typed_review_cache_stale" if stale_framework_cache else "framework_typed_review_cache_missing"
                return result.model_copy(update={
                    "status": "human_legal_review",
                    "queue_type": "human_legal_review",
                    "result_code": "LEGAL_UNCERTAINTY",
                    "failure_codes": ["LEGAL_UNCERTAINTY"],
                    "review_reasons": [reason],
                    "uncertain_elements": [reason.upper()],
                    "technical_detail": None,
                    "expected_repair_action": "run_standard_mapping_with_api_key_to_create_current_framework_typed_review_cache",
                    "reviewer_model_name": review_model,
                    "reviewer_cache_key": last_key,
                    "reviewer_llm_call": False,
                    "reviewer_cache_hit": reviewer_cache_hit,
                    "review_resolution_attempted": True,
                    "review_resolution_completed": True,
                    "review_resolution_notes": resolution_notes,
                })
            technical_detail = (
                "api_key_required_for_reviewer_cache_miss"
                if self.live and not api_key_available()
                else reviewer_error_code
            )
            update = {
                "status": "technical_repair",
                "queue_type": "technical_repair",
                "result_code": "MODEL_ERROR",
                "failure_codes": ["MODEL_ERROR"],
                "review_reasons": [],
                "technical_detail": technical_detail,
                "affected_evidence_ids": [],
                "expected_repair_action": "set_OPENAI_API_KEY_then_rerun" if technical_detail.startswith("api_key_required") else _reviewer_repair_action(technical_detail),
                "reviewer_model_name": review_model,
                "reviewer_cache_key": last_key,
                "reviewer_llm_call": reviewer_failed,
                "reviewer_cache_hit": reviewer_cache_hit,
                "review_resolution_attempted": True,
                "review_resolution_completed": True,
                "review_resolution_notes": resolution_notes,
            }
            return result.model_copy(update=update)
        reviewed = apply_reviewer_decisions(
            result,
            reviewer_decisions,
            reviewer_model_name=review_model,
            reviewer_cache_key=last_key,
            reviewer_llm_call=reviewer_llm_call,
            reviewer_cache_hit=reviewer_cache_hit,
            task=resolver_task,
        )
        return reviewed.model_copy(update={
            "review_resolution_attempted": True,
            "review_resolution_completed": True,
            "review_resolution_notes": resolution_notes,
        })

    def _extract_reviewer_with_schema_repair(
        self,
        task: CandidateTask,
        match: IndicatorMatch,
        review_model: str,
    ) -> tuple[ReviewerDecision, int, str]:
        retry_instructions = ""
        allowed_elements = _review_required_elements(task, match)
        allowed_exclusions = _review_allowed_exclusions(task)
        allowed_evidence_ids = set(task.evidence_segments)
        require_basis = task.route_topic == "P7_RETENTION"
        total_retries = 0
        last_issues: dict | None = None
        for attempt in range(2):
            assert_model_allowed("reviewer", review_model, input_type="provision")
            reviewer, retries = extract_reviewer_decision(task, match, review_model, max_retries=1, retry_instructions=retry_instructions)
            total_retries += retries
            reviewer, issues = normalize_reviewer_decision(
                reviewer,
                required_elements=allowed_elements,
                allowed_exclusions=allowed_exclusions,
                allowed_evidence_ids=allowed_evidence_ids,
                require_record_scope_basis=require_basis,
            )
            last_issues = issues
            if not reviewer_schema_retry_needed(issues):
                return reviewer, total_retries, review_model
            retry_instructions = reviewer_retry_instructions(issues)
        raise RuntimeError(f"reviewer_schema_error_after_retry: {last_issues}")

    def _resolve_review_context(self, task: CandidateTask, result: ValidatedTaskResult) -> tuple[CandidateTask, list[str]]:
        notes: list[str] = []
        segments = dict(task.evidence_segments)
        if "R_PARENT" not in segments and task.parent_section_text and task.parent_section_text.strip() != task.focal_text.strip():
            segments["R_PARENT"] = f"Complete parent section: {task.parent_section_text[:6000]}"
            notes.append("added_complete_parent_section")
        heading = " | ".join(value for value in (task.law_title, task.section_heading) if value)
        if heading and "R_SCOPE" not in segments:
            segments["R_SCOPE"] = f"Title and headings: {heading}"
            notes.append("added_title_and_heading_scope")
        supporting_context = "\n".join(f"[{sid}] {text}" for sid, text in segments.items() if sid != "S1")
        return task.model_copy(update={"evidence_segments": segments, "supporting_context": supporting_context}), notes

    def _build_evidence_catalog_task(self, task: CandidateTask) -> tuple[CandidateTask, dict[str, str]]:
        catalog = evidence_catalog_for_task(task)
        old_ids = list(task.evidence_segments)
        evidence_id_map = {old_id: entry.evidence_id for old_id, entry in zip(old_ids, catalog)}
        segments = {entry.evidence_id: entry.text for entry in catalog}
        supporting_context = "\n".join(f"[{entry.evidence_id}] {entry.text}" for entry in catalog if entry.role != "focal")
        return task.model_copy(update={"evidence_segments": segments, "supporting_context": supporting_context}), evidence_id_map

    def _process_pdf_document_task(self, task: PDFDocumentTask, review_model: str, cache: dict[str, dict]) -> dict:
        pdf_model = pdf_mapper_model_name()
        base = {
            "task_id": task.task_id,
            "task_type": "pdf_document",
            "economy": task.economy,
            "document_id": task.document_id,
            "law_title": task.title,
            "collection": task.collection,
            "official_number": task.official_number,
            "year": task.year,
            "language": task.language,
            "source_url": task.source_url,
            "raw_path": task.raw_path,
            "source_sha256": task.source_sha256,
            "prefilter_status": task.prefilter_status,
            "model_name": pdf_model,
            "pdf_mapper_llm_call": False,
            "pdf_mapper_cache_hit": False,
            "reviewer_llm_call": False,
            "reviewer_cache_hit": False,
            "citation_verified": 0,
            "citation_failed": 0,
            "citation_unverifiable": 0,
            "fallback_model_calls": 0,
            "claims": [],
        }
        if task.prefilter_status in {"reject"}:
            return dict(base, status="prefilter_rejected", pdf_documents_no_match=1)
        artifact_path = Path(task.pdf_text_path)
        if not artifact_path.exists() or not artifact_path.stat().st_size:
            return dict(base, status="technical_repair", technical_detail="docling_artifact_missing_or_empty")
        try:
            artifact = load_docling_artifact(artifact_path)
        except Exception as exc:
            return dict(base, status="technical_repair", technical_detail=f"docling_artifact_invalid:{type(exc).__name__}")
        text_hash = str(artifact.get("document_text_hash") or task.document_text_hash or task.source_sha256 or "")
        if not text_hash:
            return dict(base, status="technical_repair", technical_detail="docling_document_text_hash_missing")
        task = task.model_copy(
            update={
                "source_sha256": text_hash,
                "document_text_hash": text_hash,
                "page_count": int(artifact.get("page_count") or 0) or task.page_count,
            }
        )
        base["source_sha256"] = text_hash
        key = pdf_mapper_cache_key(task, pdf_model)
        decision = None
        retries = 0
        if key in cache:
            try:
                decision = PDFMappingDecision.model_validate(cache[key])
                base["pdf_mapper_cache_hit"] = True
            except Exception:
                decision = None
        if decision is None:
            if not self.live:
                return dict(base, status="pending_live_processing", pending_reason="pdf_mapper_cache_missing")
            try:
                assert_model_allowed("pdf_mapper", pdf_model, input_type="document_direct")
                decision, retries = extract_pdf_mapping_decision(task, pdf_model)
                self._append_cache(
                    key,
                    pdf_prompt_version_for_task(task),
                    pdf_model,
                    task.task_id,
                    "pdf_mapper",
                    decision.model_dump(),
                    cache,
                )
                base["pdf_mapper_llm_call"] = True
            except Exception as exc:
                return dict(base, status="technical_repair", technical_detail=f"pdf_mapper_failed:{type(exc).__name__}", error=str(exc)[:500])
        if decision.document_decision == "no_match":
            return dict(base, status="rejected", document_decision="no_match", pdf_documents_no_match=1)
        if decision.document_decision == "technical_failure":
            return dict(base, status="technical_repair", technical_detail=decision.document_notes or "pdf_mapper_technical_failure")
        if decision.document_decision == "uncertain" and not decision.claims:
            return dict(base, status="human_legal_review", review_reason=decision.document_notes or "pdf_mapper_document_uncertain")
        claim_rows = []
        for index, claim in enumerate(decision.claims, start=1):
            if claim.indicator_id in {"P6-I5", "P4-I4", "P4-I7", "P4-I8"}:
                continue
            claim_row = self._process_pdf_claim(task, claim, index, review_model, cache)
            claim_rows.append(claim_row)
            base["reviewer_llm_call"] = bool(base["reviewer_llm_call"] or claim_row.get("reviewer_llm_call"))
            base["reviewer_cache_hit"] = bool(base["reviewer_cache_hit"] or claim_row.get("reviewer_cache_hit"))
            status = claim_row.get("citation_status")
            if status == "verified":
                base["citation_verified"] = int(base["citation_verified"]) + 1
            elif status == "failed":
                base["citation_failed"] = int(base["citation_failed"]) + 1
            elif status == "unverifiable":
                base["citation_unverifiable"] = int(base["citation_unverifiable"]) + 1
        doc_status = "accepted" if any(row.get("status") == "accepted" for row in claim_rows) else ("human_legal_review" if any(row.get("status") == "human_legal_review" for row in claim_rows) else ("technical_repair" if any(row.get("status") == "technical_repair" for row in claim_rows) else "rejected"))
        return dict(base, status=doc_status, document_decision=decision.document_decision, retries=retries, claims=claim_rows, pdf_documents_with_claims=1 if claim_rows else 0)

    def _process_pdf_claim(self, task: PDFDocumentTask, claim: PDFEvidenceClaim, index: int, review_model: str, cache: dict[str, dict]) -> dict:
        citation = self._validate_pdf_claim_citation(task, claim, index, cache)
        claim_id = f"{task.task_id}:claim-{index}:{claim.indicator_id}:{_slug(claim.article)}"
        base = {
            "claim_id": claim_id,
            "indicator_id": claim.indicator_id,
            "article": claim.article,
            "page_number": claim.page_number,
            "verbatim_snippet": claim.verbatim_snippet,
            "mapping_rationale": claim.mapping_rationale,
            "coverage": claim.coverage,
            "sector": claim.sector or "",
            "focal_role": claim.focal_role,
            "confidence": claim.confidence,
            "citation_status": citation.status,
            "citation_error": citation.error,
            "reviewer_llm_call": False,
            "reviewer_cache_hit": False,
            "reviewer_cache_key": "",
        }
        synthetic_task, decision, match = _synthetic_task_for_pdf_claim(task, claim, claim_id)
        persistent = self._persistent_human_decision_result_for_task(synthetic_task, pdf_mapper_model_name())
        if persistent is not None:
            return self._pdf_claim_row_from_persistent_decision(base, claim, persistent)
        result = validate_decision(
            synthetic_task,
            decision,
            model_name=pdf_mapper_model_name(),
            prompt_version=PDF_PROMPT_VERSION,
            cache_key=pdf_mapper_cache_key(task, pdf_mapper_model_name()),
            llm_call=False,
            cache_hit=True,
            retries=0,
        )
        reviewed = self._review_if_needed(synthetic_task, result, review_model, cache, refresh_reviewer=True)
        status = reviewed.status
        if status == "technical_repair" and str(reviewed.technical_detail or "") == "reviewer_model_unavailable":
            status = "human_legal_review"
        authoritative_decision = _reviewer_final_decision(reviewed)
        authoritative_focal_role = str(reviewed.focal_role or "").strip() or str(claim.focal_role or "").strip()
        authoritative_rationale = str(reviewed.rationale or claim.mapping_rationale or "").strip()
        conflicts = _pdf_claim_authoritative_conflicts(claim, reviewed, authoritative_decision, authoritative_focal_role)
        if _malaysia_p4_pdf_reviewer_no_match_rejected(task, reviewed, authoritative_decision):
            status = "rejected"
            conflicts = []
        if status == "accepted" and conflicts:
            status = "human_legal_review"
        reviewer_attrs = reviewed.reviewer_attributes or _reviewer_attrs_from_pdf_claim_attributes(claim.attributes)
        return dict(
            base,
            status=status,
            authoritative_status=reviewed.status,
            authoritative_decision=authoritative_decision,
            authoritative_focal_role=authoritative_focal_role,
            authoritative_rationale=authoritative_rationale,
            authoritative_conflicts=conflicts,
            result_code=reviewed.result_code,
            technical_detail=reviewed.technical_detail,
            uncertain_elements=[*reviewed.uncertain_elements, *conflicts],
            uncertain_exclusions=reviewed.uncertain_exclusions,
            focal_uncertainty=reviewed.focal_uncertainty or ("pdf_claim_reviewer_cache_missing" if str(reviewed.technical_detail or "") == "reviewer_model_unavailable" else ("pdf_claim_authoritative_decision_conflict" if conflicts else "")),
            reviewer_llm_call=reviewed.reviewer_llm_call,
            reviewer_cache_hit=reviewed.reviewer_cache_hit,
            reviewer_cache_key=reviewed.reviewer_cache_key or "",
            reviewer_attributes=reviewer_attrs.model_dump(),
            validated_attributes=_validated_attributes_for_indicator(
                claim.indicator_id,
                reviewer_attrs,
                synthetic_task,
                reviewed.accepted_matches[0] if reviewed.accepted_matches else {},
            ),
            reviewed_result=reviewed.model_dump(),
        )

    def _pdf_claim_row_from_persistent_decision(self, base: dict, claim: PDFEvidenceClaim, result: ValidatedTaskResult) -> dict:
        accepted = result.status == "accepted"
        attrs = result.human_validated_attributes or (result.reviewer_attributes.model_dump() if result.reviewer_attributes else claim.attributes.model_dump())
        match = result.accepted_matches[0] if result.accepted_matches else {}
        return dict(
            base,
            article=result.focal_provision_id or base.get("article"),
            verbatim_snippet=str(match.get("quote") or base.get("verbatim_snippet") or ""),
            mapping_rationale=result.rationale,
            status="accepted" if accepted else ("rejected" if result.status == "rejected" else result.status),
            authoritative_status="accepted" if accepted else ("rejected" if result.status == "rejected" else result.status),
            authoritative_decision="accepted" if accepted else ("rejected" if result.status == "rejected" else str(result.result_code or "")),
            authoritative_focal_role=result.focal_role or claim.focal_role or "operative",
            authoritative_rationale=result.rationale,
            authoritative_conflicts=[],
            result_code=result.result_code,
            technical_detail=result.technical_detail,
            uncertain_elements=result.uncertain_elements,
            uncertain_exclusions=result.uncertain_exclusions,
            focal_uncertainty=result.focal_uncertainty or "",
            reviewer_llm_call=False,
            reviewer_cache_hit=True,
            reviewer_cache_key=result.reviewer_cache_key or "",
            reviewer_attributes=attrs,
            validated_attributes=attrs,
            reviewed_result=result.model_dump(),
            citation_status="verified" if accepted else base.get("citation_status"),
            citation_error="" if accepted else base.get("citation_error"),
            confidence=1.0 if accepted else base.get("confidence"),
        )

    def _validate_pdf_claim_citation(self, task: PDFDocumentTask, claim: PDFEvidenceClaim, index: int, cache: dict[str, dict]):
        key = pdf_citation_cache_key(task, claim, "docling_page")
        if key in cache:
            data = cache[key]
            cached_status = data.get("status", "unverifiable")
            if cached_status != "failed":
                return type("_CachedCitation", (), {"status": cached_status, "error": data.get("error", "")})()
        validation = validate_docling_page_citation(
            verbatim_snippet=claim.verbatim_snippet,
            artifact_path=Path(task.pdf_text_path),
            page_number=claim.page_number,
            article=claim.article,
            source_url=task.source_url,
            expected_text_hash=task.document_text_hash or task.source_sha256,
        )
        cached = cache.get(key)
        if cached is None or cached.get("status") != validation.status or cached.get("error") != validation.error:
            self._append_cache(key, PDF_CITATION_PROMPT_VERSION, "docling_page", f"{task.task_id}:claim-{index}", "pdf_citation", {"status": validation.status, "error": validation.error}, cache)
        return validation

    def _append_cache(self, key: str, prompt_version: str, model: str, task_id: str, task_type: str, decision: dict, cache: dict[str, dict]) -> None:
        with self.cache_lock:
            append_jsonl_atomic(
                self.cache_path,
                {
                    "key": key,
                    "stage": _cache_stage(task_type),
                    "prompt_version": prompt_version,
                    "model_name": model,
                    "task_id": task_id,
                    "task_type": task_type,
                    "decision": decision,
                },
            )
            cache[key] = decision

    def _canonical_document_meta(self, document_id: str) -> dict:
        if self._canonical_doc_meta_cache is None:
            docs: dict[str, dict] = {}
            document_manifest = self.root / "zone1_documents.jsonl"
            if document_manifest.exists():
                for row in read_jsonl(document_manifest):
                    doc_id = str(row.get("document_id") or "").strip()
                    if not doc_id:
                        continue
                    docs[doc_id] = row
            self._canonical_doc_meta_cache = docs
        return self._canonical_doc_meta_cache.get(document_id, {})

    def _canonical_provision_meta(self, document_id: str, provision_id: str) -> dict:
        if not document_id or not provision_id:
            return {}
        if document_id not in self._canonical_provision_meta_cache:
            self._load_canonical_provision_table(document_id)
        table = self._canonical_provision_meta_cache.get(document_id, {})
        if provision_id in table:
            return table[provision_id]
        return table.get(_strip_trailing_subparts(provision_id), {})

    def _load_canonical_provision_table(self, document_id: str) -> None:
        if document_id in self._canonical_provision_meta_cache:
            return
        manifest_row = None
        provision_manifest = self.root / "zone1_provisions_manifest.jsonl"
        if provision_manifest.exists():
            for row in read_jsonl(provision_manifest):
                if str(row.get("document_id") or "").strip() == document_id:
                    manifest_row = row
                    break
        table: dict[str, dict] = {}
        if manifest_row is not None:
            rel_path = str(manifest_row.get("provisions_path") or "").strip()
            path = self._resolve_project_path(rel_path) if rel_path else None
            if path and path.exists():
                for row in read_jsonl(path):
                    pid = str(row.get("provision_id") or "").strip()
                    if pid:
                        table[pid] = row
        self._canonical_provision_meta_cache[document_id] = table

    def _canonical_article_reference(self, *, document_id: str, provision_id: str, focal_quote: str, law_title: str) -> str:
        meta = self._canonical_provision_meta(document_id, provision_id)
        canonical_citation = str(meta.get("canonical_citation") or "").strip()
        number = str(meta.get("provision_number") or meta.get("section") or "").strip()
        composite_number = _composite_section_number_from_meta(meta, number)
        if composite_number:
            number = composite_number
        if not number:
            return canonical_citation
        suffix = _leading_subsection_from_quote(focal_quote)
        prefix = _citation_prefix_from_collection(
            str(self._canonical_document_meta(document_id).get("collection") or ""),
            law_title,
        )
        exact = f"{number}{suffix}".strip() if number.startswith(("s. ", "Reg. ", "Rule ", "Art. ")) else f"{prefix} {number}{suffix}".strip()
        if number.startswith(("s. ", "Reg. ", "Rule ", "Art. ")):
            exact = f"{number}{suffix}".strip()
        if canonical_citation:
            if canonical_citation.startswith("Schedule 1, APP"):
                heading_blob = " ".join(str(meta.get(key) or "") for key in ("article", "heading", "provision_number"))
                if re.search(r"^\s*(?:\d{1,2}\s+)?Australian\s+Privacy\s+Principle\s+\d{1,2}\b", heading_blob, flags=re.I):
                    return canonical_citation
                return exact
            if suffix and suffix not in canonical_citation:
                return f"{canonical_citation}, {exact}"
            return canonical_citation
        return exact

    def _canonical_location_reference(self, *, document_id: str, provision_id: str, focal_quote: str = "") -> str:
        meta = self._canonical_provision_meta(document_id, provision_id)
        page = str(meta.get("source_page_number") or meta.get("page_number") or "").strip()
        path = _clean_chunk_path(str(meta.get("provision_path") or "").strip())
        if page and path:
            return f"Page {page} / {path}"
        if page:
            return f"Page {page}"
        return path

    def load_contexts(self) -> list[ProvisionContext]:
        return list(self.iter_contexts())

    def load_pdf_document_tasks(self) -> list[PDFDocumentTask]:
        document_manifest = self.root / "zone1_documents.jsonl"
        if document_manifest.exists() and document_manifest.stat().st_size:
            return self._load_pdf_document_tasks_from_documents(document_manifest)
        return []

    def _load_pdf_document_tasks_from_documents(self, document_manifest: Path) -> list[PDFDocumentTask]:
        if docling_worker_count() > 1:
            return self._load_pdf_document_tasks_from_documents_parallel(document_manifest)
        tasks: list[PDFDocumentTask] = []
        seen: set[str] = set()
        stats: dict[str, object] = {
            "document_direct_records_scanned": 0,
            "recall_candidate_pdfs": 0,
            "recall_non_candidate_pdfs": 0,
            "recall_source_type_distribution": Counter(),
            "candidate_counts_by_indicator": Counter(),
            "existing_docling_artifacts_valid": 0,
            "existing_docling_artifacts_reused": 0,
            "pending_docling_materialization": 0,
            "artifact_materialization_attempted": 0,
            "docling_native_text_success": 0,
            "docling_ocr_fallback_attempted": 0,
            "docling_ocr_fallback_success": 0,
            "docling_failed": 0,
            "docling_failure_reasons": Counter(),
            "explicitly_skipped_with_reason": Counter(),
            "post_docling_routed_documents": 0,
            "post_docling_candidates_retained": 0,
            "post_docling_filtered_out": 0,
            "direct_mapper_tasks_created": 0,
        }
        for row in read_jsonl(document_manifest):
            row = _malaysia_p4_document_row(row, self.project_root, self.pillars)
            processing_mode = str(row.get("processing_mode") or "").casefold()
            if processing_mode != "document_direct":
                continue
            stats["document_direct_records_scanned"] = int(stats["document_direct_records_scanned"]) + 1
            if str(row.get("download_status") or "success").casefold() not in {"success", "available", ""}:
                stats["explicitly_skipped_with_reason"]["download_unavailable"] += 1  # type: ignore[index]
                continue
            document_id = str(row.get("document_id") or "").strip()
            if not document_id or document_id in seen:
                stats["explicitly_skipped_with_reason"]["duplicate_or_missing_document_id"] += 1  # type: ignore[index]
                continue
            seen.add(document_id)
            status = str(row.get("prefilter_status") or "uncertain").casefold()
            if status not in {"candidate", "reject", "uncertain", "pass", "relevant", "review"}:
                status = "uncertain"
            raw_path = self._resolve_project_path(row.get("raw_path"))
            recall = _recall_route_document(
                row=row, project_root=self.project_root, pillars=self.pillars
            )
            stats["recall_source_type_distribution"][recall["recall_source_type"]] += 1  # type: ignore[index]
            if not recall["candidate_indicators"]:
                stats["recall_non_candidate_pdfs"] = int(stats["recall_non_candidate_pdfs"]) + 1
                continue
            stats["recall_candidate_pdfs"] = int(stats["recall_candidate_pdfs"]) + 1
            stats["candidate_counts_by_indicator"].update(recall["candidate_indicators"])  # type: ignore[union-attr]
            had_valid_artifact = _valid_existing_docling_artifact(row=row, project_root=self.project_root)
            if had_valid_artifact:
                stats["existing_docling_artifacts_valid"] = int(stats["existing_docling_artifacts_valid"]) + 1
            else:
                stats["pending_docling_materialization"] = int(stats["pending_docling_materialization"]) + 1
                stats["artifact_materialization_attempted"] = int(stats["artifact_materialization_attempted"]) + 1
            artifact_path, artifact, artifact_status = _materialize_docling_for_candidate(row=row, project_root=self.project_root)
            if artifact_path is None or artifact is None:
                stats["docling_failed"] = int(stats["docling_failed"]) + 1
                reason = artifact_status or "docling_failed"
                stats["docling_failure_reasons"][reason] += 1  # type: ignore[index]
                continue
            if artifact_status == "reused":
                stats["existing_docling_artifacts_reused"] = int(stats["existing_docling_artifacts_reused"]) + 1
            else:
                extraction_pass = str(artifact.get("extraction_pass") or "")
                if extraction_pass == "native_text":
                    stats["docling_native_text_success"] = int(stats["docling_native_text_success"]) + 1
                elif extraction_pass == "ocr_fallback":
                    stats["docling_ocr_fallback_attempted"] = int(stats["docling_ocr_fallback_attempted"]) + 1
                    stats["docling_ocr_fallback_success"] = int(stats["docling_ocr_fallback_success"]) + 1
            source_hash = str(artifact.get("document_text_hash") or "")
            if not source_hash:
                stats["docling_failed"] = int(stats["docling_failed"]) + 1
                stats["docling_failure_reasons"]["document_text_hash_missing"] += 1  # type: ignore[index]
                continue
            stats["post_docling_routed_documents"] = int(stats["post_docling_routed_documents"]) + 1
            routed = _route_docling_document(
                row=row, artifact_path=artifact_path, pillars=self.pillars
            )
            if not routed["candidate_indicators"]:
                stats["post_docling_filtered_out"] = int(stats["post_docling_filtered_out"]) + 1
                continue
            stats["post_docling_candidates_retained"] = int(stats["post_docling_candidates_retained"]) + 1
            candidate_indicators = routed["candidate_indicators"]
            task_id = f"docdirect:{document_id}:{source_hash[:16]}:{_slug('-'.join(candidate_indicators))}"
            override_fingerprint = malaysia_p4_override_fingerprint(row, self.project_root) if self.pillars == {4} else ""
            if override_fingerprint:
                task_id = f"{task_id}:{hashlib.sha256(override_fingerprint.encode('utf-8')).hexdigest()[:12]}"
            tasks.append(
                PDFDocumentTask(
                    task_id=task_id,
                    economy=str(row.get("economy") or self.economy),
                    document_id=document_id,
                    collection=str(row.get("collection") or ""),
                    title=str(row.get("title") or document_id),
                    official_number=str(row.get("official_number") or ""),
                    year=str(row.get("year") or ""),
                    language=str(row.get("language") or ""),
                    source_url=str(row.get("source_url") or row.get("canonical_url") or ""),
                    raw_path=str(raw_path),
                    pdf_text_path=str(artifact_path),
                    document_text_hash=source_hash,
                    candidate_indicators=candidate_indicators,
                    matched_pages=routed["matched_pages"],
                    matched_context=routed["matched_context"],
                    prefilter_status=status,  # type: ignore[arg-type]
                    source_sha256=source_hash,
                    page_count=routed["page_count"],
                )
            )
            stats["direct_mapper_tasks_created"] = int(stats["direct_mapper_tasks_created"]) + 1
            attempted = int(stats["artifact_materialization_attempted"])
            if attempted and attempted % 50 == 0:
                print(
                    "Docling materialization progress: "
                    f"{attempted}/{stats['pending_docling_materialization']} | "
                    f"Artifacts reused: {stats['existing_docling_artifacts_reused']} | "
                    f"Native success: {stats['docling_native_text_success']} | "
                    f"OCR success: {stats['docling_ocr_fallback_success']} | "
                    f"Failed: {stats['docling_failed']} | "
                    f"Direct tasks created so far: {stats['direct_mapper_tasks_created']}",
                    flush=True,
                )
        self.pdf_direct_stats = _finalize_pdf_direct_stats(stats)
        _assert_pdf_direct_invariants(self.pdf_direct_stats)
        return tasks

    def _load_pdf_document_tasks_from_documents_parallel(self, document_manifest: Path) -> list[PDFDocumentTask]:
        stats: dict[str, object] = {
            "document_direct_records_scanned": 0,
            "recall_candidate_pdfs": 0,
            "recall_non_candidate_pdfs": 0,
            "recall_source_type_distribution": Counter(),
            "candidate_counts_by_indicator": Counter(),
            "existing_docling_artifacts_valid": 0,
            "existing_docling_artifacts_reused": 0,
            "pending_docling_materialization": 0,
            "artifact_materialization_attempted": 0,
            "docling_native_text_success": 0,
            "docling_ocr_fallback_attempted": 0,
            "docling_ocr_fallback_success": 0,
            "docling_failed": 0,
            "docling_failure_reasons": Counter(),
            "explicitly_skipped_with_reason": Counter(),
            "post_docling_routed_documents": 0,
            "post_docling_candidates_retained": 0,
            "post_docling_filtered_out": 0,
            "direct_mapper_tasks_created": 0,
        }
        seen: set[str] = set()
        candidates: list[tuple[int, dict, str]] = []
        for index, row in enumerate(read_jsonl(document_manifest)):
            row = _malaysia_p4_document_row(row, self.project_root, self.pillars)
            processing_mode = str(row.get("processing_mode") or "").casefold()
            if processing_mode != "document_direct":
                continue
            stats["document_direct_records_scanned"] = int(stats["document_direct_records_scanned"]) + 1
            if str(row.get("download_status") or "success").casefold() not in {"success", "available", ""}:
                stats["explicitly_skipped_with_reason"]["download_unavailable"] += 1  # type: ignore[index]
                continue
            document_id = str(row.get("document_id") or "").strip()
            if not document_id or document_id in seen:
                stats["explicitly_skipped_with_reason"]["duplicate_or_missing_document_id"] += 1  # type: ignore[index]
                continue
            seen.add(document_id)
            recall = _recall_route_document(
                row=row, project_root=self.project_root, pillars=self.pillars
            )
            stats["recall_source_type_distribution"][recall["recall_source_type"]] += 1  # type: ignore[index]
            if not recall["candidate_indicators"]:
                stats["recall_non_candidate_pdfs"] = int(stats["recall_non_candidate_pdfs"]) + 1
                continue
            stats["recall_candidate_pdfs"] = int(stats["recall_candidate_pdfs"]) + 1
            stats["candidate_counts_by_indicator"].update(recall["candidate_indicators"])  # type: ignore[union-attr]
            if _valid_existing_docling_artifact(row=row, project_root=self.project_root):
                stats["existing_docling_artifacts_valid"] = int(stats["existing_docling_artifacts_valid"]) + 1
            else:
                stats["pending_docling_materialization"] = int(stats["pending_docling_materialization"]) + 1
                stats["artifact_materialization_attempted"] = int(stats["artifact_materialization_attempted"]) + 1
            candidates.append((index, row, str(row.get("prefilter_status") or "uncertain")))

        results: list[dict] = []
        workers = docling_worker_count()
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    _materialize_route_docling_candidate_for_worker,
                    index,
                    row,
                    str(self.project_root),
                    status,
                    tuple(sorted(self.pillars)),
                )
                for index, row, status in candidates
            ]
            completed = 0
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                completed += 1
                attempted = int(stats["artifact_materialization_attempted"])
                if attempted and completed % 50 == 0:
                    created = sum(1 for item in results if item.get("artifact_status") not in {"reused", None})
                    failed = sum(1 for item in results if item.get("status") == "failed")
                    tasks_so_far = sum(1 for item in results if item.get("task"))
                    reused = sum(1 for item in results if item.get("artifact_status") == "reused")
                    print(
                        "Docling materialization progress: "
                        f"{min(completed, attempted)}/{stats['pending_docling_materialization']} | "
                        f"Artifacts reused: {reused} | Native success: {created} | OCR success: 0 | "
                        f"Failed: {failed} | Direct tasks created so far: {tasks_so_far}",
                        flush=True,
                    )

        tasks: list[PDFDocumentTask] = []
        for result in sorted(results, key=lambda item: int(item.get("index") or 0)):
            if result.get("status") == "failed":
                stats["docling_failed"] = int(stats["docling_failed"]) + 1
                stats["docling_failure_reasons"][str(result.get("reason") or "docling_failed")] += 1  # type: ignore[index]
                continue
            artifact_status = str(result.get("artifact_status") or "")
            if artifact_status == "reused":
                stats["existing_docling_artifacts_reused"] = int(stats["existing_docling_artifacts_reused"]) + 1
            else:
                extraction_pass = str(result.get("extraction_pass") or "")
                if extraction_pass == "native_text":
                    stats["docling_native_text_success"] = int(stats["docling_native_text_success"]) + 1
                elif extraction_pass == "ocr_fallback":
                    stats["docling_ocr_fallback_attempted"] = int(stats["docling_ocr_fallback_attempted"]) + 1
                    stats["docling_ocr_fallback_success"] = int(stats["docling_ocr_fallback_success"]) + 1
            stats["post_docling_routed_documents"] = int(stats["post_docling_routed_documents"]) + 1
            task = result.get("task")
            if not task:
                stats["post_docling_filtered_out"] = int(stats["post_docling_filtered_out"]) + 1
                continue
            stats["post_docling_candidates_retained"] = int(stats["post_docling_candidates_retained"]) + 1
            stats["direct_mapper_tasks_created"] = int(stats["direct_mapper_tasks_created"]) + 1
            tasks.append(PDFDocumentTask(**task))
        self.pdf_direct_stats = _finalize_pdf_direct_stats(stats)
        _assert_pdf_direct_invariants(self.pdf_direct_stats)
        return tasks

    def iter_contexts(self):
        provision_manifest = self.root / "zone1_provisions_manifest.jsonl"
        if not provision_manifest.exists() or not provision_manifest.stat().st_size:
            raise RuntimeError(
                "Zone 1 per-document provision manifest missing. Run: "
                f"python -m rdtii_tool build-corpus --economy {self.economy_slug} --zone 1"
            )
        seen_records = False
        found = False
        for row in self._iter_zone1_provision_rows(provision_manifest):
            seen_records = True
            record_type = str(row.get("record_type") or "provision").casefold()
            processing_mode = str(row.get("processing_mode") or "").strip()
            if not processing_mode:
                raise RuntimeError(
                    "Zone 1 provision row missing processing_mode; rebuild Zone 1 before mapping. "
                    f"document_id={row.get('document_id')} provision_id={row.get('provision_id')}"
                )
            if record_type == "pdf_document" or processing_mode == "document_direct":
                # Document-direct sources are loaded from zone1_documents and
                # routed over Docling artifacts; they are never treated as
                # canonical structured provisions.
                continue
            if processing_mode == "parse_failed":
                continue
            if str(row.get("extraction_method") or "").casefold() == "document_fallback":
                continue
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            provision_path_parts = str(row.get("provision_path") or "")
            parts = [part.strip() for part in provision_path_parts.split("/") if part.strip()]
            found = True
            yield ProvisionContext(
                economy=str(row.get("economy") or self.economy),
                document_id=str(row["document_id"]),
                law_title=str(row.get("title") or row.get("official_title") or row["document_id"]),
                instrument_type=str(row.get("collection") or row.get("source_format") or ""),
                    source_url=row.get("canonical_url") or row.get("source_url"),
                    provision_id=str(row["provision_id"]),
                    processing_mode=processing_mode,
                    citation_mode="structured_provision",
                    source_locator=str(row.get("source_locator") or row.get("anchor_url") or row.get("anchor") or row.get("provision_id") or ""),
                    focal_text_hash=str(row.get("text_hash") or hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()),
                    document_content_hash=str(row.get("document_content_hash") or row.get("content_hash") or ""),
                    canonical_schema_version=str(row.get("canonical_schema_version") or row.get("parser_version") or ""),
                    provision_metadata_snapshot={
                        "article": row.get("article") or row.get("provision_number") or row.get("section") or "",
                        "heading": row.get("heading") or row.get("provision_label") or "",
                        "hierarchy": row.get("hierarchy") or [],
                        "anchor_url": row.get("anchor_url") or row.get("anchor") or "",
                    },
                    section_reference=str(
                        row.get("provision_path")
                        or row.get("heading")
                        or row.get("article")
                        or row.get("section")
                        or row.get("provision_label")
                        or row["provision_id"]
                    ),
                text=text,
                part_heading="; ".join(parts[:1]),
                division_heading="; ".join(parts[1:2]),
            )
        if not seen_records:
            raise RuntimeError(f"No provision rows found from {provision_manifest}")

    def _iter_zone1_provision_rows(self, provision_manifest: Path):
        allowed_statuses = {"success", "partial", "pdf_document", ""}
        for manifest_row in read_jsonl(provision_manifest):
            if str(manifest_row.get("extraction_status") or "").casefold() not in allowed_statuses:
                continue
            rel_path = str(manifest_row.get("provisions_path") or "").strip()
            if not rel_path:
                continue
            path = Path(rel_path)
            if not path.is_absolute():
                path = self.root.parent.parent.parent / path
            if not path.exists() or not path.stat().st_size:
                continue
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        yield row

    def _resolve_project_path(self, value) -> Path:
        path = Path(str(value or ""))
        if path.is_absolute():
            return path
        return self.root.parent.parent.parent / path

    def _load_cache(self) -> dict[str, dict]:
        cache: dict[str, dict] = {}
        for row in read_jsonl(self.cache_path):
            if row.get("key") and row.get("decision") and row.get("prompt_version"):
                cache[row["key"]] = row["decision"]
        return cache

    def _mapper_replay_coverage(
        self,
        provision_tasks: list[CandidateTask],
        model: str,
        cache: dict[str, dict],
    ) -> dict:
        missing: list[str] = []
        mapper_hits = 0
        for task in provision_tasks:
            if task.task_kind == "treaty_provision":
                mapper_hits += 1
                continue
            for key in mapper_cache_lookup_keys(task, model, prompt_version_for_task(task)):
                try:
                    _parse_cached_mapping_decision(cache.get(key))
                except Exception:
                    continue
                mapper_hits += 1
                break
            else:
                missing.append(task.task_id)
        return {"mapper_hits": mapper_hits, "missing_task_ids": missing}

    def _pdf_replay_coverage(self, pdf_tasks: list[PDFDocumentTask], cache: dict[str, dict]) -> dict:
        pdf_model = pdf_mapper_model_name()
        hits = 0
        missing = []
        for task in pdf_tasks:
            if task.prefilter_status == "reject":
                continue
            key = pdf_mapper_cache_key(task, pdf_model)
            try:
                PDFMappingDecision.model_validate(cache.get(key))
            except Exception:
                missing.append(task.task_id)
            else:
                hits += 1
        return {"pdf_mapper_hits": hits, "missing_pdf_mapper_cache": len(missing), "missing_pdf_task_ids": missing[:50]}

    def _print_pdf_direct_plan(self, pdf_tasks: list[PDFDocumentTask], pdf_coverage: dict) -> None:
        stats = self.pdf_direct_stats or {}
        print(f"Document-direct records scanned: {stats.get('document_direct_records_scanned', 0)}", flush=True)
        print(f"Recall candidate PDFs: {stats.get('recall_candidate_pdfs', 0)}", flush=True)
        print(f"Recall non-candidate PDFs: {stats.get('recall_non_candidate_pdfs', 0)}", flush=True)
        print(f"Existing Docling artifacts valid: {stats.get('existing_docling_artifacts_valid', 0)}", flush=True)
        print(f"Existing Docling artifacts reused: {stats.get('existing_docling_artifacts_reused', 0)}", flush=True)
        print(f"Pending Docling materialization: {stats.get('pending_docling_materialization', 0)}", flush=True)
        print(f"Docling native-text success: {stats.get('docling_native_text_success', 0)}", flush=True)
        print(f"Docling OCR fallback attempted: {stats.get('docling_ocr_fallback_attempted', 0)}", flush=True)
        print(f"Docling OCR fallback success: {stats.get('docling_ocr_fallback_success', 0)}", flush=True)
        print(f"Docling failed: {stats.get('docling_failed', 0)}", flush=True)
        print(f"Post-Docling candidates retained: {stats.get('post_docling_candidates_retained', 0)}", flush=True)
        print(f"Post-Docling candidates filtered out: {stats.get('post_docling_filtered_out', 0)}", flush=True)
        print(f"Direct mapper tasks created: {stats.get('direct_mapper_tasks_created', len(pdf_tasks))}", flush=True)
        print(f"Direct mapper tasks loaded from cache: {pdf_coverage['pdf_mapper_hits']}", flush=True)

    def _mapper_replay_available_task_ids(
        self,
        provision_tasks: list[CandidateTask],
        model: str,
        cache: dict[str, dict],
    ) -> list[str]:
        available: list[str] = []
        for task in provision_tasks:
            if task.task_kind == "treaty_provision":
                available.append(task.task_id)
                continue
            for key in mapper_cache_lookup_keys(task, model, prompt_version_for_task(task)):
                try:
                    _parse_cached_mapping_decision(cache.get(key))
                except Exception:
                    continue
                available.append(task.task_id)
                break
        return available

    def _write_cache_compatibility_report(self, coverage: dict, provision_total: int, cache: dict[str, dict]) -> None:
        stage_counts: Counter[str] = Counter()
        for row in read_jsonl(self.cache_path):
            stage_counts[str(row.get("stage") or "unknown")] += 1
        report = {
            "generated_at": time.time(),
            "economy": self.economy,
            "candidate_tasks": provision_total,
            "mapper_cache_hits": coverage["mapper_hits"],
            "missing_mapper_cache": len(coverage["missing_task_ids"]),
            "missing_task_ids_sample": coverage["missing_task_ids"][:50],
            "cache_entries_loaded": len(cache),
            "cache_stage_counts": dict(stage_counts),
            "mapper_cache_reuse_allowed": not coverage["missing_task_ids"],
            "reviewer_cache_policy": "reuse exact current reviewer keys only; stale or legacy reviewer cache is not accepted for production outputs",
            "final_outputs_used_as_cache": False,
        }
        write_json(self.output_dir / "cache_compatibility_report.json", report)

    def _validate_rebuild_run(
        self,
        summary: dict,
        provision_results: list[ValidatedTaskResult],
        coverage: dict,
        pdf_coverage: dict | None = None,
    ) -> dict:
        pdf_coverage = pdf_coverage or {}
        reviewer_schema_errors = self._reviewer_schema_error_count(provision_results)
        provision_total = int(summary.get("candidate_tasks") or 0)
        pdf_total = int(summary.get("pdf_documents") or 0)
        provision_status_total = (
            sum(1 for item in provision_results if item.status == "accepted")
            + sum(1 for item in provision_results if item.status == "rejected")
            + sum(1 for item in provision_results if item.status == "supporting_only")
            + sum(1 for item in provision_results if item.queue_type == "model_review_pending")
            + sum(1 for item in provision_results if item.queue_type == "human_legal_review")
            + sum(1 for item in provision_results if item.queue_type == "technical_repair")
        )
        failures: list[str] = []
        warnings: list[str] = []
        if reviewer_schema_errors > 0:
            warnings.append("reviewer_schema_error_present")
        if provision_status_total != provision_total:
            warnings.append("task_status_total_mismatch")
        if int(summary.get("technical_repair_tasks") or 0) > 0:
            warnings.append("technical_repair_tasks_present")
        if int(summary.get("pdf_documents_failed") or 0) > 0:
            warnings.append("pdf_technical_repair_present")
        if provision_total <= 0 and pdf_total <= 0:
            failures.append("zero_mapping_tasks")
        if int(summary.get("submission_rows") or 0) <= 0 and int(summary.get("accepted_measures") or 0) <= 0:
            failures.append("no_valid_outputs")
        if not self.live and len(coverage.get("missing_task_ids") or []) > 0:
            warnings.append("cache_only_mapper_cache_missing")
        if not self.live and int(pdf_coverage.get("missing_pdf_mapper_cache") or 0) > 0:
            warnings.append("cache_only_pdf_mapper_cache_missing")
        if not self.live and int(summary.get("pdf_documents_pending_live_processing") or 0) > 0:
            warnings.append("cache_only_pdf_pending_live_processing")
        if not self.live and int(summary.get("network_calls") or 0) > 0:
            warnings.append("cache_only_network_calls_present")
        counts_total = sum(int(value) for value in (summary.get("counts_by_indicator") or {}).values())
        if counts_total != int(summary.get("accepted_measures") or 0):
            warnings.append("counts_by_indicator_total_mismatch")
        semantic_checks = self._semantic_validation_checks()
        warnings.extend(semantic_checks["failures"])
        return {
            "generated_at": time.time(),
            "passed": not failures,
            "failures": failures,
            "warnings": warnings,
            "status": "completed_with_warnings" if warnings and not failures else ("failed" if failures else "completed"),
            "summary": summary,
            "checks": {
                "reviewer_schema_errors": reviewer_schema_errors,
                "status_total": provision_status_total,
                "candidate_tasks": provision_total,
                "mapper_cache_hits": coverage.get("mapper_hits"),
                "missing_mapper_cache": len(coverage.get("missing_task_ids") or []),
                "pdf_mapper_cache_hits": pdf_coverage.get("pdf_mapper_hits"),
                "missing_pdf_mapper_cache": int(pdf_coverage.get("missing_pdf_mapper_cache") or 0),
                "counts_by_indicator_total": counts_total,
                "non_citation_technical_repairs": max(
                    0,
                    int(summary.get("technical_repair_tasks") or 0)
                    - int(summary.get("citation_failed") or 0)
                    - int(summary.get("citation_unverifiable") or 0),
                ),
                "semantic_consistency": semantic_checks,
            },
        }

    def _semantic_validation_checks(self) -> dict:
        if self.pillars == {4}:
            return self._p4_semantic_validation_checks()
        failures: list[str] = []
        details: dict[str, int | list[str]] = {}
        atomic_rows = read_jsonl(self.output_dir / "atomic_evidence.jsonl")
        submission_path = self.submission_dir / f"{self.output_prefix}.csv"
        submission_rows: list[dict] = []
        if submission_path.exists():
            with submission_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != SUBMISSION_COLUMNS:
                    failures.append("submission_columns_not_canonical")
                submission_rows = list(reader)
        else:
            failures.append("submission_csv_missing")

        framework_articles = [row for row in atomic_rows if str(row.get("article") or "").startswith("framework-")]
        details["framework_article_records"] = len(framework_articles)
        if framework_articles:
            failures.append("framework_article_in_atomic_evidence")

        reviewerless = [
            row for row in atomic_rows
            if row.get("decision") == "accepted"
            and not row.get("reviewer_task_id")
        ]
        details["accepted_reviewerless"] = len(reviewerless)
        if reviewerless:
            failures.append("accepted_evidence_without_reviewer")

        p7i3_missing = []
        p7i4_missing = []
        p7i5_missing = []
        contradiction = []
        p6i2_situs_missing = []
        p6i4_non_data_asset = []
        p7i3_indeterminate_duration = []
        invalid_article = []
        citation_provenance_failures = []
        mixed_citation_source = []
        internal_locator_records = []
        legacy_attrs = []
        framework_elements: dict[str, set[str]] = {"P7-I1": set(), "P7-I2": set()}
        seen: set[tuple[str, str, str, str, str]] = set()
        duplicate_count = 0
        for row in atomic_rows:
            attrs = _row_validated_attributes(row)
            if "reviewer_attributes" in row:
                legacy_attrs.append(str(row.get("evidence_id") or ""))
            if row.get("decision") == "accepted" and row.get("citation_status") == "verified":
                key = (
                    str(row.get("economy") or ""),
                    str(row.get("document_id") or ""),
                    str(row.get("article") or ""),
                    str(row.get("indicator_id") or ""),
                    " ".join(_row_focal_quote(row).split()),
                )
                if key in seen:
                    duplicate_count += 1
                seen.add(key)
                if row.get("indicator_id") == "P7-I3" and not _validated_retention_periods_present(attrs):
                    p7i3_missing.append(str(row.get("evidence_id") or ""))
                if row.get("indicator_id") == "P7-I4" and attrs.get("accountability_path") in {None, "", "uncertain"}:
                    p7i4_missing.append(str(row.get("evidence_id") or ""))
                if row.get("indicator_id") == "P7-I5" and attrs.get("judicial_authorization") in {None, "", "uncertain"}:
                    p7i5_missing.append(str(row.get("evidence_id") or ""))
                if row.get("indicator_id") in {"P7-I1", "P7-I2"}:
                    element = str(attrs.get("framework_element") or "")
                    if element:
                        framework_elements[str(row.get("indicator_id"))].add(element)
                if row.get("indicator_id") == "P6-I2" and not _p6_i2_submission_snippet_has_storage_situs(row, self.economy):
                    p6i2_situs_missing.append(str(row.get("evidence_id") or ""))
                if row.get("indicator_id") in {"P6-I1", "P6-I4"} and _p6_i4_submission_non_data_asset(row):
                    p6i4_non_data_asset.append(str(row.get("evidence_id") or ""))
                if row.get("indicator_id") == "P7-I3" and _p7_i3_submission_indeterminate_duration(row):
                    p7i3_indeterminate_duration.append(str(row.get("evidence_id") or ""))
                article = str(row.get("article") or "")
                if article.upper() == "SUPPORTING INSTRUMENT" or article.casefold().startswith("framework-"):
                    invalid_article.append(str(row.get("evidence_id") or ""))
                citation_mode = str(row.get("citation_mode") or "structured_provision")
                if citation_mode == "structured_provision":
                    canonical_id = str(row.get("canonical_provision_id") or "")
                    source_locator = str(row.get("source_locator") or "")
                    canonical_meta = self._canonical_provision_meta(str(row.get("document_id") or ""), canonical_id)
                    expected_locator = str(canonical_meta.get("source_locator") or canonical_meta.get("anchor_url") or canonical_meta.get("anchor") or canonical_meta.get("provision_id") or "")
                    expected_article = self._canonical_article_reference(
                        document_id=str(row.get("document_id") or ""),
                        provision_id=canonical_id,
                        focal_quote=_row_focal_quote(row),
                        law_title=str(row.get("law_name") or ""),
                    )
                    expected_location = self._canonical_location_reference(
                        document_id=str(row.get("document_id") or ""),
                        provision_id=canonical_id,
                        focal_quote=_row_focal_quote(row),
                    )
                    canonical_text = str(canonical_meta.get("text") or "")
                    if not canonical_meta or not canonical_id or not source_locator:
                        citation_provenance_failures.append(str(row.get("evidence_id") or ""))
                    elif expected_locator and source_locator != expected_locator:
                        citation_provenance_failures.append(str(row.get("evidence_id") or ""))
                    elif expected_article and article != expected_article:
                        mixed_citation_source.append(str(row.get("evidence_id") or ""))
                    elif expected_location and str(row.get("location_reference") or "") != expected_location:
                        mixed_citation_source.append(str(row.get("evidence_id") or ""))
                    elif canonical_text and _norm_key(_row_focal_quote(row)) not in _norm_key(canonical_text):
                        mixed_citation_source.append(str(row.get("evidence_id") or ""))
                    combined_locator = " ".join([article, str(row.get("location_reference") or "")]).casefold()
                    structured_locator = " ".join([combined_locator, canonical_id.casefold(), source_locator.casefold()])
                    if (
                        "1 text" in combined_locator
                        or "#chunk" in combined_locator
                        or "fallback chunk" in combined_locator
                        or str(row.get("location_reference") or "").casefold().startswith("pdf p.")
                        or "pdf-p-" in structured_locator
                    ):
                        internal_locator_records.append(str(row.get("evidence_id") or ""))
                elif citation_mode == "document_direct":
                    if not row.get("document_content_hash") or not row.get("page_number"):
                        citation_provenance_failures.append(str(row.get("evidence_id") or ""))
                elif citation_mode != "treaty_provision":
                    citation_provenance_failures.append(str(row.get("evidence_id") or ""))

        details["duplicate_submission_evidence"] = duplicate_count
        details["citation_provenance_failures"] = len(citation_provenance_failures)
        details["article_location_quote_different_source"] = len(mixed_citation_source)
        details["internal_locator_records"] = len(internal_locator_records)
        details["accepted_p7i3_missing_duration_attributes"] = len(p7i3_missing)
        details["accepted_p7i3_indeterminate_duration"] = len(p7i3_indeterminate_duration)
        details["accepted_p7i4_missing_accountability_path"] = len(p7i4_missing)
        details["accepted_p7i5_missing_judicial_authorization"] = len(p7i5_missing)
        details["accepted_p6i2_missing_storage_situs"] = len(p6i2_situs_missing)
        details["accepted_p6i4_non_data_asset"] = len(p6i4_non_data_asset)
        details["framework_atomic_evidence_bypass"] = 0
        details["invalid_article_records"] = len(invalid_article)
        details["accepted_rationale_contradictions"] = len(contradiction)
        details["legacy_reviewer_attributes"] = len(legacy_attrs)
        required_framework = {
            "P7-I1": {"personal_data_scope", "substantive_duties_or_rights", "regulator_or_enforcement"},
            "P7-I2": {"cybersecurity_scope", "substantive_cybersecurity_obligation", "authority_or_enforcement"},
        }
        missing_framework = {
            indicator: sorted(required - framework_elements[indicator])
            for indicator, required in required_framework.items()
            if required - framework_elements[indicator]
        }
        details["framework_element_coverage"] = {indicator: sorted(values) for indicator, values in framework_elements.items()}
        details["framework_element_missing"] = missing_framework
        if duplicate_count:
            failures.append("duplicate_submission_evidence")
        if p7i3_missing:
            failures.append("accepted_p7i3_missing_duration_attributes")
        if p7i4_missing:
            failures.append("accepted_p7i4_missing_accountability_path")
        if p7i5_missing:
            failures.append("accepted_p7i5_missing_judicial_authorization")
        if p6i2_situs_missing:
            failures.append("accepted_p6i2_missing_storage_situs")
        if p6i4_non_data_asset:
            failures.append("accepted_p6i4_non_data_asset")
        if p7i3_indeterminate_duration:
            failures.append("accepted_p7i3_indeterminate_duration")
        if invalid_article:
            failures.append("invalid_article_records")
        if contradiction:
            failures.append("accepted_rationale_contradiction")
        if legacy_attrs:
            failures.append("legacy_reviewer_attributes_present")
        if citation_provenance_failures:
            failures.append("citation_provenance_invalid")
        if mixed_citation_source:
            failures.append("article_location_quote_different_source")
        if internal_locator_records:
            failures.append("internal_locator_in_submission")

        bad_confidence = 0
        bad_rationale: list[str] = []
        for row in submission_rows:
            try:
                value = float(str(row.get("Confidence") or ""))
            except ValueError:
                bad_confidence += 1
                continue
            if not 0 <= value <= 1:
                bad_confidence += 1
            rationale_failures = validate_submission_rationale(str(row.get("Mapping Rationale") or ""))
            if rationale_failures:
                bad_rationale.append(
                    "|".join(
                        str(row.get(field) or "")
                        for field in ("Economy", "Indicator ID", "Law Name", "Article / Section")
                    )
                    + f":{','.join(rationale_failures)}"
                )
        details["bad_submission_confidence"] = bad_confidence
        details["bad_submission_rationale"] = len(bad_rationale)
        details["bad_submission_rationale_rows"] = bad_rationale[:50]
        if bad_confidence:
            failures.append("submission_confidence_not_numeric_0_1")
        if bad_rationale:
            failures.append("submission_mapping_rationale_invalid")
        return {"failures": failures, "details": details}

    def _p4_semantic_validation_checks(self) -> dict:
        failures: list[str] = []
        details: dict[str, object] = {}
        submission_path = self.submission_dir / f"{self.output_prefix}.csv"
        if not submission_path.exists():
            return {"failures": ["submission_csv_missing"], "details": details}
        with submission_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != SUBMISSION_COLUMNS:
                failures.append("submission_columns_not_canonical")
            rows = list(reader)
        valid_indicators = {f"P4-I{index}" for index in range(1, 11)}
        invalid_indicators = [
            str(row.get("Indicator ID") or "")
            for row in rows
            if str(row.get("Indicator ID") or "") not in valid_indicators
        ]
        numeric_law_names = [
            str(row.get("Law Name") or "")
            for row in rows
            if str(row.get("Law Name") or "").strip().isdigit()
        ]
        invalid_status_rows = [
            row
            for row in rows
            if str(row.get("Indicator ID") or "") in {"P4-I4", "P4-I7", "P4-I8"}
            and (
                str(row.get("Article / Section") or "") != "Treaty status"
                or not str(row.get("Source URL") or "").startswith("https://")
                or not str(row.get("Verbatim Snippet") or "").strip()
            )
        ]
        missing_ordinary_citations = [
            row
            for row in rows
            if str(row.get("Indicator ID") or "") not in {"P4-I4", "P4-I7", "P4-I8"}
            and (
                not str(row.get("Article / Section") or "").strip()
                or not str(row.get("Verbatim Snippet") or "").strip()
                or not str(row.get("Source URL") or "").strip()
            )
        ]
        bad_rationales = [
            row
            for row in rows
            if validate_submission_rationale(str(row.get("Mapping Rationale") or ""))
        ]
        details.update(
            {
                "submission_rows": len(rows),
                "invalid_indicators": invalid_indicators,
                "numeric_law_names": numeric_law_names,
                "invalid_treaty_status_rows": len(invalid_status_rows),
                "missing_ordinary_citations": len(missing_ordinary_citations),
                "bad_submission_rationales": len(bad_rationales),
            }
        )
        if invalid_indicators:
            failures.append("invalid_p4_indicator")
        if numeric_law_names:
            failures.append("numeric_law_name")
        if invalid_status_rows:
            failures.append("invalid_treaty_status_row")
        if missing_ordinary_citations:
            failures.append("missing_p4_citation")
        if bad_rationales:
            failures.append("submission_mapping_rationale_invalid")
        return {"failures": failures, "details": details}

    @staticmethod
    def _reviewer_schema_error_count(provision_results: list[ValidatedTaskResult]) -> int:
        count = 0
        for result in provision_results:
            text = " ".join(
                str(value or "")
                for value in (
                    getattr(result, "result_code", None),
                    getattr(result, "technical_detail", None),
                    " ".join(getattr(result, "failure_codes", []) or []),
                    getattr(result, "error", None),
                )
            )
            if "REVIEWER_SCHEMA_ERROR" in text or "reviewer_schema_error" in text:
                count += 1
        return count

    def _move_staging_to_failed(self, staging_dir: Path, name: str) -> Path:
        failed = self.mappings_dir / "failed_runs" / name
        if failed.exists():
            shutil.rmtree(failed)
        failed.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging_dir), str(failed))
        return failed

    def _publish_current(self, staging_dir: Path) -> None:
        current = self.mappings_dir / "current"
        previous = self.mappings_dir / "previous_current"
        if previous.exists():
            shutil.rmtree(previous)
        if current.exists():
            shutil.move(str(current), str(previous))
        shutil.move(str(staging_dir), str(current))
        submission_src = current / "submission"
        submission_dst = (
            self.root / "submission"
            if self.pillars == {6, 7}
            else self.root / "submission" / "p4"
        )
        if submission_src.exists():
            preserved_action_cache = None
            if submission_dst.exists():
                action_cache = submission_dst / "final_audit_actions.jsonl"
                if action_cache.exists():
                    preserved_action_cache = action_cache.read_bytes()
                shutil.rmtree(submission_dst)
            shutil.copytree(submission_src, submission_dst)
            stale_final_rows = submission_dst / "final_rows.jsonl"
            if stale_final_rows.exists():
                stale_final_rows.unlink()
            if preserved_action_cache is not None:
                (submission_dst / "final_audit_actions.jsonl").write_bytes(preserved_action_cache)
            shutil.rmtree(submission_src)
        legacy_submission = self.mappings_dir / "submission"
        if legacy_submission.exists():
            shutil.rmtree(legacy_submission)
        if previous.exists():
            shutil.rmtree(previous)
        self._sync_current_compatibility_files(current)

    def _sync_current_compatibility_files(self, current: Path) -> None:
        names = (
            "atomic_evidence.jsonl",
            "measures.jsonl",
            "review_queue.jsonl",
            "technical_repair_queue.jsonl",
            "external_status_review.jsonl",
            "indicator_summary.json",
            "mapping_summary.json",
            "run_validation_report.json",
            "cache_compatibility_report.json",
            "rebuild_before_after.json",
            "technical_repair_before_after.json",
            "treaty_download_report.json",
        )
        for name in names:
            src = current / name
            if src.exists():
                shutil.copy2(src, self.mappings_dir / name)

    def _reset_incremental_outputs(self) -> None:
        self.mappings_dir.mkdir(parents=True, exist_ok=True)
        for path in (
            self.task_results_path,
            self.routing_audit_path,
            self.mappings_dir / f"{self.output_prefix}_results.jsonl",
            self.mappings_dir / f"{self.output_prefix}_results.csv",
            self.mappings_dir / f"{self.output_prefix}_review_queue.jsonl",
            self.summary_path,
            self.mappings_dir / "mapping_before_after_diff.json",
            self.mappings_dir / "technical_repair_before_after.json",
        ):
            if path.exists():
                path.unlink()

    @staticmethod
    def _update_stats(stats: Counter, result) -> None:
        stats[result.status] += 1
        if result.llm_call:
            stats["llm_calls"] += 1 + result.retries
        if result.cache_hit:
            stats["cache_hits"] += 1
        if getattr(result, "reviewer_llm_call", False):
            stats["reviewer_llm_calls"] += 1
        if getattr(result, "reviewer_cache_hit", False):
            stats["reviewer_cache_hits"] += 1
        stats["retries"] += result.retries

    @staticmethod
    def _update_pdf_stats(stats: Counter, result: dict) -> None:
        stats["pdf_documents"] += 1
        stats["pdf_completed_direct_tasks"] += 1
        if result.get("prefilter_status") == "reject" or result.get("status") == "prefilter_rejected":
            stats["pdf_prefilter_rejected"] += 1
        if result.get("pdf_mapper_llm_call"):
            stats["pdf_mapper_calls"] += 1
        if result.get("pdf_mapper_cache_hit"):
            stats["pdf_mapper_cache_hits"] += 1
        if result.get("document_decision") == "no_match":
            stats["pdf_mapper_no_match"] += 1
        if result.get("pdf_documents_with_claims") or result.get("claims"):
            stats["pdf_documents_with_claims"] += 1
        if result.get("status") == "technical_repair":
            if result.get("pdf_mapper_llm_call") or result.get("pdf_mapper_cache_hit") or result.get("claims"):
                stats["pdf_technical_after_mapper"] += 1
            else:
                stats["pdf_technical_before_mapper"] += 1
        stats["fallback_model_calls"] += int(result.get("fallback_model_calls") or 0)
        stats["pdf_citation_verified"] += int(result.get("citation_verified") or 0)
        stats["pdf_citation_failed"] += int(result.get("citation_failed") or 0)
        stats["pdf_citation_unverifiable"] += int(result.get("citation_unverifiable") or 0)

    def _print_progress(self, result, total: int, completed: int, stats: Counter, started: float, workers: int) -> None:
        elapsed = max(0.1, time.time() - started)
        rate = completed / elapsed
        remaining = max(0, int((total - completed) / rate)) if rate else 0
        if isinstance(result, dict):
            if completed % 100 != 0 and completed != total and result.get("status") not in {"accepted", "human_legal_review", "model_review_pending"}:
                return
            print(
                f"Progress: {completed}/{total} | PDF completed: {stats['pdf_completed_direct_tasks']} | PDF calls: {stats['pdf_mapper_calls']} | PDF cache hits: {stats['pdf_mapper_cache_hits']} | "
                f"PDF prefilter rejected: {stats['pdf_prefilter_rejected']} | PDF no_match: {stats['pdf_mapper_no_match']} | PDF docs with claims: {stats['pdf_documents_with_claims']} | "
                f"PDF technical pre/post mapper: {stats['pdf_technical_before_mapper']}/{stats['pdf_technical_after_mapper']} | LLM calls: {stats['llm_calls']} | Cache hits: {stats['cache_hits']} | Retries: {stats['retries']} | Active workers: {workers} | "
                f"Elapsed: {int(elapsed)}s | ETA: {remaining}s",
                flush=True,
            )
            return
        if result.status == "accepted":
            print(f"[ACCEPT] {result.indicator} | {result.law_title} | {getattr(result, 'focal_provision_id', 'framework')}", flush=True)
        elif result.status == "model_review_pending":
            missing = result.result_code or "MODEL_REVIEW_PENDING"
            print(f"[PENDING] {result.indicator or '-'} | {result.law_title} | {getattr(result, 'focal_provision_id', 'framework')} | reason: {missing}", flush=True)
        elif result.status in {"review", "human_legal_review"}:
            missing = result.result_code or "LEGAL_UNCERTAINTY"
            print(f"[REVIEW] {result.indicator or '-'} | {result.law_title} | {getattr(result, 'focal_provision_id', 'framework')} | missing: {missing}", flush=True)
        if completed % 100 != 0 and completed != total and result.status not in {"accepted", "review", "human_legal_review", "model_review_pending"}:
            return
        print(
            f"Progress: {completed}/{total} | Accepted: {stats['accepted']} | Review: {stats['review'] + stats['human_legal_review']} | Rejected: {stats['rejected']} | Technical: {stats['error'] + stats['technical_repair']} | "
            f"LLM calls: {stats['llm_calls']} | Cache hits: {stats['cache_hits']} | Retries: {stats['retries']} | Active workers: {workers} | "
            f"Elapsed: {int(elapsed)}s | ETA: {remaining}s",
            flush=True,
        )

    def _write_progress_summary(
        self,
        provisions_loaded: int,
        routing_stats: dict,
        provision_total: int,
        completed_count: int,
        provision_results: list[ValidatedTaskResult],
        stats: Counter,
        started: float,
        *,
        pdf_tasks: list[PDFDocumentTask] | None = None,
        pdf_results: list[dict] | None = None,
    ) -> None:
        pdf_tasks = pdf_tasks or []
        pdf_results = pdf_results or []
        elapsed = time.time() - started
        total = provision_total + len(pdf_tasks)
        rate = completed_count / elapsed if elapsed and completed_count else 0
        remaining = (total - completed_count) / rate if rate else None
        payload = {
            "provisions_loaded": provisions_loaded,
            "provisions_scanned": int(routing_stats.get("provisions_scanned", provisions_loaded)),
            "candidate_tasks": provision_total,
            "documents_loaded": len({*(result.document_id for result in provision_results), *(task.document_id for task in pdf_tasks)}),
            "provision_documents": len({result.document_id for result in provision_results}),
            "pdf_documents": len(pdf_tasks),
            "pdf_prefilter_rejected": sum(1 for item in pdf_results if item.get("status") == "prefilter_rejected"),
            "pdf_documents_considered": sum(1 for item in pdf_results if item.get("status") != "prefilter_rejected"),
            "pdf_documents_sent_to_model": stats["pdf_mapper_calls"],
            "pdf_mapper_calls": stats["pdf_mapper_calls"],
            "pdf_mapper_cache_hits": stats["pdf_mapper_cache_hits"],
            "pdf_documents_no_match": sum(1 for item in pdf_results if item.get("pdf_documents_no_match")),
            "pdf_documents_with_claims": sum(1 for item in pdf_results if item.get("pdf_documents_with_claims")),
            "pdf_documents_failed": sum(1 for item in pdf_results if item.get("status") == "technical_repair"),
            "pdf_documents_pending_live_processing": sum(1 for item in pdf_results if item.get("status") == "pending_live_processing"),
            "mapper_calls": stats["llm_calls"],
            "mapper_cache_hits": stats["cache_hits"],
            "reviewer_calls": stats["reviewer_llm_calls"],
            "reviewer_cache_hits": stats["reviewer_cache_hits"],
            "network_calls": stats["llm_calls"] + stats["reviewer_llm_calls"] + stats["pdf_mapper_calls"],
            "accepted_tasks": sum(1 for result in provision_results if result.status == "accepted"),
            "accepted_measures": 0,
            "rejected_tasks": sum(1 for result in provision_results if result.status == "rejected"),
            "model_review_pending_tasks": sum(1 for result in provision_results if result.queue_type == "model_review_pending"),
            "human_legal_review_tasks": sum(1 for result in provision_results if result.queue_type == "human_legal_review"),
            "technical_repair_tasks": sum(1 for result in provision_results if result.queue_type == "technical_repair"),
            "external_source_tasks": 0,
            "external_status_review_tasks": 0,
            "framework_measures": sum(1 for result in provision_results if result.status == "accepted" and result.indicator in {"P7-I1", "P7-I2"}),
            "counts_by_indicator": self._counts_by_indicator(provision_results),
            "elapsed_seconds": elapsed,
            "models": {"mapper": mapper_model_name(), "reviewer": review_model_name(mapper_model_name()), "pdf_mapper": pdf_mapper_model_name()},
            "actual_models_used": {
                "mapper": mapper_model_name(),
                "reviewer": review_model_name(mapper_model_name()),
                "pdf_mapper": pdf_mapper_model_name(),
            },
            "fallback_model_calls": stats["fallback_model_calls"],
            "versions": {
                "indicator_spec": P4_INDICATOR_SPEC_VERSION if self.pillars == {4} else INDICATOR_SPEC_VERSION,
                "mapper_prompt": all_prompt_versions().get("p4" if self.pillars == {4} else "default", ""),
                "reviewer_prompt": all_prompt_versions().get("p4_reviewer", "") if self.pillars == {4} else REVIEWER_PROMPT_VERSION,
                "reviewer_by_group": all_prompt_versions().get("reviewer_by_group", {}),
                "pdf_prompt": P4_PDF_PROMPT_VERSION if self.pillars == {4} else PDF_PROMPT_VERSION,
                "pdf_schema": P4_PDF_OUTPUT_SCHEMA_VERSION if self.pillars == {4} else PDF_OUTPUT_SCHEMA_VERSION,
                "resolver": P4_VALIDATION_VERSION if self.pillars == {4} else RESOLVER_VERSION,
                "framework_pipeline": P4_AGGREGATION_VERSION if self.pillars == {4} else FRAMEWORK_PIPELINE_VERSION,
                "citation_validator": CITATION_VALIDATOR_VERSION,
                "output_schema": OUTPUT_SCHEMA_VERSION,
                "schema": MAPPING_SCHEMA_VERSION,
                **(
                    {
                        "p4_routing": P4_ROUTING_VERSION,
                        "p4_validation": P4_VALIDATION_VERSION,
                        "p4_mapper_cache_schema": P4_MAPPER_CACHE_SCHEMA_VERSION,
                    }
                    if self.pillars == {4}
                    else {}
                ),
            },
            "progress": {"completed": completed_count, "total": total, "estimated_remaining_seconds": remaining},
        }
        with self.summary_lock:
            write_json(self.summary_path, payload)

    def _export_final_outputs(
        self,
        provisions_loaded: int,
        routing_stats: dict,
        provision_tasks: list[CandidateTask],
        provision_results: list[ValidatedTaskResult],
        stats: Counter,
        started: float,
        before_snapshot: dict | None,
        *,
        pdf_tasks: list[PDFDocumentTask] | None = None,
        pdf_results: list[dict] | None = None,
    ) -> dict:
        pdf_tasks = pdf_tasks or []
        pdf_results = pdf_results or []
        provision_total = len(provision_tasks)
        provision_task_by_id = {task.task_id: task for task in provision_tasks}
        provision_by_id = {result.task_id: result for result in provision_results}
        external_rows = (
            load_p4_treaty_status(self.project_root, self.economy)
            if self.pillars == {4}
            else check_agreements(report_dir=self.output_dir, economy=self.economy)
        )
        if self.pillars == {4}:
            external_rows = self._apply_p4_status_human_decisions(external_rows)
        external_reviews = [dict(item, review_type="external_status") for item in external_rows if item.get("review_required")]
        external_accepted = [item for item in external_rows if not item.get("review_required")]
        treaty_tasks, treaty_results = (
            ([], []) if self.pillars == {4} else self._build_treaty_task_results(external_accepted)
        )
        for task in treaty_tasks:
            provision_task_by_id[task.task_id] = task
        final_provision = [*provision_by_id.values(), *treaty_results]
        provision_measures = aggregate_provision_measures(final_provision)
        framework_measures = []
        atomic_records = self._build_atomic_evidence(final_provision, provision_task_by_id, pdf_results=pdf_results)
        if self.pillars == {4}:
            atomic_records.extend(self._build_p4_status_evidence(external_accepted))
        atomic_records = _prefer_consolidated_principal_evidence(atomic_records)
        citation_failed = [record for record in atomic_records if record.citation_status != "verified"]
        submission_records = [record for record in atomic_records if record.decision == "accepted" and record.citation_status == "verified"]
        framework_indicator_set = (
            {"P4-I2", "P4-I5", "P4-I6", "P4-I10"}
            if self.pillars == {4}
            else {"P7-I1", "P7-I2"}
        )
        framework_submission_records = [
            record for record in submission_records if record.indicator_id in framework_indicator_set
        ]
        framework_conclusions = (
            aggregate_p4_framework_conclusions(final_provision)
            if self.pillars == {4}
            else []
        )
        accepted_review_identities = {
            (
                str(record.economy or ""),
                str(record.indicator_id or ""),
                str(record.document_id or ""),
                str(record.article or ""),
            )
            for record in submission_records
        }
        measure_rows = [_clean_json_metadata(item.model_dump()) for item in provision_measures + framework_measures]
        counts_by_indicator = Counter(record.indicator_id for record in submission_records)
        accepted_measures = len(submission_records)
        review_rows = [
            dict(item.model_dump(), review_type=item.queue_type or "human_legal_review")
            for item in final_provision
            if item.queue_type != "none" or item.status in {"review", "human_legal_review", "model_review_pending", "technical_repair"}
        ]
        review_rows += self._pdf_review_rows(pdf_results)
        review_rows += [dict(item, review_type="external_source", queue_type="external_source") for item in external_reviews]
        if self.pillars == {4}:
            for conclusion in framework_conclusions:
                if conclusion.get("framework_status") != "uncertain":
                    continue
                indicator = str(conclusion.get("indicator_id") or "")
                review_rows.append(
                    {
                        "task_id": f"framework-conclusion:{self.economy_slug}:{indicator}",
                        "economy": self.economy,
                        "document_id": "",
                        "law_title": "Framework conclusion",
                        "focal_provision_id": "Framework conclusion",
                        "indicator": indicator,
                        "status": "human_legal_review",
                        "queue_type": "human_legal_review",
                        "result_code": "LEGAL_UNCERTAINTY",
                        "uncertain_elements": list(conclusion.get("missing_elements") or []),
                        "uncertain_exclusions": [],
                        "focal_uncertainty": "P4 framework evidence is insufficient for a complete, partial, or absent conclusion.",
                        "validated_attributes": conclusion,
                        "rationale": "Framework conclusion remains uncertain because source evidence or coverage is insufficient.",
                        "source_url": "",
                        "review_type": "framework_conclusion",
                    }
                )
        for record in citation_failed:
            review_rows.append(
                {
                    "task_id": record.mapper_task_id,
                    "reviewer_task_id": record.reviewer_task_id,
                    "economy": record.economy,
                    "document_id": record.document_id,
                    "law_title": record.law_name,
                    "focal_provision_id": record.article,
                    "indicator": record.indicator_id,
                    "status": "technical_repair",
                    "queue_type": "technical_repair",
                    "result_code": "TECHNICAL_INPUT_ERROR",
                    "technical_detail": f"citation_{record.citation_status}: {record.citation_error}",
                    "affected_evidence_ids": [record.evidence_id],
                    "expected_repair_action": "repair_or_verify_verbatim_citation",
                    "review_type": "technical_repair",
                }
            )
        _assert_review_queue_diagnostics(review_rows)
        technical_rows = [row for row in review_rows if row.get("queue_type") == "technical_repair"]
        model_pending_rows = [row for row in review_rows if row.get("queue_type") == "model_review_pending"]
        human_review_rows = [row for row in review_rows if row.get("queue_type") == "human_legal_review"]
        filtered_human_review_rows = []
        for row in human_review_rows:
            identity = (
                str(row.get("economy") or self.economy),
                str(row.get("indicator") or ""),
                str(row.get("document_id") or ""),
                str(row.get("focal_provision_id") or ""),
            )
            if identity in accepted_review_identities:
                continue
            filtered_human_review_rows.append(row)
        human_review_rows = filtered_human_review_rows
        human_review_rows = self._annotate_human_review_rows(human_review_rows, provision_task_by_id)
        write_jsonl(self.output_dir / "atomic_evidence.jsonl", [_clean_json_metadata(item.model_dump()) for item in atomic_records])
        write_jsonl(self.output_dir / "measures.jsonl", measure_rows)
        write_jsonl(self.output_dir / "review_queue.jsonl", human_review_rows)
        write_jsonl(self.output_dir / "model_review_pending_queue.jsonl", model_pending_rows)
        write_jsonl(self.output_dir / "technical_repair_queue.jsonl", technical_rows)
        write_jsonl(self.output_dir / "external_status_review.jsonl", external_reviews)
        if self.pillars == {4}:
            write_json(self.output_dir / "framework_conclusions.json", {"conclusions": framework_conclusions})
            write_json(self.submission_dir / "framework_conclusions.json", {"conclusions": framework_conclusions})
        human_review_sync = sync_human_review_workbook(
            self.project_root,
            self.economy_slug,
            human_review_rows,
            provision_task_by_id,
            accepted_identities=accepted_review_identities,
            output_dir=self.submission_dir,
            scope=self.scope_slug,
        )
        self._write_human_review_submission_files(human_review_rows)
        indicator_summary = self._indicator_summary(submission_records, final_provision, external_rows)
        write_json(self.output_dir / "indicator_summary.json", indicator_summary)
        submission_export = export_submission(
            self.economy_slug,
            self.submission_dir,
            submission_records,
            output_prefix=self.output_prefix,
        )
        summary = {
            "provisions_loaded": provisions_loaded,
            "provisions_scanned": int(routing_stats.get("provisions_scanned", provisions_loaded)),
            "candidate_tasks": provision_total,
            "economy": self.economy,
            "documents_loaded": len({*(result.document_id for result in final_provision), *(task.document_id for task in pdf_tasks)}),
            "provision_documents": len({result.document_id for result in final_provision}),
            "pdf_documents": len(pdf_tasks),
            "pdf_prefilter_rejected": sum(1 for item in pdf_results if item.get("status") == "prefilter_rejected"),
            "pdf_documents_considered": sum(1 for item in pdf_results if item.get("status") != "prefilter_rejected"),
            "pdf_documents_sent_to_model": sum(1 for item in pdf_results if item.get("pdf_mapper_llm_call")),
            "pdf_mapper_calls": sum(1 for item in pdf_results if item.get("pdf_mapper_llm_call")),
            "pdf_mapper_cache_hits": sum(1 for item in pdf_results if item.get("pdf_mapper_cache_hit")),
            "pdf_documents_no_match": sum(1 for item in pdf_results if item.get("pdf_documents_no_match")),
            "pdf_documents_with_claims": sum(1 for item in pdf_results if item.get("pdf_documents_with_claims")),
            "pdf_documents_failed": sum(1 for item in pdf_results if item.get("status") == "technical_repair"),
            "pdf_documents_pending_live_processing": sum(1 for item in pdf_results if item.get("status") == "pending_live_processing"),
            "mapper_calls": sum((1 + item.retries) for item in final_provision if item.llm_call),
            "mapper_cache_hits": sum(1 for item in final_provision if item.cache_hit),
            "reviewer_calls": sum(1 for item in final_provision if getattr(item, "reviewer_llm_call", False)),
            "reviewer_cache_hits": sum(1 for item in final_provision if getattr(item, "reviewer_cache_hit", False)),
            "network_calls": (
                sum((1 + item.retries) for item in final_provision if item.llm_call)
                + sum(1 for item in final_provision if getattr(item, "reviewer_llm_call", False))
                + sum(1 for item in pdf_results if item.get("pdf_mapper_llm_call"))
            ),
            "accepted_tasks": sum(1 for item in final_provision if item.status == "accepted"),
            "accepted_measures": accepted_measures,
            "measure_records": len(measure_rows),
            "atomic_evidence_records": len(atomic_records),
            "submission_rows": len(submission_records),
            "citation_verified": sum(1 for item in atomic_records if item.citation_status == "verified"),
            "citation_failed": sum(1 for item in atomic_records if item.citation_status == "failed"),
            "citation_unverifiable": sum(1 for item in atomic_records if item.citation_status == "unverifiable"),
            "rejected_tasks": sum(1 for item in final_provision if item.status == "rejected"),
            "supporting_only_tasks": sum(1 for item in final_provision if item.status == "supporting_only"),
            "model_review_pending_tasks": sum(1 for item in final_provision if item.queue_type == "model_review_pending"),
            "human_legal_review_tasks": sum(1 for item in final_provision if item.queue_type == "human_legal_review"),
            "technical_repair_tasks": len(technical_rows),
            "human_review_workbook": human_review_sync.get("path"),
            "human_review_import": human_review_sync,
            "external_source_tasks": len(external_reviews),
            "external_status_review_tasks": len(external_reviews),
            "framework_measures": len(framework_measures) + len(framework_submission_records),
            "framework_conclusions": len(framework_conclusions),
            "counts_by_indicator": dict(counts_by_indicator),
            "submission": submission_export,
            "elapsed_seconds": time.time() - started,
            "models": {"mapper": mapper_model_name(), "reviewer": review_model_name(mapper_model_name()), "pdf_mapper": pdf_mapper_model_name()},
            "actual_models_used": {
                "mapper": mapper_model_name(),
                "reviewer": review_model_name(mapper_model_name()),
                "pdf_mapper": pdf_mapper_model_name(),
            },
            "fallback_model_calls": stats["fallback_model_calls"],
            "token_usage": {},
            "estimated_cost": {},
            "versions": {
                "indicator_spec": P4_INDICATOR_SPEC_VERSION if self.pillars == {4} else INDICATOR_SPEC_VERSION,
                "mapper_prompt": all_prompt_versions().get("p4" if self.pillars == {4} else "default", ""),
                "reviewer_prompt": all_prompt_versions().get("p4_reviewer", "") if self.pillars == {4} else REVIEWER_PROMPT_VERSION,
                "reviewer_by_group": all_prompt_versions().get("reviewer_by_group", {}),
                "pdf_prompt": P4_PDF_PROMPT_VERSION if self.pillars == {4} else PDF_PROMPT_VERSION,
                "pdf_schema": P4_PDF_OUTPUT_SCHEMA_VERSION if self.pillars == {4} else PDF_OUTPUT_SCHEMA_VERSION,
                "resolver": P4_VALIDATION_VERSION if self.pillars == {4} else RESOLVER_VERSION,
                "framework_pipeline": P4_AGGREGATION_VERSION if self.pillars == {4} else FRAMEWORK_PIPELINE_VERSION,
                "citation_validator": CITATION_VALIDATOR_VERSION,
                "output_schema": OUTPUT_SCHEMA_VERSION,
                "schema": MAPPING_SCHEMA_VERSION,
                **(
                    {
                        "p4_routing": P4_ROUTING_VERSION,
                        "p4_validation": P4_VALIDATION_VERSION,
                        "p4_mapper_cache_schema": P4_MAPPER_CACHE_SCHEMA_VERSION,
                    }
                    if self.pillars == {4}
                    else {}
                ),
            },
        }
        write_json(self.summary_path, summary)
        write_json(self.output_dir / "mapping_summary.json", summary)
        self._write_before_after_diff(before_snapshot, summary, [item.model_dump() for item in atomic_records], review_rows)
        self._write_technical_repair_diff(before_snapshot, final_provision)
        return summary

    def _annotate_human_review_rows(self, rows: list[dict], provision_task_by_id: dict[str, CandidateTask]) -> list[dict]:
        annotated: list[dict] = []
        for row in rows:
            item = dict(row)
            task = provision_task_by_id.get(str(item.get("task_id") or ""))
            if task:
                indicator = str(item.get("indicator") or task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else ""))
                item["review_key"] = review_key_for_task(task, indicator) if indicator else str(item.get("human_review_id") or item.get("task_id") or "")
                item["evidence_hash"] = source_fingerprint_for_task(task, indicator)
            else:
                item["review_key"] = str(item.get("review_key") or item.get("human_review_id") or item.get("claim_id") or item.get("task_id") or "")
                if not item.get("evidence_hash"):
                    basis = "|".join(
                        str(item.get(field) or "")
                        for field in (
                            "economy",
                            "document_id",
                            "indicator",
                            "claim_id",
                            "focal_provision_id",
                            "focal_quote",
                            "verbatim_snippet",
                            "technical_detail",
                        )
                    )
                    item["evidence_hash"] = hashlib.sha256(basis.encode("utf-8")).hexdigest() if basis else ""
            annotated.append(item)
        return annotated

    def _write_human_review_submission_files(self, rows: list[dict]) -> None:
        cleaned_rows = [_clean_json_metadata(row) for row in rows]
        write_jsonl(self.submission_dir / "human_review.jsonl", cleaned_rows)
        preferred = [
            "review_key",
            "evidence_hash",
            "economy",
            "indicator",
            "law_title",
            "focal_provision_id",
            "page_number",
            "focal_quote",
            "status",
            "queue_type",
            "result_code",
            "citation_status",
            "citation_error",
            "raw_mapper_decision",
            "raw_focal_role",
            "reviewer_decision",
            "reviewer_status",
            "authoritative_status",
            "authoritative_decision",
            "authoritative_focal_role",
            "authoritative_conflicts",
            "review_reasons",
            "rationale",
            "source_url",
            "document_id",
            "task_id",
        ]
        extra = sorted({key for row in cleaned_rows for key in row.keys()} - set(preferred))
        columns = [key for key in preferred if any(key in row for row in cleaned_rows)] + extra
        if not columns:
            columns = preferred[:2]
        path = self.submission_dir / "human_review.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in cleaned_rows:
                writer.writerow({key: _human_review_csv_cell(row.get(key)) for key in columns})

    def _build_atomic_evidence(
        self,
        provision_results: list[ValidatedTaskResult],
        provision_task_by_id: dict[str, CandidateTask],
        *,
        pdf_results: list[dict] | None = None,
    ) -> list[AtomicEvidenceRecord]:
        discovery_registry = self._discovery_registry()
        records: list[AtomicEvidenceRecord] = []
        seen_evidence_ids: set[str] = set()
        for result in provision_results:
            if result.status != "accepted":
                continue
            task = provision_task_by_id.get(result.task_id)
            if task is None:
                continue
            source_text = _provision_source_text(task)
            for match in result.accepted_matches:
                indicator = match.get("indicator")
                snippet = _atomic_snippet_for_result(task, match)
                provision_id = str(result.focal_provision_id or "").strip()
                document_meta = self._canonical_document_meta(result.document_id)
                if task.task_kind == "treaty_provision":
                    canonical_meta = {}
                    article = provision_id
                    location_reference = provision_id
                else:
                    canonical_meta = self._canonical_provision_meta(result.document_id, provision_id)
                    article = self._canonical_article_reference(
                        document_id=result.document_id,
                        provision_id=provision_id,
                        focal_quote=snippet,
                        law_title=result.law_title,
                    )
                    location_reference = self._canonical_location_reference(
                        document_id=result.document_id,
                        provision_id=provision_id,
                        focal_quote=snippet,
                    )
                snippet, validation = _verified_snippet_candidate(
                    snippet,
                    source_text=source_text,
                    article=provision_id,
                    expected_article=provision_id,
                    source_url=result.source_url,
                )
                citation_mode = "treaty_provision" if task.task_kind == "treaty_provision" else "structured_provision"
                source_locator = str(canonical_meta.get("source_locator") or canonical_meta.get("anchor_url") or canonical_meta.get("anchor") or canonical_meta.get("provision_id") or task.source_locator or "").strip()
                canonical_text = str(canonical_meta.get("text") or "")
                source_text_hash = str(canonical_meta.get("text_hash") or "")
                if canonical_text and not source_text_hash:
                    source_text_hash = hashlib.sha256(canonical_text.encode("utf-8", errors="ignore")).hexdigest()
                citation_error_parts = [validation.error] if validation.error else []
                citation_status = validation.status
                if citation_mode == "treaty_provision":
                    source_text_hash = task.focal_text_hash or hashlib.sha256(source_text.encode("utf-8", errors="ignore")).hexdigest()
                elif not canonical_meta:
                    citation_status = "failed"
                    citation_error_parts.append("canonical_provision_not_found")
                elif str(canonical_meta.get("document_id") or "") != result.document_id:
                    citation_status = "failed"
                    citation_error_parts.append("canonical_document_mismatch")
                elif task.source_locator and source_locator and task.source_locator != source_locator:
                    citation_status = "failed"
                    citation_error_parts.append("source_locator_mismatch")
                elif canonical_text and _norm_key(snippet) and _norm_key(snippet) not in _norm_key(canonical_text):
                    citation_status = "failed"
                    citation_error_parts.append("quote_not_in_canonical_provision")
                attrs = result.reviewer_attributes or _attributes_from_match(match)
                validated_attrs = result.human_validated_attributes if result.decision_source == "human_review" and result.human_validated_attributes else _validated_attributes_for_indicator(str(indicator or ""), attrs, task, match)
                if indicator == "P7-I5" and attrs.judicial_authorization in {None, "uncertain"}:
                    continue
                evidence_id = self._atomic_evidence_id(result.document_id, article, indicator, snippet)
                if evidence_id in seen_evidence_ids:
                    continue
                seen_evidence_ids.add(evidence_id)
                rationale = str(match.get("rationale") or result.rationale or "")
                if indicator in {"P7-I1", "P7-I2"} and result.reviewer_decision and result.reviewer_decision.review_reason:
                    rationale = result.reviewer_decision.review_reason
                discovery_match = discovery_registry.match(
                    economy=result.economy,
                    indicator_id=str(indicator or ""),
                    law_name=result.law_title,
                    law_number_ref=str(document_meta.get("official_number") or ""),
                    article=article,
                    verbatim_snippet=snippet,
                )
                records.append(
                    AtomicEvidenceRecord(
                        evidence_id=evidence_id,
                        economy=economy_profile(result.economy).name,
                        indicator_id=indicator,
                        document_id=result.document_id,
                        law_name=result.law_title,
                        law_number_ref=str(document_meta.get("official_number") or ""),
                        last_amended=str(document_meta.get("last_amended") or ""),
                        instrument_role=str(document_meta.get("instrument_role") or "unknown"),
                        principal_instrument_id=str(document_meta.get("principal_instrument_id") or ""),
                        amends_instrument_id=str(document_meta.get("amends_instrument_id") or ""),
                        consolidated_target=str(document_meta.get("consolidated_target") or ""),
                        article=article,
                        location_reference=location_reference,
                        focal_quote=snippet,
                        supporting_refs=_dedupe_preserve_order(task.supporting_provision_ids),
                        mapping_rationale=rationale,
                        source_url=result.source_url or "",
                        coverage=_coverage_for_title(result.law_title),
                        sector=_sector_for_title(result.law_title),
                        discovery_tag=discovery_match.discovery_tag,
                        baseline_match_key=discovery_match.baseline_match_key,
                        baseline_match_basis=discovery_match.baseline_match_basis,
                        baseline_row_id=discovery_match.baseline_row_id,
                        baseline_file_hash=discovery_match.baseline_file_hash,
                        confidence=1.0 if result.model_name == "human_verified" else (0.9 if not result.warnings else 0.7),
                        focal_role=result.focal_role or str(match.get("focal_role") or ""),
                        decision="accepted",
                        decision_reason=result.result_code or "",
                        mapper_task_id=result.task_id,
                        reviewer_task_id=result.reviewer_cache_key,
                        citation_status=citation_status,  # type: ignore[arg-type]
                        citation_error=";".join(part for part in citation_error_parts if part),
                        citation_mode=citation_mode,
                        canonical_provision_id=None if citation_mode == "treaty_provision" else str(canonical_meta.get("provision_id") or provision_id),
                        source_locator=source_locator,
                        canonical_schema_version=str(canonical_meta.get("canonical_schema_version") or canonical_meta.get("parser_version") or task.canonical_schema_version or ""),
                        source_text_hash=source_text_hash,
                        document_content_hash=str(document_meta.get("content_hash") or document_meta.get("sha256") or task.document_content_hash or ""),
                        citation_provenance=f"{citation_mode}:{result.document_id}:{str(canonical_meta.get('provision_id') or provision_id)}:{source_locator}",
                        notes=_notes_from_validated_attributes(validated_attrs),
                        validated_attributes=validated_attrs,
                        decision_source=result.decision_source,
                        human_review_id=result.human_review_id,
                        reviewed_by=result.reviewed_by,
                        reviewed_at=result.reviewed_at,
                    )
                )
        for result in pdf_results or []:
            if result.get("status") not in {"accepted", "human_legal_review", "technical_repair"}:
                continue
            for claim in result.get("claims") or []:
                if claim.get("status") != "accepted":
                    continue
                reviewed_result = claim.get("reviewed_result") if isinstance(claim.get("reviewed_result"), dict) else {}
                authoritative_status = str(claim.get("authoritative_status") or reviewed_result.get("status") or "")
                reviewer_decision_data = reviewed_result.get("reviewer_decision") if isinstance(reviewed_result.get("reviewer_decision"), dict) else {}
                authoritative_decision = str(claim.get("authoritative_decision") or reviewer_decision_data.get("decision") or "")
                authoritative_focal_role = str(claim.get("authoritative_focal_role") or reviewed_result.get("focal_role") or claim.get("focal_role") or "")
                if authoritative_status != "accepted":
                    continue
                if authoritative_decision not in {"match", "accepted"}:
                    continue
                if claim.get("authoritative_conflicts"):
                    continue
                if authoritative_focal_role == "supporting_only" or str(claim.get("focal_role") or "") == "supporting_only":
                    continue
                indicator_id = str(claim.get("indicator_id") or "")
                validated_attrs = claim.get("validated_attributes") if isinstance(claim.get("validated_attributes"), dict) else {}
                if not validated_attrs:
                    reviewer_attrs_data = claim.get("reviewer_attributes") if isinstance(claim.get("reviewer_attributes"), dict) else {}
                    try:
                        reviewer_attrs = ReviewerAttributes.model_validate(reviewer_attrs_data)
                    except Exception:
                        reviewer_attrs = ReviewerAttributes()
                    validated_attrs = _validated_attributes_for_indicator(indicator_id, reviewer_attrs, None, claim)
                evidence_id = self._atomic_evidence_id(result.get("document_id", ""), claim.get("article", ""), claim.get("indicator_id", ""), claim.get("verbatim_snippet", ""))
                if evidence_id in seen_evidence_ids:
                    continue
                seen_evidence_ids.add(evidence_id)
                discovery_match = discovery_registry.match(
                    economy=str(result.get("economy") or self.economy),
                    indicator_id=str(claim.get("indicator_id") or ""),
                    law_name=str(result.get("law_title") or ""),
                    law_number_ref=str(result.get("official_number") or ""),
                    article=str(claim.get("article") or ""),
                    verbatim_snippet=str(claim.get("verbatim_snippet") or ""),
                )
                records.append(
                    AtomicEvidenceRecord(
                        evidence_id=evidence_id,
                        economy=economy_profile(str(result.get("economy") or self.economy)).name,
                        indicator_id=str(claim.get("indicator_id")),
                        document_id=str(result.get("document_id") or ""),
                        law_name=str(result.get("law_title") or result.get("document_id") or ""),
                        law_number_ref=str(result.get("official_number") or ""),
                        article=str(claim.get("article") or ""),
                        location_reference=f"Page {claim.get('page_number')}",
                        focal_quote=str(claim.get("verbatim_snippet") or ""),
                        supporting_refs=[],
                        mapping_rationale=str(claim.get("authoritative_rationale") or claim.get("mapping_rationale") or ""),
                        source_url=str(result.get("source_url") or ""),
                        coverage=str(claim.get("coverage") or "uncertain"),
                        sector=str(claim.get("sector") or ""),
                        discovery_tag=discovery_match.discovery_tag,
                        baseline_match_key=discovery_match.baseline_match_key,
                        baseline_match_basis=discovery_match.baseline_match_basis,
                        baseline_row_id=discovery_match.baseline_row_id,
                        baseline_file_hash=discovery_match.baseline_file_hash,
                        confidence=max(0.0, min(1.0, float(claim.get("confidence") or 0.0))),
                        focal_role=authoritative_focal_role,
                        decision="accepted",
                        decision_reason=str(claim.get("result_code") or "PDF_DIRECT_MAPPING"),
                        mapper_task_id=str(result.get("task_id") or ""),
                        reviewer_task_id=str(claim.get("reviewer_cache_key") or ""),
                        citation_status=str(claim.get("citation_status") or "unverifiable"),
                        citation_error=str(claim.get("citation_error") or ""),
                        citation_mode="document_direct",
                        canonical_provision_id=None,
                        source_locator="",
                        canonical_schema_version=str(result.get("canonical_schema_version") or ""),
                        source_text_hash="",
                        document_content_hash=str(result.get("source_sha256") or result.get("document_content_hash") or ""),
                        citation_provenance=f"document_direct:{result.get('document_id')}:page-{claim.get('page_number')}:{claim.get('article')}",
                        page_number=int(claim.get("page_number") or 0) if str(claim.get("page_number") or "").isdigit() else None,
                        printed_article=str(claim.get("article") or ""),
                        notes=_notes_from_validated_attributes(validated_attrs),
                        validated_attributes=validated_attrs,
                    )
                )
        return records

    def _build_treaty_task_results(self, rows: list[dict]) -> tuple[list[CandidateTask], list[ValidatedTaskResult]]:
        tasks: list[CandidateTask] = []
        results: list[ValidatedTaskResult] = []
        for row in rows:
            snippet = str(row.get("verbatim_snippet") or "").strip()
            article = str(row.get("article_reference") or "").strip()
            agreement = str(row.get("agreement_name") or "Agreement").strip()
            source_url = str(row.get("official_source") or "").strip()
            if not snippet or not article or not source_url:
                continue
            document_id = _slug(agreement)
            task = CandidateTask(
                task_id=f"treaty:{self.economy_slug}:{document_id}:{_slug(article)}",
                economy=self.economy,
                indicator_id="P6-I5",
                task_kind="treaty_provision",
                source_type="treaty",
                document_id=document_id,
                source_record_id=f"{agreement}:{article}",
                law_title=agreement,
                instrument_type="treaty",
                parent_instrument=None,
                focal_provision_id=article,
                focal_quote=snippet,
                normalized_provision_id=_slug(article),
                section_heading=article,
                focal_text=snippet,
                supporting_provision_ids=[],
                parent_section_text=snippet,
                supporting_context=str(row.get("participation_status") or ""),
                route_topic="P6_TREATY",
                candidate_indicators=["P6-I5"],
                source_url=source_url,
                contract_version=INDICATOR_SPEC_VERSION,
                evidence_segments={"S1": snippet},
                recall_source="primary_router",
                matched_patterns=["treaty_registry:data_flow_commitment"],
                audit_confidence=None,
            )
            result = self._process_deterministic_evidence_task(task, "deterministic", "deterministic")
            tasks.append(task)
            results.append(result)
        return tasks, results

    def _build_p4_status_evidence(self, rows: list[dict]) -> list[AtomicEvidenceRecord]:
        records: list[AtomicEvidenceRecord] = []
        for row in rows:
            indicator = str(row.get("indicator_id") or "")
            instrument = str(row.get("instrument") or "").strip()
            status_text = str(row.get("status_text") or "").strip()
            source_url = str(row.get("official_source_url") or "").strip()
            if indicator not in {"P4-I4", "P4-I7", "P4-I8"} or not instrument or not status_text or not source_url:
                continue
            status = str(row.get("status") or "")
            effective_date = str(row.get("effective_date") or "")
            last_checked = str(row.get("last_checked") or "")
            decision_source = (
                "human_review"
                if row.get("decision_source") == "human_review"
                else "deterministic"
            )
            document_id = f"p4-status-{_slug(indicator)}-{self.economy_slug}"
            attributes = {
                "status": status,
                "instrument": instrument,
                "accession_or_ratification_date": str(row.get("accession_or_ratification_date") or ""),
                "effective_date": effective_date,
                "last_checked": last_checked,
                "registry_version": str(row.get("registry_version") or ""),
                "rdtii_score": row.get("rdtii_score"),
            }
            records.append(
                AtomicEvidenceRecord(
                    evidence_id=self._atomic_evidence_id(document_id, "Treaty status", indicator, status_text),
                    economy=self.economy,
                    indicator_id=indicator,  # type: ignore[arg-type]
                    document_id=document_id,
                    law_name=instrument,
                    law_number_ref="WIPO treaty status",
                    last_amended=last_checked,
                    instrument_role="external_status",
                    article="Treaty status",
                    location_reference=f"Effective date: {effective_date or 'not confirmed'}; last checked: {last_checked}",
                    focal_quote=status_text,
                    mapping_rationale=(
                        (
                            "A completed human review, supported by the recorded official WIPO source, "
                            f"records {self.economy} as {status.replace('_', ' ')} for {instrument}."
                        )
                        if decision_source == "human_review"
                        else (
                            f"The local audited WIPO status registry records {self.economy} as "
                            f"{status.replace('_', ' ')} for {instrument}."
                        )
                    ),
                    source_url=source_url,
                    coverage="International treaty status",
                    sector="Intellectual property",
                    discovery_tag="NEW",
                    confidence=1.0,
                    focal_role="operative",
                    decision="accepted",
                    decision_reason=(
                        "validated_human_treaty_status"
                        if decision_source == "human_review"
                        else "deterministic_local_treaty_registry"
                    ),
                    mapper_task_id=f"external-status:{self.economy_slug}:{indicator}",
                    citation_status="verified",
                    citation_mode="external_status",
                    citation_provenance=(
                        f"{decision_source}:{row.get('registry_version')}"
                    ),
                    notes=(
                        f"status={status}; effective_date={effective_date}; "
                        f"last_checked={last_checked}"
                    ),
                    validated_attributes=attributes,
                    decision_source=decision_source,
                    human_review_id=row.get("human_review_id"),
                    reviewed_by=row.get("reviewed_by"),
                    reviewed_at=row.get("reviewed_at"),
                )
            )
        return records

    def _apply_p4_status_human_decisions(self, rows: list[dict]) -> list[dict]:
        resolved: list[dict] = []
        for row in rows:
            if not row.get("review_required"):
                resolved.append(row)
                continue
            row = {
                **row,
                "review_key": str(
                    row.get("review_key")
                    or review_key_for_unbound_review(row)
                ),
            }
            review_key = str(row.get("review_key") or review_key_for_unbound_review(row))
            decision = self.human_decisions.get(review_key) if review_key else None
            if decision is None:
                resolved.append(row)
                continue
            if str(decision.get("human_decision") or "") != "accepted":
                resolved.append(
                    {
                        **row,
                        "external_source_reason": "human_decision_did_not_supply_accepted_status",
                    }
                )
                continue
            attrs = _parse_human_validated_attributes(decision)
            payload = {
                **row,
                **attrs,
                "economy": self.economy,
                "indicator_id": row.get("indicator_id"),
            }
            try:
                status = P4TreatyStatus.model_validate(
                    {
                        key: payload.get(key)
                        for key in P4TreatyStatus.model_fields
                    }
                )
            except Exception:
                resolved.append(
                    {
                        **row,
                        "external_source_reason": "human_status_payload_invalid",
                    }
                )
                continue
            error = validate_p4_treaty_status(status)
            if error:
                resolved.append(
                    {
                        **row,
                        "external_source_reason": f"human_status_payload_invalid:{error}",
                    }
                )
                continue
            accepted = p4_treaty_status_row(status, decision_source="human_review")
            accepted["registry_version"] = P4_TREATY_REGISTRY_VERSION
            accepted["human_review_id"] = decision.get("review_id")
            accepted["reviewed_by"] = decision.get("reviewer_name")
            accepted["reviewed_at"] = decision.get("reviewed_at")
            resolved.append(accepted)
        return resolved

    def _indicator_summary(
        self,
        submission_records: list[AtomicEvidenceRecord],
        provision_results: list[ValidatedTaskResult],
        external_rows: list[dict],
    ) -> dict:
        indicators = (
            tuple(f"P4-I{index}" for index in range(1, 11))
            if self.pillars == {4}
            else ("P6-I1", "P6-I2", "P6-I3", "P6-I4", "P6-I5", "P7-I1", "P7-I2", "P7-I3", "P7-I4", "P7-I5")
        )
        evidence_counts = Counter(record.indicator_id for record in submission_records)
        model_pending = Counter(str(item.indicator or "") for item in provision_results if item.queue_type == "model_review_pending")
        human = Counter(str(item.indicator or "") for item in provision_results if item.queue_type == "human_legal_review")
        technical = Counter(str(item.indicator or "") for item in provision_results if item.queue_type == "technical_repair")
        out = {}
        for indicator in indicators:
            if evidence_counts[indicator]:
                status = "evidence_found"
            elif indicator in {"P4-I4", "P4-I7", "P4-I8", "P6-I5"} and any(
                row.get("indicator_id") == indicator and not row.get("review_required")
                for row in external_rows
            ):
                status = "external_status_complete"
            elif model_pending[indicator]:
                status = "model_review_pending"
            elif human[indicator]:
                status = "human_review_required"
            elif technical[indicator]:
                status = "technical_repair_required"
            else:
                status = "no_relevant_measure_found"
            out[indicator] = {
                "status": status,
                "atomic_evidence_count": int(evidence_counts[indicator]),
                "model_review_pending_count": int(model_pending[indicator]),
                "human_review_count": int(human[indicator]),
                "technical_repair_count": int(technical[indicator]),
            }
        return out

    def _discovery_registry(self) -> KnownEvidenceRegistry:
        return load_legal_inventory_registry(self.project_root)

    @staticmethod
    def _atomic_evidence_id(document_id: str, article: str, indicator: str, snippet: str) -> str:
        digest = hashlib.sha256(f"{document_id}|{article}|{indicator}|{snippet}".encode("utf-8")).hexdigest()[:20]
        return f"{document_id}:{_slug(article)}:{indicator}:{digest}"

    def _read_stable_current_snapshot(self) -> dict:
        current = self.mappings_dir / "current"
        summary_path = current / "mapping_summary.json"
        if not summary_path.exists():
            summary_path = current / f"{self.output_prefix}_mapping_summary.json"
        if not summary_path.exists():
            return {
                "summary": {},
                "result_count": 0,
                "review_count": 0,
                "technical_repair": [],
                "counts_by_indicator": {},
            }
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            summary = {}
        rows = read_jsonl(current / "atomic_evidence.jsonl") or read_jsonl(current / f"{self.output_prefix}_results.jsonl")
        review_rows = read_jsonl(current / "review_queue.jsonl") + read_jsonl(current / "technical_repair_queue.jsonl")
        if not review_rows:
            review_rows = read_jsonl(current / f"{self.output_prefix}_review_queue.jsonl")
        technical_rows = [row for row in review_rows if row.get("queue_type") == "technical_repair"]
        return {
            "summary": summary,
            "result_count": len(rows),
            "review_count": len(review_rows),
            "technical_repair": technical_rows,
            "counts_by_indicator": summary.get("counts_by_indicator", {}),
        }

    def _write_before_after_diff(self, before: dict | None, summary: dict, result_rows: list[dict], review_rows: list[dict]) -> None:
        before_counts = (before or {}).get("counts_by_indicator", {})
        after_counts = summary.get("counts_by_indicator", {})
        indicators = sorted(set(before_counts) | set(after_counts))
        diff = {
            "generated_at": time.time(),
            "before": before or {},
            "after": {
                "result_count": len(result_rows),
                "review_count": len(review_rows),
                "summary": summary,
            },
            "counts_by_indicator_delta": {
                indicator: int(after_counts.get(indicator, 0)) - int(before_counts.get(indicator, 0))
                for indicator in indicators
            },
            "change_reasons": [
                "targeted Reviewer focal-role rules",
                "P7-I3 record_scope_basis enforcement",
                "P6-I2 focal local-storage proof enforcement",
                "measure-level adjacent subsection de-duplication",
                "P6-I5 treaty registry external-status source",
            ],
            "remaining_review_items": {
                "human_legal_review": sum(1 for row in review_rows if row.get("queue_type") == "human_legal_review"),
                "technical_repair": sum(1 for row in review_rows if row.get("queue_type") == "technical_repair"),
                "external_source": sum(1 for row in review_rows if row.get("queue_type") == "external_source"),
            },
        }
        write_json(self.output_dir / "rebuild_before_after.json", diff)

    def _write_technical_repair_diff(self, before: dict | None, final_results: list) -> None:
        before_rows = (before or {}).get("technical_repair", [])
        by_task = {getattr(item, "task_id", None): item for item in final_results}
        rows = []
        for before_row in before_rows:
            task_id = before_row.get("task_id")
            after = by_task.get(task_id)
            rows.append(
                {
                    "task_id": task_id,
                    "law_title": before_row.get("law_title"),
                    "focal_provision_id": before_row.get("focal_provision_id"),
                    "route_topic": before_row.get("route_topic") or before_row.get("framework_topic"),
                    "original_status": before_row.get("status"),
                    "original_result_code": before_row.get("result_code"),
                    "original_technical_detail": before_row.get("technical_detail"),
                    "fix_applied": "reviewer_evidence_catalog_schema_v2" if after is not None else "not_reprocessed",
                    "final_status": getattr(after, "status", None),
                    "final_queue_type": getattr(after, "queue_type", None),
                    "final_result_code": getattr(after, "result_code", None),
                    "final_indicator": getattr(after, "indicator", None),
                    "final_failed_required_elements": getattr(after, "failed_required_elements", []),
                    "final_triggered_exclusions": getattr(after, "triggered_exclusions", []),
                    "final_uncertain_elements": getattr(after, "uncertain_elements", []),
                    "final_uncertain_exclusions": getattr(after, "uncertain_exclusions", []),
                    "final_technical_detail": getattr(after, "technical_detail", None),
                }
            )
        payload = {
            "generated_at": time.time(),
            "technical_repair_before": len(before_rows),
            "technical_repair_after": sum(1 for item in final_results if getattr(item, "queue_type", None) == "technical_repair"),
            "items": rows,
        }
        write_json(self.output_dir / "technical_repair_before_after.json", payload)

    @staticmethod
    def _write_csv(path: Path, measures: list, external_rows: list[dict] | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "economy",
                    "indicator_id",
                    "act_and_or_practice",
                    "coverage",
                    "section_reference",
                    "verbatim_snippet",
                    "mapping_rationale",
                    "timeframe",
                    "source_url",
                    "confidence",
                    "review_flag",
                ],
            )
            writer.writeheader()
            for measure in measures:
                writer.writerow(
                    {
                        "economy": _clean_metadata(measure.economy, "economy"),
                        "indicator_id": _clean_metadata(measure.indicator_id, "indicator_id"),
                        "act_and_or_practice": _clean_metadata(measure.official_title, "act_and_or_practice"),
                        "coverage": _clean_metadata(measure.coverage, "coverage"),
                        "section_reference": "; ".join(measure.section_references),
                        "verbatim_snippet": "; ".join(measure.verbatim_snippets),
                        "mapping_rationale": _clean_metadata(measure.mapping_rationale, "mapping_rationale"),
                        "timeframe": "current",
                        "source_url": _clean_metadata(measure.source_url or "", "source_url"),
                        "confidence": _clean_metadata(measure.confidence, "confidence"),
                        "review_flag": "yes" if measure.review_required else "no",
                    }
                )
            for row in external_rows or []:
                writer.writerow(
                    {
                        "economy": _clean_metadata(row.get("economy", ""), "economy"),
                        "indicator_id": _clean_metadata(row.get("indicator_id", ""), "indicator_id"),
                        "act_and_or_practice": _clean_metadata(row.get("agreement_name", ""), "act_and_or_practice"),
                        "coverage": "Horizontal",
                        "section_reference": _clean_metadata(row.get("article_reference", ""), "section_reference"),
                        "verbatim_snippet": row.get("verbatim_snippet", ""),
                        "mapping_rationale": _clean_metadata(row.get("participation_status", ""), "mapping_rationale"),
                        "timeframe": _clean_metadata(row.get("effective_date") or "current", "timeframe"),
                        "source_url": _clean_metadata(row.get("official_source") or "", "source_url"),
                        "confidence": "high" if row.get("binding_commitment_present") else "review",
                        "review_flag": "yes" if row.get("review_required") else "no",
                    }
                )

    @staticmethod
    def _counts_by_indicator(provision_results: list[ValidatedTaskResult]) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for result in provision_results:
            if result.status == "accepted":
                for match in result.accepted_matches:
                    if match.get("indicator"):
                        counts[match["indicator"]] += 1
        return dict(counts)

    @staticmethod
    def _counts_from_result_rows(rows: list[dict]) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for row in rows:
            indicator = row.get("indicator_id")
            if indicator:
                counts[str(indicator)] += 1
        return dict(counts)

    @staticmethod
    def _pdf_review_rows(pdf_results: list[dict]) -> list[dict]:
        rows: list[dict] = []
        for result in pdf_results:
            for claim in result.get("claims") or []:
                if claim.get("status") == "technical_repair":
                    rows.append(
                        {
                            "task_id": result.get("task_id"),
                            "claim_id": claim.get("claim_id"),
                            "economy": result.get("economy"),
                            "document_id": result.get("document_id"),
                            "law_title": result.get("law_title"),
                            "focal_provision_id": claim.get("article"),
                            "page_number": claim.get("page_number"),
                            "focal_quote": claim.get("verbatim_snippet"),
                            "verbatim_snippet": claim.get("verbatim_snippet"),
                            "source_url": result.get("source_url"),
                            "indicator": claim.get("indicator_id"),
                            "status": "technical_repair",
                            "queue_type": "technical_repair",
                            "result_code": "TECHNICAL_INPUT_ERROR",
                            "technical_detail": claim.get("technical_detail") or claim.get("citation_error") or "pdf_claim_processing_failed",
                            "affected_evidence_ids": [claim.get("claim_id")],
                            "expected_repair_action": "repair_pdf_claim_or_verify_page_citation",
                            "citation_status": claim.get("citation_status"),
                            "citation_error": claim.get("citation_error"),
                            "raw_mapper_decision": claim.get("status"),
                            "raw_focal_role": claim.get("focal_role"),
                            "authoritative_status": claim.get("authoritative_status"),
                            "authoritative_decision": claim.get("authoritative_decision"),
                            "authoritative_focal_role": claim.get("authoritative_focal_role"),
                            "authoritative_conflicts": claim.get("authoritative_conflicts") or [],
                            "review_type": "technical_repair",
                        }
                    )
                elif claim.get("status") == "human_legal_review":
                    reason = claim.get("focal_uncertainty") or claim.get("result_code") or "pdf_claim_requires_legal_review"
                    rows.append(
                        {
                            "task_id": result.get("task_id"),
                            "claim_id": claim.get("claim_id"),
                            "economy": result.get("economy"),
                            "document_id": result.get("document_id"),
                            "law_title": result.get("law_title"),
                            "focal_provision_id": claim.get("article"),
                            "page_number": claim.get("page_number"),
                            "focal_quote": claim.get("verbatim_snippet"),
                            "verbatim_snippet": claim.get("verbatim_snippet"),
                            "source_url": result.get("source_url"),
                            "indicator": claim.get("indicator_id"),
                            "status": "human_legal_review",
                            "queue_type": "human_legal_review",
                            "result_code": claim.get("result_code") or "LEGAL_UNCERTAINTY",
                            "uncertain_elements": claim.get("uncertain_elements") or ["PDF_CLAIM_UNCERTAIN"],
                            "uncertain_exclusions": claim.get("uncertain_exclusions") or [],
                            "focal_uncertainty": reason,
                            "citation_status": claim.get("citation_status"),
                            "citation_error": claim.get("citation_error"),
                            "raw_mapper_decision": claim.get("status"),
                            "raw_focal_role": claim.get("focal_role"),
                            "reviewer_decision": (claim.get("reviewed_result") or {}).get("decision") if isinstance(claim.get("reviewed_result"), dict) else "",
                            "reviewer_status": (claim.get("reviewed_result") or {}).get("status") if isinstance(claim.get("reviewed_result"), dict) else "",
                            "authoritative_status": claim.get("authoritative_status"),
                            "authoritative_decision": claim.get("authoritative_decision"),
                            "authoritative_focal_role": claim.get("authoritative_focal_role"),
                            "authoritative_conflicts": claim.get("authoritative_conflicts") or [],
                            "review_type": "human_legal_review",
                        }
                    )
        return rows

    @staticmethod
    def _error_result(task, model: str, error: str, kind: str):
        prompt_version = prompt_version_for_task(task)
        return ValidatedTaskResult(
            task_id=task.task_id,
            economy=task.economy,
            document_id=task.document_id,
            law_title=task.law_title,
            instrument_type=task.instrument_type,
            source_url=task.source_url,
            focal_provision_id=task.focal_provision_id,
            route_topic=task.route_topic,
            candidate_indicators=task.candidate_indicators,
            status="technical_repair",
            queue_type="technical_repair",
            result_code="TECHNICAL_INPUT_ERROR",
            indicator=None,
            decision=None,
            failure_codes=["TECHNICAL_INPUT_ERROR"],
            review_reasons=[],
            rationale="Task processing failed.",
            prompt_version=prompt_version,
            validation_version=P4_VALIDATION_VERSION if task.route_topic.startswith("P4_") else VALIDATION_VERSION,
            model_name=model,
            cache_key=cache_key(task, model, prompt_version),
            llm_call=False,
            cache_hit=False,
            retries=0,
            technical_detail=error,
            affected_evidence_ids=[],
            expected_repair_action="inspect_pipeline_task_or_source",
            error=error,
            warnings=[],
        )

    @staticmethod
    def _pdf_error_result(task: PDFDocumentTask, error: str) -> dict:
        return {
            "task_id": task.task_id,
            "task_type": "pdf_document",
            "economy": task.economy,
            "document_id": task.document_id,
            "law_title": task.title,
            "collection": task.collection,
            "source_url": task.source_url,
            "raw_path": task.raw_path,
            "source_sha256": task.source_sha256,
            "prefilter_status": task.prefilter_status,
            "status": "technical_repair",
            "technical_detail": error or "pdf_document_processing_failed",
            "model_name": pdf_mapper_model_name(),
            "pdf_mapper_llm_call": False,
            "pdf_mapper_cache_hit": False,
            "claims": [],
        }


def _deterministic_mapping_decision(task: CandidateTask, indicator: str) -> MappingDecision:
    elements = [RequiredElementReview(element_code=code, status="present", evidence_ids=["S1"]) for code in _review_required_elements(task)]
    return MappingDecision(
        decision="match",
        matches=[
            IndicatorMatch(
                indicator=indicator,  # type: ignore[arg-type]
                legal_function="operative_rule",
                actor=_deterministic_actor(task),
                modality="deterministic",
                action=_deterministic_action(task),
                regulated_object=_deterministic_object(task),
                object_type="information",
                geographic_nexus="",
                duration=_deterministic_duration(task),
                conditions=[],
                operative_evidence_ids=["S1"],
                supporting_evidence_ids=[key for key in task.evidence_segments if key != "S1"],
                evidence_ids=["S1"],
                required_element_status=elements,
                triggered_exclusions=[],
                why_included=_deterministic_rationale(task, indicator),
                adjacent_indicator_analysis=[],
            )
        ],
        rationale=_deterministic_rationale(task, indicator),
    )


def _human_mapping_decision(task: CandidateTask, indicator: str, quote: str, human_decision: str, decision: dict) -> MappingDecision:
    if human_decision == "rejected":
        return MappingDecision(decision="no_match", matches=[], rationale=str(decision.get("human_rationale") or "Human reviewer rejected the candidate."))
    original = _mapping_decision_from_human_record(decision, indicator)
    if original is not None:
        return original
    return MappingDecision(
        decision="match" if human_decision in {"accepted", "supporting_only"} else "uncertain",
        matches=[
            IndicatorMatch(
                indicator=indicator,
                legal_function="operative_rule",
                actor=task.law_title,
                modality="human_review",
                action="human reviewed legal conclusion",
                regulated_object=quote,
                object_type=None,
                geographic_nexus=None,
                duration=None,
                conditions=[],
                operative_evidence_ids=["S1"],
                supporting_evidence_ids=[key for key in task.evidence_segments if key != "S1"],
                evidence_ids=["S1"],
                required_element_status=[
                    RequiredElementReview(element_code=code, status="present", evidence_ids=["S1"])
                    for code in _review_required_elements(task)
                ],
                triggered_exclusions=[],
                why_included=str(decision.get("human_rationale") or "Human reviewer accepted the candidate."),
                adjacent_indicator_analysis=[],
            )
        ],
        rationale=str(decision.get("human_rationale") or "Human reviewed decision."),
    )


def _mapping_decision_from_human_record(decision: dict, indicator: str) -> MappingDecision | None:
    raw = decision.get("original_mapper_decision")
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    if not isinstance(raw, dict) or not raw.get("matches"):
        return None
    payload = dict(raw)
    payload["decision"] = "match"
    payload["rationale"] = str(decision.get("human_rationale") or payload.get("rationale") or "Human reviewed decision.")
    matches = []
    for item in payload.get("matches") or []:
        if not isinstance(item, dict):
            continue
        updated = dict(item)
        updated["indicator"] = indicator
        updated["why_included"] = str(decision.get("human_rationale") or updated.get("why_included") or "Human reviewed decision.")
        matches.append(updated)
    payload["matches"] = matches[:1]
    try:
        return MappingDecision.model_validate(payload)
    except Exception:
        return None


def _human_reviewer_decision(task: CandidateTask, indicator: str, human_decision: str, attrs: dict, decision: dict) -> ReviewerDecision:
    required = _review_required_elements(task)
    exclusions = _review_allowed_exclusions(task)
    element_status = "supported" if human_decision == "accepted" else "not_supported"
    focal_role = "supporting_only" if human_decision == "supporting_only" else "operative"
    if human_decision == "accepted":
        review_decision = "match"
    elif human_decision == "supporting_only":
        review_decision = "supporting_only"
    else:
        review_decision = "no_match"
    reviewer_attrs = _reviewer_attributes_from_human_attrs(indicator, attrs)
    optional_checks = [
        ReviewerOptionalCheck(
            check_code="HUMAN_REVIEW",
            status="applied",
            evidence_ids=["S1"],
            reason="Exact-match human review decision",
        )
    ]
    if indicator in {"P4-I2", "P4-I5", "P4-I6", "P4-I10"}:
        framework_element = str(
            attrs.get("framework_element")
            or attrs.get("candidate_element")
            or attrs.get("framework_candidate_element")
            or _framework_candidate_element_name(task)
            or ""
        )
        optional_checks.append(
            ReviewerOptionalCheck(
                check_code="P4_FRAMEWORK_ELEMENT",
                status="captured",
                evidence_ids=["S1"],
                reason=json.dumps(
                    {
                        "candidate_element": framework_element,
                        "framework_element": framework_element,
                    },
                    sort_keys=True,
                ),
            )
        )
    return ReviewerDecision(
        focal_integrity=FocalIntegrityAssessment(status="ok", reason="human reviewer exact-match decision"),
        focal_role=focal_role,
        elements=[
            ReviewerElementAssessment(
                element_id=code,
                status=element_status,
                evidence_ids=["S1"] if element_status == "supported" else [],
                reason=str(decision.get("human_rationale") or "Human reviewed decision."),
            )
            for code in required
        ],
        exclusions=[
            ReviewerExclusionAssessment(exclusion_id=code, status="not_triggered", evidence_ids=[], reason="Human reviewer did not trigger this exclusion.")
            for code in exclusions
        ],
        attributes=reviewer_attrs,
        optional_checks=optional_checks,
        decision=review_decision,
        review_reason=str(decision.get("human_rationale") or "Human reviewed decision."),
    )


def _parse_human_validated_attributes(decision: dict) -> dict:
    corrected = str(decision.get("corrected_validated_attributes") or "").strip()
    if corrected:
        try:
            value = json.loads(corrected)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    original = decision.get("original_validated_attributes")
    if isinstance(original, dict):
        return original
    if isinstance(original, str) and original.strip():
        try:
            value = json.loads(original)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    return {}


def _reviewer_attributes_from_human_attrs(indicator: str, attrs: dict) -> ReviewerAttributes:
    if indicator == "P7-I3":
        periods = attrs.get("retention_periods") if isinstance(attrs.get("retention_periods"), list) else []
        first = periods[0] if periods and isinstance(periods[0], dict) else {}
        return ReviewerAttributes(
            record_scope_basis=attrs.get("record_scope_basis") or "UNCERTAIN",
            minimum_duration_value=str(first.get("value") or "") or None,
            minimum_duration_unit=str(first.get("unit") or "") or None,
            trigger_event=str(first.get("trigger_event") or attrs.get("trigger_event") or "") or None,
        )
    if indicator == "P7-I4":
        return ReviewerAttributes(accountability_path=attrs.get("accountability_path") or "uncertain")
    if indicator == "P7-I5":
        return ReviewerAttributes(judicial_authorization=attrs.get("judicial_authorization") or "uncertain")
    if indicator in {"P4-I2", "P4-I5", "P4-I6", "P4-I10"}:
        return ReviewerAttributes(
            framework_legal_function=attrs.get("legal_function") or attrs.get("framework_legal_function"),
            coverage=attrs.get("coverage"),
        )
    if indicator in {"P7-I1", "P7-I2"}:
        return ReviewerAttributes(
            framework_candidate_element=attrs.get("candidate_element") or attrs.get("framework_candidate_element") or attrs.get("framework_element"),
            framework_element=attrs.get("framework_element"),
            framework_legal_function=attrs.get("legal_function") or attrs.get("framework_legal_function"),
            coverage=attrs.get("coverage"),
        )
    return ReviewerAttributes(coverage=attrs.get("coverage"), sector=attrs.get("sector"))


def _reviewer_attrs_from_pdf_claim_attributes(attrs) -> ReviewerAttributes:
    if attrs is None:
        return ReviewerAttributes()
    data = attrs.model_dump() if hasattr(attrs, "model_dump") else dict(attrs or {})
    try:
        return ReviewerAttributes.model_validate(data)
    except Exception:
        return ReviewerAttributes(
            coverage=data.get("coverage"),
            sector=data.get("sector"),
            framework_candidate_element=_valid_reviewer_attribute("framework_candidate_element", data.get("framework_candidate_element") or data.get("framework_element")),
            framework_element=_valid_reviewer_attribute("framework_element", data.get("framework_element")),
            framework_legal_function=_valid_reviewer_attribute("framework_legal_function", data.get("framework_legal_function") or data.get("legal_function")),
            record_scope_basis=_valid_reviewer_attribute("record_scope_basis", data.get("record_scope_basis")),
            judicial_authorization=_valid_reviewer_attribute("judicial_authorization", data.get("judicial_authorization")),
            accountability_path=_valid_reviewer_attribute("accountability_path", data.get("accountability_path")),
            minimum_duration_value=data.get("minimum_duration_value"),
            minimum_duration_unit=data.get("minimum_duration_unit"),
            trigger_event=data.get("trigger_event"),
        )


def _valid_reviewer_attribute(field: str, value):
    if value is None or value == "":
        return None
    try:
        return getattr(ReviewerAttributes.model_validate({field: value}), field)
    except Exception:
        return None


def _deterministic_typed_review(task: CandidateTask, match: IndicatorMatch) -> ReviewerDecision:
    required = _review_required_elements(task, match)
    exclusions = _review_allowed_exclusions(task)
    element_rows = [
        ReviewerElementAssessment(
            element_id=code,
            status="supported",
            evidence_ids=["S1"],
            reason=_deterministic_element_reason(task, code),
        )
        for code in required
    ]
    exclusion_rows = [
        ReviewerExclusionAssessment(exclusion_id=code, status="not_triggered", evidence_ids=[], reason="No hard exclusion is triggered by the deterministic source adapter.")
        for code in exclusions
    ]
    attrs = _deterministic_attributes(task, match)
    return ReviewerDecision(
        focal_integrity=FocalIntegrityAssessment(status="ok", reason="deterministic source adapter used the focal provision text verbatim"),
        focal_role="operative",
        elements=element_rows,
        exclusions=exclusion_rows,
        attributes=attrs,
        optional_checks=[],
        decision="match",
        review_reason=_deterministic_rationale(task, str(match.indicator or "")),
    )


def _deterministic_attributes(task: CandidateTask, match: IndicatorMatch) -> ReviewerAttributes:
    indicator = str(match.indicator or task.indicator_id or "")
    if indicator == "P6-I5":
        return ReviewerAttributes(coverage="Horizontal", sector="International agreement")
    return ReviewerAttributes()


def _deterministic_rationale(task: CandidateTask, indicator: str) -> str:
    if task.task_kind == "treaty_provision":
        return "Deterministic treaty evidence from local versioned treaty registry and normalized treaty text."
    return "Deterministic evidence task."


def _deterministic_actor(task: CandidateTask) -> str:
    if task.task_kind == "treaty_provision":
        return task.economy
    return task.law_title


def _deterministic_action(task: CandidateTask) -> str:
    if task.task_kind == "treaty_provision":
        return "binding treaty data-flow commitment"
    return "deterministic evidence"


def _deterministic_object(task: CandidateTask) -> str:
    if task.task_kind == "treaty_provision":
        return "cross-border data flow commitment"
    return "source text"


def _deterministic_duration(task: CandidateTask) -> str | None:
    return None


def _deterministic_element_reason(task: CandidateTask, code: str) -> str:
    return f"Supported by focal source text S1 for task_kind={task.task_kind}; code={code}."


def _deterministic_task_cache_key(task: CandidateTask) -> str:
    return hashlib.sha256(f"deterministic_mapper|{task.task_id}|{task.contract_version}|{task.focal_text}".encode("utf-8")).hexdigest()


def _deterministic_reviewer_cache_key(task: CandidateTask) -> str:
    return hashlib.sha256(f"deterministic_reviewer|{task.task_id}|{task.contract_version}|{task.focal_text}".encode("utf-8")).hexdigest()


def cache_key(task: CandidateTask, model: str, prompt_version: str) -> str:
    context_hash = hashlib.sha256(f"{task.focal_text}\n{task.supporting_context}".encode("utf-8")).hexdigest()
    p4_scope = task.route_topic.startswith("P4_")
    indicator_spec_version = P4_INDICATOR_SPEC_VERSION if p4_scope else INDICATOR_SPEC_VERSION
    cache_schema_version = P4_MAPPER_CACHE_SCHEMA_VERSION if p4_scope else MAPPER_CACHE_SCHEMA_VERSION
    return hashlib.sha256(
        "|".join([
            "provision_mapper",
            task.economy,
            task.document_id,
            task.focal_provision_id,
            task.route_topic,
            ",".join(task.candidate_indicators),
            context_hash,
            prompt_version,
            indicator_spec_version,
            cache_schema_version,
            "mapping_mode=provision",
            model,
        ]).encode("utf-8")
    ).hexdigest()


def _legacy_routing_cache_key(task: CandidateTask, model: str, prompt_version: str, routing_version: str) -> str:
    """Compatibility with cache rows created by the prior abnormal run.

    That version included ROUTING_VERSION in the LLM cache key, which made
    deterministic routing changes capable of invalidating paid Mapper results.
    """

    context_hash = hashlib.sha256(f"{task.focal_text}\n{task.supporting_context}".encode("utf-8")).hexdigest()
    return hashlib.sha256(
        "|".join([
            "provision_mapper",
            task.economy,
            task.document_id,
            task.focal_provision_id,
            task.route_topic,
            ",".join(task.candidate_indicators),
            context_hash,
            prompt_version,
            INDICATOR_SPEC_VERSION,
            MAPPER_CACHE_SCHEMA_VERSION,
            routing_version,
            "mapping_mode=provision",
            model,
        ]).encode("utf-8")
    ).hexdigest()


def mapper_cache_lookup_keys(task: CandidateTask, model: str, prompt_version: str) -> tuple[str, ...]:
    keys = [cache_key(task, model, prompt_version)]
    for version in LEGACY_ROUTING_CACHE_VERSIONS:
        key = _legacy_routing_cache_key(task, model, prompt_version, version)
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def pdf_mapper_cache_key(task: PDFDocumentTask, model: str, page_range: str = "whole") -> str:
    p4_scope = any(indicator.startswith("P4-") for indicator in task.candidate_indicators)
    malaysia_override_scope = (
        "malaysia_p4_source_override"
        if p4_scope
        and task.economy.casefold() == "malaysia"
        and task.official_number in {"Act 291", "Act 332"}
        else ""
    )
    key_parts = [
        task.document_text_hash or task.source_sha256,
        task.document_id,
        task.economy,
        "document_direct",
        page_range,
        ",".join(task.candidate_indicators),
        model,
        P4_PDF_PROMPT_VERSION if p4_scope else PDF_PROMPT_VERSION,
        P4_INDICATOR_SPEC_VERSION if p4_scope else INDICATOR_SPEC_VERSION,
        P4_PDF_OUTPUT_SCHEMA_VERSION if p4_scope else PDF_OUTPUT_SCHEMA_VERSION,
    ]
    if malaysia_override_scope:
        key_parts.extend([malaysia_override_scope, task.title, task.official_number])
    return hashlib.sha256(
        "|".join(key_parts).encode("utf-8")
    ).hexdigest()


def pdf_citation_cache_key(task: PDFDocumentTask, claim: PDFEvidenceClaim, mode: str) -> str:
    return hashlib.sha256(
        "|".join(
            [
                task.document_text_hash or task.source_sha256,
                str(claim.page_number),
                normalized_snippet_hash(claim.verbatim_snippet),
                claim.article,
                mode,
                PDF_CITATION_PROMPT_VERSION,
                "docling_page",
            ]
        ).encode("utf-8")
    ).hexdigest()


def _route_topic_for_indicator(indicator: str) -> str:
    p4_topics = {
        "P4-I1": "P4_PATENT_APPLICATION",
        "P4-I2": "P4_PATENT_ENFORCEMENT",
        "P4-I3": "P4_PATENT_ENFORCEMENT",
        "P4-I4": "P4_TREATY_STATUS",
        "P4-I5": "P4_COPYRIGHT_FRAMEWORK",
        "P4-I6": "P4_ONLINE_COPYRIGHT",
        "P4-I7": "P4_TREATY_STATUS",
        "P4-I8": "P4_TREATY_STATUS",
        "P4-I9": "P4_DISCLOSURE",
        "P4-I10": "P4_TRADE_SECRET_FRAMEWORK",
    }
    if indicator in p4_topics:
        return p4_topics[indicator]
    if indicator.startswith("P6-I"):
        return "P6_LOCATION"
    if indicator == "P7-I1":
        return "P7_DATA_PROTECTION_FRAMEWORK"
    if indicator == "P7-I2":
        return "P7_CYBERSECURITY_FRAMEWORK"
    if indicator == "P7-I3":
        return "P7_RETENTION"
    if indicator == "P7-I4":
        return "P7_ACCOUNTABILITY"
    if indicator == "P7-I5":
        return "P7_GOVERNMENT_ACCESS"
    return "P7_ACCOUNTABILITY"


def _synthetic_task_for_pdf_claim(task: PDFDocumentTask, claim: PDFEvidenceClaim, claim_id: str) -> tuple[CandidateTask, MappingDecision, IndicatorMatch]:
    route_topic = _route_topic_for_indicator(claim.indicator_id)
    evidence_segments = {
        "S1": claim.verbatim_snippet,
        "R_SCOPE": f"PDF document title: {task.title}; page {claim.page_number}; article {claim.article}",
    }
    if claim.indicator_id in {"P4-I2", "P4-I5", "P4-I6", "P4-I10"}:
        allowed_elements = FRAMEWORK_REVIEW_ELEMENTS[claim.indicator_id]
    elif route_topic in P4_REVIEW_ELEMENTS_BY_GROUP:
        allowed_elements = P4_REVIEW_ELEMENTS_BY_GROUP[route_topic]
    elif route_topic == "P6_LOCATION":
        allowed_elements = P6_REVIEW_ELEMENTS
    elif route_topic == "P7_DATA_PROTECTION_FRAMEWORK":
        allowed_elements = FRAMEWORK_REVIEW_ELEMENTS["P7-I1"]
    elif route_topic == "P7_CYBERSECURITY_FRAMEWORK":
        allowed_elements = FRAMEWORK_REVIEW_ELEMENTS["P7-I2"]
    else:
        allowed_elements = P7_REVIEW_ELEMENTS_BY_GROUP.get(route_topic, tuple())
    status_by_element = {item.element_code: item.status for item in claim.elements}
    required_status = [
        RequiredElementReview(
            element_code=code,
            status="present" if status_by_element.get(code) == "supported" else ("uncertain" if status_by_element.get(code) == "uncertain" else "absent"),
            evidence_ids=["S1"],
        )
        for code in allowed_elements
    ]
    triggered = [item.exclusion_code for item in claim.exclusions if item.status == "triggered"]
    match = IndicatorMatch(
        indicator=claim.indicator_id,
        legal_function="operative_rule",
        actor=None,
        modality=None,
        action=None,
        regulated_object=None,
        object_type=None,
        geographic_nexus=None,
        duration=None,
        conditions=[],
        operative_evidence_ids=["S1"],
        supporting_evidence_ids=[],
        evidence_ids=["S1"],
        required_element_status=required_status,
        triggered_exclusions=triggered,
        why_included=claim.mapping_rationale,
        adjacent_indicator_analysis=[],
    )
    framework_candidate = canonical_framework_element(
        claim.indicator_id,
        claim.attributes.framework_candidate_element or claim.attributes.framework_element,
    )
    matched_patterns = ["pdf_direct_mapping"]
    if framework_candidate:
        matched_patterns.append(f"framework_element:{framework_candidate}")
    candidate = CandidateTask(
        task_id=claim_id,
        economy=task.economy,
        indicator_id=claim.indicator_id,
        task_kind=(
            "framework_element"
            if claim.indicator_id in {"P4-I2", "P4-I5", "P4-I6", "P4-I10"}
            else "document_claim"
        ),
        source_type="pdf_document",
        document_id=task.document_id,
        law_title=task.title,
        instrument_type=task.collection,
        focal_provision_id=claim.article or f"document_direct_page_{claim.page_number}",
        normalized_provision_id=_slug(claim.article or f"document_direct_page_{claim.page_number}"),
        processing_mode="document_direct",
        citation_mode="document_direct",
        source_locator=f"page-{claim.page_number}:{claim.article}",
        focal_text_hash=normalized_snippet_hash(claim.verbatim_snippet),
        document_content_hash=task.document_text_hash or task.source_sha256,
        section_heading="",
        focal_text=claim.verbatim_snippet,
        parent_section_text=claim.verbatim_snippet,
        supporting_context=evidence_segments["R_SCOPE"],
        route_topic=route_topic,  # type: ignore[arg-type]
        candidate_indicators=[claim.indicator_id],
        source_url=task.source_url,
        evidence_segments=evidence_segments,
        recall_source="primary_router",
        matched_patterns=matched_patterns,
        audit_confidence="high" if claim.confidence >= 0.75 else "medium",
    )
    decision = MappingDecision(decision="match", matches=[match], rationale=claim.mapping_rationale)
    return candidate, decision, match


def _document_mapping_specs(pillars: set[int] | None):
    if pillars == {4}:
        return [
            INDICATOR_SPECS[indicator]
            for indicator in ("P4-I1", "P4-I2", "P4-I3", "P4-I5", "P4-I6", "P4-I9", "P4-I10")
        ]
    groups = (
        "P6_LOCATION",
        "P7_DATA_PROTECTION_FRAMEWORK",
        "P7_CYBERSECURITY_FRAMEWORK",
        "P7_RETENTION",
        "P7_ACCOUNTABILITY",
        "P7_GOVERNMENT_ACCESS",
    )
    return [
        spec
        for group in groups
        for spec in specs_for_group(group)
        if spec.indicator_id != "P6-I5"
    ]


def _recall_route_document(*, row: dict, project_root: Path, pillars: set[int] | None = None) -> dict:
    row = _malaysia_p4_document_row(row, project_root, pillars)
    text, source_type = _recall_text_for_document(row=row, project_root=project_root)
    if not text.strip():
        return {"candidate_indicators": [], "matched_terms": {}, "matched_spans": [], "recall_source_type": source_type}
    override_indicators = _malaysia_p4_override_indicators(row, pillars)
    if override_indicators:
        return {
            "candidate_indicators": override_indicators,
            "matched_terms": {indicator: ["malaysia_p4_source_override"] for indicator in override_indicators},
            "matched_spans": [
                {
                    "indicator_id": indicator,
                    "terms": ["malaysia_p4_source_override"],
                    "text": f"{row.get('title')} ({row.get('official_number')})",
                }
                for indicator in override_indicators
            ],
            "recall_source_type": f"{source_type}:malaysia_p4_source_override",
        }
    folded = _norm_key(text)
    specs = _document_mapping_specs(pillars)
    title_context = _malaysia_p4_title_context(row)
    matched_terms: dict[str, list[str]] = {}
    matched_spans: list[dict] = []
    for spec in specs:
        if not _malaysia_p4_pdf_source_family_allowed(row, pillars, spec.indicator_id, title_context):
            continue
        categories = (spec.positive_expressions[:12], spec.object_terms[:16], spec.action_terms[:12])
        hits: list[str] = []
        category_count = 0
        for terms in categories:
            category_hits = [term for term in terms if term and _norm_key(term) in folded]
            if category_hits:
                category_count += 1
                hits.extend(category_hits[:3])
        if category_count < 2:
            continue
        matched_terms[spec.indicator_id] = sorted(set(hits))
    if matched_terms:
        for indicator, terms in matched_terms.items():
            snippet = _page_context_snippet(text, terms)
            matched_spans.append({"indicator_id": indicator, "terms": terms[:8], "text": snippet[:1200]})
    return {
        "candidate_indicators": sorted(matched_terms),
        "matched_terms": matched_terms,
        "matched_spans": matched_spans,
        "recall_source_type": source_type,
    }


def _recall_text_for_document(*, row: dict, project_root: Path) -> tuple[str, str]:
    pdf_text = _project_path(project_root, row.get("pdf_text_path"))
    if pdf_text and pdf_text.exists():
        if pdf_text.suffix.casefold() == ".json":
            try:
                return docling_document_text(load_docling_artifact(pdf_text)), "docling_artifact"
            except Exception:
                pass
        try:
            text = pdf_text.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                return text, "existing_recall_text"
        except Exception:
            pass
    normalized = _project_path(project_root, row.get("normalized_path"))
    if normalized and normalized.exists() and normalized.suffix.casefold() not in {".json", ".jsonl"}:
        try:
            text = normalized.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                return text, "normalized_text"
        except Exception:
            pass
    artifact_path = _docling_artifact_path(row=row, project_root=project_root)
    if artifact_path.exists():
        try:
            return docling_document_text(load_docling_artifact(artifact_path)), "docling_artifact"
        except Exception:
            pass
    metadata_text = " ".join(str(row.get(key) or "") for key in ("title", "official_number", "collection", "source_url"))
    return metadata_text, "metadata"


def _materialize_docling_for_candidate(*, row: dict, project_root: Path) -> tuple[Path | None, dict | None, str]:
    artifact_path = _docling_artifact_path(row=row, project_root=project_root)
    raw_path = _project_path(project_root, row.get("raw_path"))
    if artifact_path.exists():
        try:
            artifact = load_docling_artifact(artifact_path)
            if raw_path and raw_path.exists() and artifact.get("source_content_hash") != docling_sha256_path(raw_path):
                pass
            else:
                return artifact_path, artifact, "reused"
        except Exception:
            pass
    if not raw_path or not raw_path.exists() or raw_path.suffix.casefold() != ".pdf":
        text, source_type = _recall_text_for_document(row=row, project_root=project_root)
        if text.strip():
            try:
                artifact = _write_plain_text_direct_artifact(
                    row=row,
                    project_root=project_root,
                    artifact_path=artifact_path,
                    text=text,
                    source_type=source_type,
                    raw_path=raw_path,
                )
                return artifact_path, artifact, "native_text"
            except Exception as exc:
                return None, None, f"text_artifact_failed:{type(exc).__name__}"
        return None, None, "document_text_missing"
    try:
        page_count = pdf_page_count(raw_path)
        page_limit = docling_max_pages()
    except DoclingPdfError as exc:
        return None, None, str(exc)
    if page_count > page_limit:
        reason = (
            "document_too_large"
            f":document_id={str(row.get('document_id') or '')}"
            f":title={_slug(str(row.get('title') or ''))[:80]}"
            f":source={raw_path}"
            f":page_count={page_count}"
            f":limit={page_limit}"
        )
        return None, None, reason
    try:
        artifact = extract_docling_pdf_artifact(
            pdf_path=raw_path,
            artifact_path=artifact_path,
            document_id=str(row.get("document_id") or ""),
            source_url=str(row.get("source_url") or row.get("canonical_url") or ""),
            title=str(row.get("title") or ""),
        )
        return artifact_path, artifact, str(artifact.get("artifact_status") or "created")
    except DoclingPdfError as exc:
        return None, None, str(exc)


def _materialize_route_docling_candidate_for_worker(
    index: int,
    row: dict,
    project_root: str,
    status: str,
    pillars: tuple[int, ...] = (6, 7),
) -> dict:
    root = Path(project_root)
    row = _malaysia_p4_document_row(row, root, set(pillars))
    artifact_path, artifact, artifact_status = _materialize_docling_for_candidate(row=row, project_root=root)
    if artifact_path is None or artifact is None:
        return {"index": index, "status": "failed", "reason": artifact_status or "docling_failed"}
    source_hash = str(artifact.get("document_text_hash") or "")
    if not source_hash:
        return {"index": index, "status": "failed", "reason": "document_text_hash_missing"}
    routed = _route_docling_document(
        row=row, artifact_path=artifact_path, pillars=set(pillars)
    )
    if not routed["candidate_indicators"]:
        return {
            "index": index,
            "status": "filtered",
            "artifact_status": artifact_status,
            "extraction_pass": str(artifact.get("extraction_pass") or ""),
        }
    candidate_indicators = routed["candidate_indicators"]
    document_id = str(row.get("document_id") or "").strip()
    raw_path = _project_path(root, row.get("raw_path"))
    task_id = f"docdirect:{document_id}:{source_hash[:16]}:{_slug('-'.join(candidate_indicators))}"
    override_fingerprint = malaysia_p4_override_fingerprint(row, root) if set(pillars) == {4} else ""
    if override_fingerprint:
        task_id = f"{task_id}:{hashlib.sha256(override_fingerprint.encode('utf-8')).hexdigest()[:12]}"
    if status.casefold() not in {"candidate", "reject", "uncertain", "pass", "relevant", "review"}:
        status = "uncertain"
    return {
        "index": index,
        "status": "task",
        "artifact_status": artifact_status,
        "extraction_pass": str(artifact.get("extraction_pass") or ""),
        "task": {
            "task_id": task_id,
            "economy": str(row.get("economy") or ""),
            "document_id": document_id,
            "collection": str(row.get("collection") or ""),
            "title": str(row.get("title") or document_id),
            "official_number": str(row.get("official_number") or ""),
            "year": str(row.get("year") or ""),
            "language": str(row.get("language") or ""),
            "source_url": str(row.get("source_url") or row.get("canonical_url") or ""),
            "raw_path": str(raw_path),
            "pdf_text_path": str(artifact_path),
            "document_text_hash": source_hash,
            "candidate_indicators": candidate_indicators,
            "matched_pages": routed["matched_pages"],
            "matched_context": routed["matched_context"],
            "prefilter_status": status,
            "source_sha256": source_hash,
            "page_count": routed["page_count"],
        },
    }


def _write_plain_text_direct_artifact(
    *,
    row: dict,
    project_root: Path,
    artifact_path: Path,
    text: str,
    source_type: str,
    raw_path: Path | None,
) -> dict:
    normalized = re.sub(r"\r\n?", "\n", str(text or "")).strip()
    if not normalized:
        raise ValueError("empty_document_text")
    chunks = _split_text_pages(normalized)
    pages = [
        {
            "page_number": index,
            "text": chunk,
            "text_hash": hashlib.sha256(re.sub(r"\s+", " ", chunk).strip().encode("utf-8")).hexdigest(),
            "provenance": [{"source": source_type, "page_number": index}],
        }
        for index, chunk in enumerate(chunks, start=1)
    ]
    source_hash = docling_sha256_path(raw_path) if raw_path and raw_path.exists() else hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    artifact = {
        "schema_version": "rdtii-docling-pdf-artifact-v1",
        "document_id": str(row.get("document_id") or ""),
        "extractor": "plain_text_document_direct",
        "extractor_version": "rdtii-plain-text-direct-v1",
        "source_content_hash": source_hash,
        "document_text_hash": hashlib.sha256(re.sub(r"\s+", " ", normalized).strip().encode("utf-8")).hexdigest(),
        "page_count": len(pages),
        "character_count": len(normalized),
        "ocr_used": False,
        "extraction_pass": "native_text",
        "native_character_count": len(normalized),
        "final_character_count": len(normalized),
        "artifact_status": "created",
        "source_url": str(row.get("source_url") or row.get("canonical_url") or ""),
        "title": str(row.get("title") or ""),
        "pages": pages,
    }
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = artifact_path.with_suffix(artifact_path.suffix + ".tmp")
    tmp.write_text(json.dumps(artifact, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(artifact_path)
    return load_docling_artifact(artifact_path)


def _split_text_pages(text: str, *, max_chars: int = 12000) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    pages: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs or [text]:
        extra = len(paragraph) + (2 if current else 0)
        if current and current_len + extra > max_chars:
            pages.append("\n\n".join(current))
            current = [paragraph]
            current_len = len(paragraph)
        else:
            current.append(paragraph)
            current_len += extra
    if current:
        pages.append("\n\n".join(current))
    return pages or [text]


def _valid_existing_docling_artifact(*, row: dict, project_root: Path) -> bool:
    artifact_path = _docling_artifact_path(row=row, project_root=project_root)
    if not artifact_path.exists():
        return False
    raw_path = _project_path(project_root, row.get("raw_path"))
    try:
        artifact = load_docling_artifact(artifact_path)
        if raw_path and raw_path.exists() and artifact.get("source_content_hash") != docling_sha256_path(raw_path):
            return False
        return bool(artifact.get("document_text_hash") and int(artifact.get("page_count") or 0) > 0 and docling_document_text(artifact).strip())
    except Exception:
        return False


def _finalize_pdf_direct_stats(stats: dict[str, object]) -> dict[str, object]:
    finalized: dict[str, object] = {}
    for key, value in stats.items():
        if isinstance(value, Counter):
            finalized[key] = dict(value)
        else:
            finalized[key] = value
    skipped = finalized.get("explicitly_skipped_with_reason")
    finalized["explicitly_skipped_total"] = sum(int(v) for v in skipped.values()) if isinstance(skipped, dict) else 0
    return finalized


def _assert_pdf_direct_invariants(stats: dict[str, object]) -> None:
    def n(key: str) -> int:
        return int(stats.get(key) or 0)

    recall = n("recall_candidate_pdfs")
    reused = n("existing_docling_artifacts_reused")
    attempted = n("artifact_materialization_attempted")
    skipped = n("explicitly_skipped_total")
    if recall != reused + attempted + skipped:
        raise RuntimeError(
            "Document-direct scheduling invariant failed: "
            f"recall_candidate_documents={recall}, artifact_reused={reused}, "
            f"artifact_materialization_attempted={attempted}, explicitly_skipped={skipped}"
        )
    if attempted != n("docling_native_text_success") + n("docling_ocr_fallback_success") + n("docling_failed"):
        raise RuntimeError(
            "Document-direct materialization invariant failed: "
            f"attempted={attempted}, native_success={n('docling_native_text_success')}, "
            f"ocr_success={n('docling_ocr_fallback_success')}, failed={n('docling_failed')}"
        )
    successful_artifacts = reused + n("docling_native_text_success") + n("docling_ocr_fallback_success")
    if successful_artifacts != n("post_docling_routed_documents"):
        raise RuntimeError(
            "Document-direct routed-documents invariant failed: "
            f"successful_artifacts={successful_artifacts}, post_docling_routed={n('post_docling_routed_documents')}"
        )
    if n("post_docling_routed_documents") != n("direct_mapper_tasks_created") + n("post_docling_filtered_out"):
        raise RuntimeError(
            "Document-direct page-routing invariant failed: "
            f"post_docling_routed={n('post_docling_routed_documents')}, "
            f"direct_tasks_created={n('direct_mapper_tasks_created')}, "
            f"filtered_out={n('post_docling_filtered_out')}"
        )


def _docling_artifact_path(*, row: dict, project_root: Path) -> Path:
    value = str(row.get("pdf_text_path") or "")
    existing = _project_path(project_root, value)
    if existing and existing.suffix.casefold() == ".json":
        return existing
    collection = _collection_slug(row.get("collection"))
    filename = _safe_filename(row.get("document_id") or row.get("instrument_id") or "document")
    economy = _slug(str(row.get("economy") or "singapore"))
    return project_root / "outputs" / "corpus" / economy / "docling_pdf" / collection / f"{filename}.json"


def _project_path(project_root: Path, value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = project_root / path
    return path


def _collection_slug(value: object) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
    return slug or "unknown"


def _safe_filename(value: object) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return name or "document"


def _route_docling_document(
    *,
    row: dict,
    artifact_path: Path,
    pillars: set[int] | None = None,
) -> dict:
    try:
        artifact = load_docling_artifact(artifact_path)
    except Exception:
        return {"candidate_indicators": [], "matched_pages": [], "matched_context": "", "page_count": None}
    specs = _document_mapping_specs(pillars)
    override_indicators = _malaysia_p4_override_indicators(row, pillars)
    title_context = _malaysia_p4_title_context(row)
    page_hits: dict[int, set[str]] = {}
    snippets: dict[int, list[str]] = {}
    for page in artifact.get("pages") or []:
        page_no = int(page.get("page_number") or 0)
        text = str(page.get("text") or "")
        folded = _norm_key(text)
        if not page_no or not folded:
            continue
        for spec in specs:
            if override_indicators and spec.indicator_id not in set(override_indicators):
                continue
            if not _malaysia_p4_pdf_source_family_allowed(row, pillars, spec.indicator_id, title_context):
                continue
            categories = (spec.positive_expressions[:12], spec.object_terms[:16], spec.action_terms[:12])
            matched_categories = 0
            matched_terms: list[str] = []
            for terms in categories:
                hits = [term for term in terms if term and _norm_key(term) in folded]
                if hits:
                    matched_categories += 1
                    matched_terms.extend(hits[:2])
            if matched_categories < 2:
                continue
            page_hits.setdefault(page_no, set()).add(spec.indicator_id)
            snippets.setdefault(page_no, []).append(_page_context_snippet(text, matched_terms))
    candidate_indicators = sorted({indicator for values in page_hits.values() for indicator in values})
    if override_indicators:
        candidate_indicators = sorted(set(candidate_indicators) | set(override_indicators))
        if not page_hits:
            first_page = next((int(page.get("page_number") or 0) for page in artifact.get("pages") or [] if int(page.get("page_number") or 0)), 1)
            page_hits[first_page] = set(override_indicators)
            snippets[first_page] = [f"{row.get('title')} ({row.get('official_number')})"]
    matched_pages = sorted(page_hits)
    context_parts: list[str] = []
    for page_no in matched_pages[:12]:
        indicators = ", ".join(sorted(page_hits[page_no]))
        page_context = "\n---\n".join(snippets.get(page_no, [])[:4])
        context_parts.append(f"[PDF page {page_no} | candidate indicators: {indicators}]\n{page_context}")
    return {
        "candidate_indicators": candidate_indicators,
        "matched_pages": matched_pages,
        "matched_context": "\n\n".join(context_parts)[:24000],
        "page_count": int(artifact.get("page_count") or 0) or None,
    }


def _malaysia_p4_document_row(row: dict, project_root: Path, pillars: set[int] | None) -> dict:
    if pillars == {4} and str(row.get("economy") or "").strip().casefold() == "malaysia":
        return apply_malaysia_p4_override(row, project_root)
    return row


def _malaysia_p4_override_indicators(row: dict, pillars: set[int] | None) -> list[str]:
    if pillars != {4}:
        return []
    if str(row.get("economy") or "").strip().casefold() != "malaysia":
        return []
    indicators = [str(item) for item in row.get("malaysia_p4_indicators") or [] if str(item).startswith("P4-")]
    return sorted(set(indicators))


def _malaysia_p4_title_context(row: dict) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("title", "official_number", "collection", "source_family", "source_url", "canonical_url")
    )


def _malaysia_p4_pdf_source_family_allowed(row: dict, pillars: set[int] | None, indicator: str, title_context: str) -> bool:
    if pillars != {4} or str(row.get("economy") or "").strip().casefold() != "malaysia":
        return True
    if row.get("malaysia_p4_override_version"):
        return True
    if indicator in {"P4-I2", "P4-I3"}:
        return source_family_hit(title_context, ("patent", "patent procedure", "civil procedure", "court rules", "government use", "competition"))
    if indicator in {"P4-I5", "P4-I6"}:
        return source_family_hit(title_context, ("copyright",))
    if indicator in {"P4-I9", "P4-I10"}:
        return source_family_hit(title_context, ("trade secret", "confidentiality"))
    return True


def _page_context_snippet(text: str, terms: list[str]) -> str:
    if not text:
        return ""
    folded = _norm_key(text)
    positions = []
    for term in terms:
        needle = _norm_key(term)
        if not needle:
            continue
        pos = folded.find(needle)
        if pos >= 0:
            positions.append(pos)
    if not positions:
        return text[:2000].strip()
    pos = min(positions)
    start = max(0, pos - 900)
    end = min(len(text), pos + 1600)
    return text[start:end].strip()


def _attributes_from_match(match: dict) -> ReviewerAttributes:
    reviewer = match.get("reviewer") if isinstance(match, dict) else None
    if isinstance(reviewer, dict):
        attrs = reviewer.get("attributes")
        if isinstance(attrs, dict):
            try:
                return ReviewerAttributes.model_validate(attrs)
            except Exception:
                pass
    attrs = match.get("reviewer_attributes") if isinstance(match, dict) else None
    if isinstance(attrs, dict):
        try:
            return ReviewerAttributes.model_validate(attrs)
        except Exception:
            pass
    return ReviewerAttributes()


def _reviewer_final_decision(result: ValidatedTaskResult) -> str:
    if result.reviewer_decision and result.reviewer_decision.decision:
        return str(result.reviewer_decision.decision)
    # A direct claim has no accepted fallback: only the typed reviewer decision
    # can authorize the resolver's terminal state.
    return ""


def _pdf_claim_authoritative_conflicts(
    claim: PDFEvidenceClaim,
    reviewed: ValidatedTaskResult,
    authoritative_decision: str,
    authoritative_focal_role: str,
) -> list[str]:
    conflicts: list[str] = []
    if reviewed.indicator and reviewed.indicator != claim.indicator_id:
        conflicts.append("PDF_CLAIM_INDICATOR_CONFLICT")
    if authoritative_decision not in {"match", "accepted"}:
        conflicts.append("PDF_CLAIM_FINAL_DECISION_NOT_ACCEPTED")
    mapper_role = str(claim.focal_role or "").strip()
    if mapper_role == "supporting_only" or authoritative_focal_role == "supporting_only":
        conflicts.append("PDF_CLAIM_SUPPORTING_ONLY_FOCAL")
    if mapper_role and authoritative_focal_role and mapper_role != authoritative_focal_role:
        conflicts.append("PDF_CLAIM_FOCAL_ROLE_CONFLICT")
    return sorted(set(conflicts))


def _malaysia_p4_pdf_reviewer_no_match_rejected(
    task: PDFDocumentTask,
    reviewed: ValidatedTaskResult,
    authoritative_decision: str,
) -> bool:
    return (
        task.economy.casefold() == "malaysia"
        and any(indicator.startswith("P4-") for indicator in task.candidate_indicators)
        and authoritative_decision == "no_match"
        and reviewed.status == "rejected"
        and str(reviewed.technical_detail or "") == ""
    )


def _validated_attributes_for_indicator(indicator: str, attrs: ReviewerAttributes, task: CandidateTask | None, match: dict | None = None) -> dict:
    if indicator == "P7-I3":
        periods = _retention_periods_from_attrs(attrs, match)
        out = {
            "record_scope_basis": attrs.record_scope_basis,
            "retention_periods": periods,
        }
        if attrs.trigger_event:
            out["trigger_event"] = attrs.trigger_event
        return {key: value for key, value in out.items() if value is not None and value != "" and value != []}
    if indicator == "P7-I4":
        return {"accountability_path": attrs.accountability_path} if attrs.accountability_path else {}
    if indicator == "P7-I5":
        return {"judicial_authorization": attrs.judicial_authorization} if attrs.judicial_authorization else {}
    if indicator in {"P4-I2", "P4-I5", "P4-I6", "P4-I10"}:
        p4_facts = _p4_structured_facts_from_match(match)
        out = {
            "framework_element": _canonical_framework_element(
                indicator,
                str(match.get("p4_framework_element") or p4_facts.get("framework_element") or "") if isinstance(match, dict) else str(p4_facts.get("framework_element") or ""),
            ),
            "legal_function": attrs.framework_legal_function,
            "coverage": attrs.coverage,
        }
        for key in (
            "evidence_character",
            "remedy_direction",
            "protected_private_or_commercial_information",
            "unauthorised_acquisition_use_or_disclosure",
            "government_or_official_only",
        ):
            if key in p4_facts:
                out[key] = p4_facts[key]
        if indicator == "P4-I6" and p4_facts.get("online_nexus"):
            out["online_nexus"] = p4_facts["online_nexus"]
        return {key: value for key, value in out.items() if value is not None and value != ""}
    if indicator in {"P7-I1", "P7-I2"}:
        out = {
            "framework_element": _canonical_framework_element(indicator, attrs.framework_element),
            "legal_function": attrs.framework_legal_function,
            "coverage": attrs.coverage,
        }
        return {key: value for key, value in out.items() if value is not None and value != ""}
    if indicator in {"P4-I1", "P4-I3", "P4-I9"}:
        return _p4_structured_facts_from_match(match)
    if indicator == "P6-I2":
        focal = task.focal_text if task is not None else ""
        out = {
            "information_object": _attr_value_from_match(match, "regulated_object") or _extract_information_object_hint(focal),
            "storage_action": _attr_value_from_match(match, "action") or _extract_storage_action_hint(focal),
            "location_text": _attr_value_from_match(match, "geographic_nexus") or _extract_location_hint(focal),
            "location_relation": "storage_object",
        }
        return {key: value for key, value in out.items() if value is not None and value != ""}
    if indicator == "P6-I4":
        focal = task.focal_text if task is not None else ""
        out = {
            "regulated_object_type": _attr_value_from_match(match, "object_type") or _p6_object_type_hint(focal),
            "cross_border_direction": _attr_value_from_match(match, "geographic_nexus") or _extract_cross_border_hint(focal),
            "transfer_condition": _conditions_from_match(match),
            "information_bearing_object_evidence": _attr_value_from_match(match, "regulated_object") or _extract_information_object_hint(focal),
        }
        return {key: value for key, value in out.items() if value is not None and value != ""}
    if indicator == "P6-I5":
        return {"treaty_status": "in_force", "coverage": attrs.coverage or "Horizontal"}
    return {}


def _p4_structured_facts_from_match(match: dict | None) -> dict:
    if not isinstance(match, dict):
        return {}
    reviewer = match.get("reviewer")
    if not isinstance(reviewer, dict):
        return {}
    out: dict = {}
    for check in reviewer.get("optional_checks") or []:
        if not isinstance(check, dict):
            continue
        if check.get("check_code") == "P4_ONLINE_NEXUS" and check.get("status") == "supported":
            out["online_nexus"] = str(check.get("reason") or "")
        if check.get("check_code") == "P4_FRAMEWORK_ELEMENT":
            try:
                framework_payload = json.loads(str(check.get("reason") or "{}"))
            except json.JSONDecodeError:
                framework_payload = {}
            if isinstance(framework_payload, dict):
                out.update({
                    str(key): value
                    for key, value in framework_payload.items()
                    if value not in (None, "", [], {})
                })
        if check.get("check_code") != "P4_STRUCTURED_FACTS":
            continue
        try:
            facts = json.loads(str(check.get("reason") or "{}"))
        except json.JSONDecodeError:
            continue
        if isinstance(facts, dict):
            out.update(
                {
                    str(key): value
                    for key, value in facts.items()
                    if value not in (None, "", [], {})
                }
            )
    return out


def _canonical_framework_element(indicator: str, value: str | None) -> str | None:
    value = str(value or "").strip()
    aliases = {
        "P7-I1": {
            "personal_data_scope": "personal_data_scope",
            "substantive_duties_or_rights": "substantive_duties_or_rights",
            "regulator_or_enforcement": "regulator_or_enforcement",
            "authority_or_enforcement": "regulator_or_enforcement",
        },
        "P7-I2": {
            "cybersecurity_scope": "cybersecurity_scope",
            "substantive_cybersecurity_obligation": "substantive_cybersecurity_obligation",
            "substantive_duties_or_rights": "substantive_cybersecurity_obligation",
            "authority_or_enforcement": "authority_or_enforcement",
            "regulator_or_enforcement": "authority_or_enforcement",
        },
    }
    return aliases.get(indicator, {}).get(value, value or None)


def _attr_value_from_match(match: dict | None, key: str) -> str:
    if not isinstance(match, dict):
        return ""
    value = match.get(key)
    return str(value).strip() if value is not None else ""


def _conditions_from_match(match: dict | None) -> str:
    if not isinstance(match, dict):
        return ""
    value = match.get("conditions")
    if isinstance(value, list):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _dedupe_preserve_order(values: list[str] | tuple[str, ...]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = str(value or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _extract_information_object_hint(text: str) -> str:
    patterns = (
        r"\b(personal data|data|information|records?|registers?|documents?|books?|copies?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.I)
        if match:
            return match.group(1)
    return ""


def _extract_storage_action_hint(text: str) -> str:
    match = re.search(r"\b(keep|kept|store|stored|maintain|maintained|retain|retained|preserve|preserved)\b", text or "", flags=re.I)
    return match.group(1) if match else ""


def _extract_location_hint(text: str) -> str:
    match = re.search(r"\b(in|within)\s+Singapore\b", text or "", flags=re.I)
    return match.group(0) if match else ""


def _extract_cross_border_hint(text: str) -> str:
    match = re.search(r"\b(outside Singapore|overseas|foreign country|transfer(?:red)?\s+outside\s+Singapore)\b", text or "", flags=re.I)
    return match.group(0) if match else ""


def _p6_object_type_hint(text: str) -> str:
    lowered = (text or "").casefold()
    if any(term in lowered for term in ("personal data", "data", "information")):
        return "data_or_information"
    if any(term in lowered for term in ("record", "register", "document", "book", "copy")):
        return "record_or_document"
    return ""


def _retention_periods_from_attrs(attrs: ReviewerAttributes, match: dict | None = None) -> list[dict]:
    value = str(attrs.minimum_duration_value or "").strip()
    unit = str(attrs.minimum_duration_unit or "").strip()
    trigger = str(attrs.trigger_event or "").strip()
    if not value and not unit:
        return []
    values = [item.strip() for item in re.split(r"\s*(?:;|/|\bor\b)\s*", value) if item.strip()]
    if not values:
        values = [value]
    periods: list[dict] = []
    conditions = _period_conditions_from_match(match)
    formula_hint = "longer of" if "longer of" in value.casefold() else ("latest of" if "latest of" in value.casefold() else "")
    for idx, item in enumerate(values):
        match = re.match(r"(?P<value>\d+)\s*(?P<unit>day|days|month|months|year|years|hour|hours)?", item, flags=re.I)
        if match:
            period_value = match.group("value")
            period_unit = match.group("unit") or unit
        else:
            period_value = item
            period_unit = unit
        condition = _condition_for_period(period_value, item, idx, conditions, trigger, formula_hint)
        trigger_event = _trigger_without_embedded_conditions(trigger, condition)
        periods.append({"value": period_value, "unit": period_unit, "condition": condition, "trigger_event": trigger_event})
    return periods


def _period_conditions_from_match(match: dict | None) -> list[str]:
    if not isinstance(match, dict):
        return []
    raw = match.get("conditions")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _condition_for_period(period_value: str, raw_period: str, index: int, conditions: list[str], trigger: str, formula_hint: str = "") -> str:
    raw_lower = raw_period.casefold()
    if conditions:
        exact = _matching_condition_for_value(period_value, conditions)
        if exact:
            return exact
    trigger_condition = _matching_condition_for_value(period_value, re.split(r"\s*;\s*|\s*,\s*", trigger))
    if trigger_condition:
        return trigger_condition
    if "longer of" in raw_lower or "longer of" in trigger.casefold() or formula_hint == "longer of":
        return "longer-of alternative"
    if "latest of" in raw_lower or "latest of" in trigger.casefold() or formula_hint == "latest of":
        return "latest-of alternative"
    if len(conditions) > 1 and index < len(conditions):
        return conditions[index]
    if len(conditions) == 1 and not re.search(r"\b(subject to|referred to|pursuant to)\b", conditions[0], flags=re.I):
        return conditions[0]
    return ""


def _matching_condition_for_value(period_value: str, conditions: list[str]) -> str:
    value = str(period_value).strip().casefold()
    for condition in conditions:
        folded = condition.casefold()
        if not value:
            continue
        if re.search(rf"\b{re.escape(value)}\s*(?:year|years|month|months|day|days|hour|hours)\b", folded):
            return condition
        if value == "7" and ("before" in folded or "ending before" in folded):
            return condition
        if value == "5" and ("on or after" in folded or "after 1 january 2007" in folded or "on or after 6 september 2018" in folded):
            return condition
        if value == "2" and "before 6 september 2018" in folded:
            return condition
    return ""


def _trigger_without_embedded_conditions(trigger: str, condition: str) -> str:
    text = str(trigger or "").strip()
    if not text:
        return ""
    if condition and condition in text:
        text = text.replace(condition, "").strip(" ;,")
    text = re.sub(r"\bbefore \d{1,2} [A-Za-z]+ \d{4} for \d+ years?\b", "", text, flags=re.I)
    text = re.sub(r"\bon or after \d{1,2} [A-Za-z]+ \d{4} for \d+ years?\b", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" ;,") or trigger


def _notes_from_validated_attributes(attrs: dict) -> str:
    parts: list[str] = []
    for key, value in attrs.items():
        if key == "retention_periods" and isinstance(value, list):
            periods = []
            for item in value:
                if isinstance(item, dict):
                    periods.append(" ".join(str(item.get(part) or "").strip() for part in ("value", "unit", "trigger_event")).strip())
            if periods:
                parts.append(f"retention_periods={' | '.join(periods)}")
        else:
            parts.append(f"{key}={value}")
    return "; ".join(parts)


def _notes_from_attributes(attrs: ReviewerAttributes) -> str:
    parts = []
    if attrs.record_scope_basis:
        parts.append(f"record_scope_basis={attrs.record_scope_basis}")
    if attrs.judicial_authorization:
        parts.append(f"judicial_authorization={attrs.judicial_authorization}")
    if attrs.accountability_path:
        parts.append(f"accountability_path={attrs.accountability_path}")
    if attrs.minimum_duration_value or attrs.minimum_duration_unit:
        parts.append(f"minimum_duration={attrs.minimum_duration_value or ''} {attrs.minimum_duration_unit or ''}".strip())
    if attrs.trigger_event:
        parts.append(f"trigger_event={attrs.trigger_event}")
    return "; ".join(parts)


def _provision_source_text(task: CandidateTask) -> str:
    values = list(task.evidence_segments.values()) if task.evidence_segments else []
    if not values:
        values = [task.focal_text, task.parent_section_text, task.supporting_context]
    return "\n".join(str(value or "") for value in values if str(value or "").strip())


def _atomic_snippet_for_result(task: CandidateTask, match: dict) -> str:
    """Use focal text as the submission quote unless the mapper quote is focal-only.

    Mapper cache may contain parent/supporting context in ``quote``. Citation and
    Article/Section must track the focal provision, so supporting context cannot
    become the primary submission snippet.
    """

    focal = str(task.focal_text or "").strip()
    quote = str(match.get("quote") or "").strip()
    if not focal:
        return quote
    if quote and _norm_key(quote) and _norm_key(quote) in _norm_key(focal):
        return quote
    return focal


def _verified_snippet_candidate(
    snippet: str,
    *,
    source_text: str,
    article: str,
    expected_article: str,
    source_url: str | None,
) -> tuple[str, object]:
    candidates = _snippet_candidates(snippet)
    last_validation = None
    for candidate in candidates:
        validation = validate_provision_citation(
            verbatim_snippet=candidate,
            source_text=source_text,
            article=article,
            expected_article=expected_article,
            source_url=source_url,
        )
        if validation.status == "verified":
            return candidate, validation
        last_validation = validation
    return (candidates[0] if candidates else snippet), last_validation or validate_provision_citation(
        verbatim_snippet=snippet,
        source_text=source_text,
        article=article,
        expected_article=expected_article,
        source_url=source_url,
    )


def _snippet_candidates(snippet: str) -> list[str]:
    raw = str(snippet or "").strip()
    candidates: list[str] = []
    for value in (
        raw,
        raw.strip("\"'“”‘’"),
        re.sub(r"^[\"“](.*)[\"”]$", r"\1", raw).strip(),
    ):
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def _coverage_for_title(title: str) -> str:
    folded = title.casefold()
    if "personal data protection" in folded or folded.startswith("privacy act"):
        return "Horizontal"
    return "Sectoral"


def _sector_for_title(title: str) -> str:
    text = title.casefold()
    if "cybersecurity" in text or "cyber security" in text or "critical infrastructure" in text:
        return "Cybersecurity"
    if "personal data protection" in text or "privacy" in text:
        return "Data protection"
    if any(term in text for term in ("bank", "financial", "finance", "insurance", "securities", "futures", "payment", "credit")):
        return "Financial services"
    if "telecommunication" in text or "telecom" in text:
        return "Telecommunications"
    if "tax" in text or "customs" in text:
        return "Taxation"
    if "companies" in text or "business" in text or "account" in text:
        return "Corporate and accounting"
    return "Other"


def _row_focal_quote(row: dict) -> str:
    return str(row.get("focal_quote") or row.get("verbatim_snippet") or "")


def _row_validated_attributes(row: dict) -> dict:
    attrs = row.get("validated_attributes")
    if isinstance(attrs, dict):
        return attrs
    attrs = row.get("reviewer_attributes")
    return attrs if isinstance(attrs, dict) else {}


def _validated_retention_periods_present(attrs: dict) -> bool:
    periods = attrs.get("retention_periods")
    if not isinstance(periods, list) or not periods:
        return False
    for period in periods:
        if not isinstance(period, dict):
            return False
        if not period.get("value") or not period.get("unit") or not period.get("trigger_event"):
            return False
    return True


def _norm_key(value) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def _p6_i2_submission_snippet_has_storage_situs(row: dict, economy: str) -> bool:
    text = " ".join([_row_focal_quote(row), str(row.get("mapping_rationale") or ""), str(row.get("notes") or "")]).casefold()
    storage = any(term in text for term in ("keep", "kept", "store", "stored", "maintain", "maintained", "retain", "retained", "preserve", "copy", "copies"))
    information_object = any(term in text for term in ("record", "records", "book", "books", "data", "information", "document", "documents", "register", "copy", "copies"))
    domestic = any(term in text for term in domestic_terms(economy))
    false_situs = any(
        term in text
        for term in (
            "published in",
            "accessible in",
            "made available in",
            "online location",
            "website",
            "portal",
            "address in singapore",
            "address for service in singapore",
            "resident in singapore",
            "persons in singapore",
            "person in singapore",
            "patient in singapore",
            "patients in singapore",
            "live birth in singapore",
            "birth by a patient in singapore",
            "births by patients in singapore",
            "diagnosed with",
            "treated for",
            "treated in singapore",
            "diagnosed and treated in singapore",
            "places where registers are kept",
            "places at which registers are kept",
            "places at which copies of those registers are kept",
            "records of the places at which",
            "place where the register is kept",
            "where registers are kept",
        )
    )
    return storage and information_object and domestic and not false_situs


def _p6_i4_submission_non_data_asset(row: dict) -> bool:
    text = " ".join([_row_focal_quote(row), str(row.get("mapping_rationale") or ""), str(row.get("notes") or "")]).casefold()
    asset_terms = (
        "security",
        "securities",
        "coupon",
        "coupons",
        "share",
        "shares",
        "debenture",
        "debentures",
        "bond",
        "bonds",
        "property",
        "goods",
        "commodity",
        "commodities",
        "human tissue",
        "tissue",
        "organ",
        "cell",
        "specimen",
        "biological material",
        "cord blood",
        "blood",
        "regulated product",
        "electrical or electronic product",
        "e-waste",
        "product",
        "products",
    )
    transfer_terms = ("transfer", "transferred", "transferor", "transferee", "import", "export", "deliver", "shipment")
    data_terms = (
        "personal data",
        "data protection",
        "information",
        "electronic record",
        "records of",
        "copy of",
        "database",
        "dataset",
        "disclosure of information",
        "transfer of data",
        "transfer data",
    )
    return any(term in text for term in asset_terms) and any(term in text for term in transfer_terms) and not any(term in text for term in data_terms)


def _p7_i3_submission_indeterminate_duration(row: dict) -> bool:
    attrs = _row_validated_attributes(row)
    periods = attrs.get("retention_periods") if isinstance(attrs.get("retention_periods"), list) else []
    duration_text = " ".join(
        " ".join(str(period.get(key) or "") for key in ("value", "unit", "trigger_event"))
        for period in periods
        if isinstance(period, dict)
    ).casefold()
    concrete_duration = bool(re.search(r"\b\d+\b", duration_text)) and any(term in duration_text for term in ("day", "month", "year", "hour"))
    indeterminate = (
        "prescribed period",
        "prescribed duration",
        "period prescribed",
        "as prescribed",
        "such period",
        "specified period",
        "not specified",
        "unspecified",
        "unknown",
        "uncertain",
        "cannot determine",
        "not determinable",
    )
    if not concrete_duration and any(term in duration_text for term in indeterminate):
        return True
    return duration_text.strip() in {"period", "duration"}


def _slug(value) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-") or "record"


def _cache_stage(task_type: str) -> str:
    return {
        "provision": "mapper",
        "reviewer": "reviewer",
    }.get(task_type, task_type)


def _legacy_reviewer_task_key(task_id: str, prompt_version: str, model_name: str) -> str:
    return "|".join(["reviewer_by_task", task_id, prompt_version, model_name])


def _migrated_reviewer_task_key(task_id: str, model_name: str) -> str:
    return "|".join(["reviewer_v3_by_task", task_id, model_name])


def _reviewer_cache_row_score(row: dict) -> int:
    """Prefer the most complete/most recent reviewer cache row during v3 migration."""

    decision = row.get("decision") if isinstance(row.get("decision"), dict) else {}
    elements = decision.get("elements") or decision.get("element_assessments") or []
    exclusions = decision.get("exclusions") or decision.get("exclusion_assessments") or []
    prompt_version = str(row.get("prompt_version") or "")
    score = 0
    if "v6" in prompt_version:
        score += 1000
    elif "v5" in prompt_version:
        score += 800
    elif "v4" in prompt_version:
        score += 400
    elif "v3" in prompt_version:
        score += 200
    if "retention-scope" in prompt_version:
        score += 120
    if "focal-role" in prompt_version:
        score += 120
    if "evidence-catalog" in prompt_version:
        score += 80
    score += len(elements) * 20 if isinstance(elements, list) else 0
    score += len(exclusions) * 10 if isinstance(exclusions, list) else 0
    if decision.get("attributes"):
        score += 50
    if decision.get("record_scope_basis"):
        score += 20
    if decision.get("focal_role") in {"operative", "supporting_only", "uncertain"}:
        score += 20
    return score


def _parse_cached_mapping_decision(payload) -> MappingDecision:
    """Load a cached Mapper decision without letting legacy free-text reason fields control status.

    The Mapper schema and cache key stay unchanged. This parser only strips historical
    non-control fields (for example ``reason_codes``) before Pydantic validation so
    cached no_match and structurally valid match records can be replayed safely.
    """
    if not isinstance(payload, dict):
        raise TypeError("cached mapper decision is not an object")
    data = dict(payload)
    if data.get("decision") == "no_match":
        return MappingDecision.model_validate(
            {
                "decision": "no_match",
                "matches": [],
                "rationale": str(data.get("rationale") or ""),
            }
        )
    allowed_top = set(MappingDecision.model_fields)
    allowed_match = set(IndicatorMatch.model_fields)
    data = {key: value for key, value in data.items() if key in allowed_top}
    data.setdefault("matches", [])
    data.setdefault("rationale", "")
    if isinstance(data.get("matches"), list):
        data["matches"] = [
            {key: value for key, value in item.items() if key in allowed_match}
            if isinstance(item, dict)
            else item
            for item in data["matches"]
        ]
    return MappingDecision.model_validate(data)


def _parse_cached_reviewer_decision(payload) -> ReviewerDecision:
    if not isinstance(payload, dict):
        raise TypeError("cached reviewer decision is not an object")
    data = dict(payload)
    decision_missing = "decision" not in data
    focal = dict(data.get("focal_integrity") or {})
    if focal.get("status") == "technical_error":
        focal["status"] = "uncertain"
        data["focal_integrity"] = focal
    if focal.get("status") == "incomplete_focal_context":
        focal["status"] = "incomplete_context"
        data["focal_integrity"] = focal
    if data.get("focal_role") in {"incomplete", "technical_mismatch"}:
        data["focal_role"] = "uncertain"
    if data.get("record_scope_basis") and "attributes" not in data:
        data["attributes"] = {"record_scope_basis": data.get("record_scope_basis")}
    if data.get("legal_reasoning") and not data.get("review_reason"):
        data["review_reason"] = data.get("legal_reasoning")
    data.pop("triggered_exclusions", None)
    data.pop("supporting_evidence_ids", None)
    data.pop("legal_reasoning", None)
    data.pop("record_scope_basis", None)
    data.setdefault("focal_role", "operative")
    data.setdefault("decision", "uncertain")
    data.setdefault("review_reason", "")
    reviewer = ReviewerDecision.model_validate(data)
    if decision_missing:
        checks = list(reviewer.optional_checks)
        checks.append(
            ReviewerOptionalCheck(
                check_code="REVIEWER_DECISION_MISSING_IN_CACHE",
                status="applied",
                evidence_ids=[],
                reason="Cached reviewer payload predates persisted final decision field.",
            )
        )
        reviewer = reviewer.model_copy(update={"optional_checks": checks})
    return reviewer


def _complete_cached_reviewer(task: CandidateTask, reviewer: ReviewerDecision, match: IndicatorMatch | None) -> ReviewerDecision:
    if task.route_topic in {"P7_DATA_PROTECTION_FRAMEWORK", "P7_CYBERSECURITY_FRAMEWORK"}:
        return reviewer
    allowed_elements = _review_required_elements(task, match)
    allowed_exclusions = _review_allowed_exclusions(task)
    elements_by_code = {item.element_code: item for item in reviewer.element_assessments}
    exclusions_by_code = {item.exclusion_code: item for item in reviewer.exclusion_assessments}
    fallback_evidence_ids = _first_reviewer_evidence_ids(reviewer) or list(task.evidence_segments)[:1]
    exclusions: list[ReviewerExclusionAssessment] = []
    for code in allowed_exclusions:
        if code in exclusions_by_code:
            exclusions.append(_with_fallback_evidence(exclusions_by_code[code], fallback_evidence_ids))
        else:
            status = "not_triggered"
            reason = "Added during reviewer cache schema migration; no triggered finding existed in cached reviewer decision."
            if task.route_topic == "P7_RETENTION" and code in {"GOVERNMENT_DATA_ONLY", "PUBLIC_ADMINISTRATION_INTERNAL_ONLY"} and _looks_government_internal_retention(task):
                status = "triggered"
                reason = "Deterministic cache migration detected government/internal public-administration retention scope."
            evidence_ids = fallback_evidence_ids if status == "triggered" else []
            exclusions.append(ReviewerExclusionAssessment(exclusion_id=code, status=status, evidence_ids=evidence_ids, reason=reason))
    attributes = reviewer.attributes
    if task.route_topic == "P7_RETENTION" and attributes.record_scope_basis is None:
        attributes = attributes.model_copy(update={"record_scope_basis": "UNCERTAIN"})
    if task.route_topic == "P7_RETENTION":
        elements_by_code.update(_migrated_retention_elements(task, reviewer, attributes, match))
    if task.route_topic == "P7_ACCOUNTABILITY" and attributes.accountability_path is None:
        statuses = {item.element_code: item.status for item in elements_by_code.values()}
        path = "dpo_and_dpia" if statuses.get("DPO_PATH") == "supported" and statuses.get("DPIA_PATH") == "supported" else "dpo" if statuses.get("DPO_PATH") == "supported" else "dpia" if statuses.get("DPIA_PATH") == "supported" else "uncertain"
        attributes = attributes.model_copy(update={"accountability_path": path})
    elements = [_with_fallback_evidence(elements_by_code[code], fallback_evidence_ids) for code in allowed_elements if code in elements_by_code]
    reviewer = reviewer.model_copy(update={"elements": elements, "exclusions": exclusions, "attributes": attributes})
    return reviewer


def _with_fallback_evidence(item, fallback_evidence_ids: list[str]):
    if getattr(item, "status", "") in {"supported", "triggered"} and not getattr(item, "evidence_ids", []):
        return item.model_copy(update={"evidence_ids": fallback_evidence_ids})
    return item


def _migrated_retention_elements(
    task: CandidateTask,
    reviewer: ReviewerDecision,
    attributes: ReviewerAttributes,
    match: IndicatorMatch | None,
) -> dict[str, ReviewerElementAssessment]:
    elements_by_code = {item.element_code: item for item in reviewer.element_assessments}
    base_evidence_ids = _first_reviewer_evidence_ids(reviewer) or list(task.evidence_segments)[:1]
    migrated: dict[str, ReviewerElementAssessment] = {}
    if "MANDATORY_RETENTION_ACTION" not in elements_by_code:
        status = "supported" if _retention_action_present(task, match) and elements_by_code.get("OPERATIVE_RULE", None) and elements_by_code["OPERATIVE_RULE"].status == "supported" else "uncertain"
        migrated["MANDATORY_RETENTION_ACTION"] = ReviewerElementAssessment(
            element_id="MANDATORY_RETENTION_ACTION",
            status=status,
            evidence_ids=base_evidence_ids,
            reason="Derived during reviewer cache migration from an operative retain/keep/preserve/maintain obligation in the focal text.",
        )
    if "CALCULABLE_DURATION" not in elements_by_code:
        duration_supported = elements_by_code.get("MINIMUM_RETENTION_DURATION", None) and elements_by_code["MINIMUM_RETENTION_DURATION"].status == "supported"
        status = "supported" if duration_supported and _calculable_retention_duration_present(task, match) else "uncertain"
        migrated["CALCULABLE_DURATION"] = ReviewerElementAssessment(
            element_id="CALCULABLE_DURATION",
            status=status,
            evidence_ids=base_evidence_ids,
            reason="Derived during reviewer cache migration from a stated minimum retention duration that can be calculated.",
        )
    if "IN_SCOPE_RECORD_TYPE" not in elements_by_code:
        accepted_bases = {
            "PERSONAL_CUSTOMER_USER",
            "ACCOUNT_PAYMENT_TRANSACTION",
            "AML_KYC",
            "ACCOUNTING_TAX_FINANCIAL",
            "COMMUNICATIONS_PLATFORM_DIGITAL_SERVICE",
            "AUTHENTICATION_CYBERSECURITY_SYSTEM_EVENT",
            "PERSON_OR_TRANSACTION_TRACEABILITY",
            "OPERATIONAL_SECTOR_RECORD",
        }
        rejected_bases = {"PHYSICAL_OPERATIONAL_ONLY", "NONE"}
        if attributes.record_scope_basis in accepted_bases:
            status = "supported"
            reason = f"Derived during reviewer cache migration from record_scope_basis={attributes.record_scope_basis}."
        elif attributes.record_scope_basis in rejected_bases:
            status = "not_supported"
            reason = f"Derived during reviewer cache migration from record_scope_basis={attributes.record_scope_basis}."
        else:
            status = "uncertain"
            reason = "Reviewer cache did not contain a conclusive record_scope_basis."
        migrated["IN_SCOPE_RECORD_TYPE"] = ReviewerElementAssessment(
            element_id="IN_SCOPE_RECORD_TYPE",
            status=status,
            evidence_ids=base_evidence_ids,
            reason=reason,
        )
    return migrated


def _first_reviewer_evidence_ids(reviewer: ReviewerDecision) -> list[str]:
    for item in reviewer.element_assessments:
        if item.evidence_ids:
            return list(item.evidence_ids)
    return []


def _retention_action_present(task: CandidateTask, match: IndicatorMatch | None) -> bool:
    text = _strip_notes_for_cache(_fold_for_cache(task.focal_text))
    return any(term in text for term in ("retain", "keep", "preserve", "maintain"))


def _calculable_retention_duration_present(task: CandidateTask, match: IndicatorMatch | None) -> bool:
    text = _strip_notes_for_cache(_fold_for_cache(task.focal_text))
    return bool(re.search(r"\b\d+\s+(day|days|month|months|year|years)\b", text)) or any(
        term in text
        for term in (
            "at least",
            "not less than",
            "after the end",
            "from the date",
            "until the expiry",
            "for a period of",
        )
    )


def _strip_notes_for_cache(text: str) -> str:
    return re.split(r"\bnote\s*:", text, maxsplit=1)[0].strip()


def _looks_government_internal_retention(task: CandidateTask) -> bool:
    text = _fold_for_cache(f"{task.law_title} {task.focal_text} {task.supporting_context}")
    public_terms = (
        r"\bgovernment\s+(?:data|record|records)\b",
        r"\bpublic\s+administration\b",
        r"\bpublic\s+service\b",
        r"\bpublic\s+officer\b",
    )
    internal_terms = (
        r"\binternal\b",
        r"\bofficial\s+record\b",
        r"\badministration\b",
    )
    return any(re.search(pattern, text) for pattern in public_terms) and any(re.search(pattern, text) for pattern in internal_terms)


def _fold_for_cache(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _stable_text_hash(value: str) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = " ".join(text.split())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reviewer_supporting_fingerprint(task: CandidateTask) -> str:
    supporting_rows = []
    for evidence_id in sorted(task.evidence_segments):
        if evidence_id == "E1":
            continue
        supporting_rows.append(f"{evidence_id}:{unicodedata.normalize('NFC', str(task.evidence_segments[evidence_id] or '')).strip()}")
    supporting_rows.append(f"law:{unicodedata.normalize('NFC', str(task.law_title or '')).strip()}")
    return hashlib.sha256("\n".join(supporting_rows).encode("utf-8")).hexdigest()


def _assert_cached_reviewer_compatible(task: CandidateTask, reviewer: ReviewerDecision, match: IndicatorMatch | None = None) -> None:
    claimed_elements = _review_required_elements(task, match)
    allowed_exclusions = _review_allowed_exclusions(task)
    if _decision_missing_in_cache(reviewer):
        raise ValueError("cached reviewer final decision is missing")
    if task.task_kind == "framework_element":
        indicator = str(task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else ""))
        candidate_name = _framework_candidate_element_name(task)
        p4_fields = _p4_framework_fields_from_reviewer(reviewer) if indicator.startswith("P4-") else {}
        payload_candidate = canonical_framework_element(
            indicator,
            p4_fields.get("candidate_element") or reviewer.attributes.framework_candidate_element,
        )
        if not payload_candidate:
            raise ValueError("cached reviewer candidate element is missing")
        if candidate_name and payload_candidate != candidate_name:
            raise ValueError("cached reviewer candidate element is incompatible")
        if reviewer.decision == "match":
            claimed_name = canonical_framework_element(
                indicator,
                p4_fields.get("framework_element") or reviewer.attributes.framework_element,
            )
            if not claimed_name or (candidate_name and claimed_name != candidate_name):
                raise ValueError("cached reviewer claimed a different framework element")
            if not reviewer.attributes.framework_legal_function:
                raise ValueError("cached reviewer framework legal function is missing")
            claimed_code = framework_element_code(indicator, claimed_name)
            if not claimed_code:
                raise ValueError("cached reviewer claimed element code is incompatible")
            matched_assessment = next((item for item in reviewer.element_assessments if item.element_code == claimed_code), None)
            if matched_assessment is None:
                raise ValueError("cached reviewer element set is incompatible")
            if "E1" not in matched_assessment.evidence_ids:
                raise ValueError("cached reviewer focal evidence E1 is incompatible")
        valid_ids = set(task.evidence_segments)
        for item in [*reviewer.element_assessments, *reviewer.exclusion_assessments]:
            if any(evidence_id not in valid_ids for evidence_id in item.evidence_ids):
                raise ValueError("cached reviewer evidence IDs are incompatible")
        return
    element_codes = {item.element_code for item in reviewer.element_assessments}
    if element_codes != set(claimed_elements):
        raise ValueError("cached reviewer element set is incompatible")
    if {item.exclusion_code for item in reviewer.exclusion_assessments} != set(allowed_exclusions):
        raise ValueError("cached reviewer exclusion set is incompatible")
    valid_ids = set(task.evidence_segments)
    for item in [*reviewer.element_assessments, *reviewer.exclusion_assessments]:
        if any(evidence_id not in valid_ids for evidence_id in item.evidence_ids):
            raise ValueError("cached reviewer evidence IDs are incompatible")


def _p4_framework_fields_from_reviewer(reviewer: ReviewerDecision) -> dict[str, str]:
    for check in reviewer.optional_checks:
        if check.check_code != "P4_FRAMEWORK_ELEMENT":
            continue
        try:
            payload = json.loads(check.reason or "{}")
        except json.JSONDecodeError:
            return {}
        if isinstance(payload, dict):
            return {
                "candidate_element": str(payload.get("candidate_element") or ""),
                "framework_element": str(payload.get("framework_element") or ""),
            }
    return {}


def _decision_missing_in_cache(reviewer: ReviewerDecision) -> bool:
    return any(check.check_code == "REVIEWER_DECISION_MISSING_IN_CACHE" for check in reviewer.optional_checks)


def _clean_metadata(value: str, field: str) -> str:
    text = unicodedata.normalize("NFC", str(value))
    markers = ("锟", "�", "Ã", "â€“")
    if any(marker in text for marker in markers):
        raise RuntimeError(f"Output metadata field contains mojibake marker: {field}")
    return text


_VERBATIM_JSON_FIELDS = {
    "verbatim_snippets",
    "verbatim_snippet",
    "quote",
    "evidence_quotes",
}


def _clean_json_metadata(value, field: str = "$"):
    if isinstance(value, dict):
        return {
            key: (item if key in _VERBATIM_JSON_FIELDS else _clean_json_metadata(item, f"{field}.{key}"))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_clean_json_metadata(item, field) for item in value]
    if isinstance(value, str):
        return _clean_metadata(value, field)
    return value


def _human_review_csv_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _assert_review_queue_diagnostics(rows: list[dict]) -> None:
    for row in rows:
        queue_type = row.get("queue_type") or row.get("review_type")
        task_id = row.get("task_id", "<unknown>")
        if queue_type == "human_legal_review":
            if not (row.get("uncertain_elements") or row.get("uncertain_exclusions") or row.get("focal_uncertainty")):
                raise RuntimeError(f"Human legal review missing uncertainty diagnostics: {task_id}")
        if queue_type == "technical_repair":
            if not row.get("technical_detail"):
                raise RuntimeError(f"Technical repair missing technical_detail: {task_id}")
            row.setdefault("expected_repair_action", "inspect_source_or_model_output")


def reviewer_cache_key(
    task: CandidateTask,
    indicator: str,
    review_model: str,
    *,
    evidence_catalog: bool = False,
    required_elements: tuple[str, ...] = (),
) -> str:
    prompt_version = reviewer_prompt_version_for_task(task, evidence_catalog=evidence_catalog)
    framework_candidate = _framework_candidate_element_name(task) if task.task_kind == "framework_element" else ""
    framework_contract = FRAMEWORK_REVIEWER_CONTRACT_VERSION if framework_candidate else ""
    reviewer_schema_version = (
        P4_REVIEWER_SCHEMA_VERSION if task.route_topic.startswith("P4_") else REVIEWER_SCHEMA_VERSION
    )
    focal_fingerprint = _stable_text_hash(task.focal_text)
    supporting_fingerprint = _reviewer_supporting_fingerprint(task)
    return hashlib.sha256(
        "|".join([
            "reviewer",
            task.economy,
            task.task_id,
            task.route_topic,
            indicator,
            framework_candidate,
            task.document_id,
            task.focal_provision_id,
            focal_fingerprint,
            supporting_fingerprint,
            REVIEWER_CACHE_CONTRACT_VERSION,
            framework_contract,
            prompt_version,
            reviewer_schema_version,
            review_model,
        ]).encode("utf-8")
    ).hexdigest()


def _find_match(decision: MappingDecision, record: dict) -> IndicatorMatch | None:
    indicator = record.get("indicator")
    evidence_ids = tuple(record.get("evidence_ids") or ())
    for match in decision.matches:
        if match.indicator == indicator and tuple(match.evidence_ids) == evidence_ids:
            return match
    for match in decision.matches:
        if match.indicator == indicator:
            return match
    return None


def _review_required_elements(task: CandidateTask, match: IndicatorMatch | None = None) -> tuple[str, ...]:
    indicator = str(
        (match.indicator if match else None)
        or task.indicator_id
        or (task.candidate_indicators[0] if task.candidate_indicators else "")
    )
    if task.route_topic.startswith("P4_") and task.task_kind == "framework_element":
        candidate = _framework_candidate_element_code(task)
        return (candidate,) if candidate else tuple()
    if task.route_topic in P4_REVIEW_ELEMENTS_BY_GROUP:
        return P4_REVIEW_ELEMENTS_BY_GROUP[task.route_topic]
    if indicator in {"P4-I2", "P4-I5", "P4-I6", "P4-I10"}:
        candidate = _framework_candidate_element_code(task)
        return (candidate,) if candidate else tuple()
    if task.route_topic == "P6_LOCATION":
        return P6_REVIEW_ELEMENTS
    if task.route_topic == "P6_TREATY":
        return P6_TREATY_REVIEW_ELEMENTS
    if task.route_topic == "P7_DATA_PROTECTION_FRAMEWORK":
        candidate = _framework_candidate_element_code(task)
        return (candidate,) if candidate else tuple()
    if task.route_topic == "P7_CYBERSECURITY_FRAMEWORK":
        candidate = _framework_candidate_element_code(task)
        return (candidate,) if candidate else tuple()
    return P7_REVIEW_ELEMENTS_BY_GROUP.get(task.route_topic, tuple())


def _review_allowed_exclusions(task: CandidateTask) -> tuple[str, ...]:
    indicator = str(task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else ""))
    if task.route_topic.startswith("P4_") and task.task_kind == "framework_element":
        return FRAMEWORK_REVIEW_EXCLUSIONS.get(indicator, tuple())
    if task.route_topic in P4_REVIEW_EXCLUSIONS_BY_GROUP:
        return P4_REVIEW_EXCLUSIONS_BY_GROUP[task.route_topic]
    if task.route_topic == "P6_LOCATION":
        return P6_REVIEW_EXCLUSIONS
    if task.route_topic == "P6_TREATY":
        return P6_TREATY_REVIEW_EXCLUSIONS
    if task.route_topic == "P7_DATA_PROTECTION_FRAMEWORK":
        return FRAMEWORK_REVIEW_EXCLUSIONS["P7-I1"]
    if task.route_topic == "P7_CYBERSECURITY_FRAMEWORK":
        return FRAMEWORK_REVIEW_EXCLUSIONS["P7-I2"]
    return P7_REVIEW_EXCLUSIONS_BY_GROUP.get(task.route_topic, tuple())


def _framework_candidate_element_name(task: CandidateTask) -> str:
    for pattern in task.matched_patterns:
        if pattern.startswith("framework_element:"):
            return canonical_framework_element(task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else ""), pattern.split(":", 1)[1].strip())
    parts = task.task_id.split(":")
    value = parts[-2].strip() if len(parts) >= 2 else ""
    return canonical_framework_element(task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else ""), value)


def _framework_candidate_element_code(task: CandidateTask) -> str:
    indicator = task.indicator_id or (task.candidate_indicators[0] if task.candidate_indicators else "")
    value = _framework_candidate_element_name(task)
    return framework_element_code(indicator, value)


def _remap_match_evidence_ids(match: IndicatorMatch, evidence_id_map: dict[str, str]) -> IndicatorMatch:
    def remap(ids: list[str]) -> list[str]:
        return [evidence_id_map[evidence_id] for evidence_id in ids if evidence_id in evidence_id_map]

    required = [
        item.model_copy(update={"evidence_ids": remap(item.evidence_ids)})
        for item in match.required_element_status
    ]
    return match.model_copy(
        update={
            "evidence_ids": remap(match.evidence_ids),
            "operative_evidence_ids": remap(match.operative_evidence_ids),
            "supporting_evidence_ids": remap(match.supporting_evidence_ids),
            "required_element_status": required,
        }
    )


def _leading_subsection_from_quote(text: str) -> str:
    match = re.match(r"^\s*(\([^()]+\)(?:\([^()]+\))*)", str(text or ""))
    return match.group(1) if match else ""


def _trailing_subparts(provision_id: str) -> str:
    match = re.search(r"((?:\([^()]+\))+)$", str(provision_id or ""))
    return match.group(1) if match else ""


def _strip_trailing_subparts(provision_id: str) -> str:
    return re.sub(r"(?:\([^()]+\))+$", "", str(provision_id or ""))


def _citation_prefix_from_collection(collection: str, law_name: str) -> str:
    folded = f"{collection} {law_name}".casefold()
    if "regulation" in folded or "subsidiary" in folded:
        return "Reg."
    if "rule" in folded:
        return "Rule"
    if "order" in folded:
        return "Order"
    if "schedule" in folded:
        return "Sch."
    return "s."


def _composite_section_number_from_meta(meta: dict, current_number: str) -> str:
    """Recover full Australian composite section tokens such as 382-5."""

    first = re.match(r"^\s*(\d+[A-Z]?)\b", str(current_number or ""))
    if not first:
        return ""
    first_token = first.group(1)
    blob = " ".join(
        str(meta.get(key) or "")
        for key in ("article", "heading", "canonical_citation", "provision_path")
    )
    candidates = re.findall(rf"\b({re.escape(first_token)}\s*-\s*\d+[A-Z]?(?:\s*-\s*\d+[A-Z]?)*)\b", blob)
    if not candidates:
        return ""
    return re.sub(r"\s*-\s*", "-", candidates[-1]).strip()


def _clean_chunk_path(value: str) -> str:
    text = re.sub(r"#chunk\d+\b", "", str(value or ""), flags=re.I).strip()
    return re.sub(r"\s{2,}", " ", text).strip(" /-")


def _prefer_consolidated_principal_evidence(records: list[AtomicEvidenceRecord]) -> list[AtomicEvidenceRecord]:
    """Reconcile highly likely principal/amending duplicates conservatively.

    A title alone is never enough.  A duplicate requires the same indicator,
    same normalized focal text, same target section, different documents, one
    amending signal, and one current-principal signal.  The amending record is
    kept for audit but is no longer a confirmed submission row.
    """
    groups: dict[tuple[str, str, str], list[AtomicEvidenceRecord]] = {}
    for record in records:
        key = (
            str(record.indicator_id or ""),
            hashlib.sha256(_norm_key(record.focal_quote).encode("utf-8", errors="ignore")).hexdigest(),
            _normalized_target_section(record.article),
        )
        groups.setdefault(key, []).append(record)

    reconciled: list[AtomicEvidenceRecord] = []
    for group in groups.values():
        if len({item.document_id for item in group}) < 2:
            reconciled.extend(group)
            continue
        principals = [item for item in group if _current_principal_signal(item)]
        amendments = [item for item in group if _amending_signal(item)]
        if not principals or not amendments:
            reconciled.extend(group)
            continue
        principal = sorted(principals, key=lambda item: (item.instrument_role != "principal", item.law_name, item.document_id))[0]
        for item in group:
            if item is principal:
                reconciled.append(item)
            elif item in amendments:
                reconciled.append(
                    item.model_copy(
                        update={
                            "decision": "human_legal_review",
                            "decision_reason": f"POTENTIAL_PRINCIPAL_AMENDING_DUPLICATE; retained principal evidence_id={principal.evidence_id}",
                        }
                    )
                )
            else:
                reconciled.append(item)
    return reconciled


def _amending_signal(record: AtomicEvidenceRecord) -> bool:
    role = str(record.instrument_role or "").casefold()
    if role == "amending" or record.amends_instrument_id or record.consolidated_target:
        return True
    title = str(record.law_name or "").casefold()
    return "amendment" in title


def _current_principal_signal(record: AtomicEvidenceRecord) -> bool:
    role = str(record.instrument_role or "").casefold()
    if role == "principal":
        return True
    title = str(record.law_name or "").casefold()
    return "amendment" not in title and "repeal" not in title and "consequential" not in title


def _normalized_target_section(article: str) -> str:
    raw = str(article or "")
    matches = re.findall(r"\b(?:s|reg|rule|art)\.?\s*([0-9]+(?:\s*-\s*[0-9A-Z]+)*(?:[A-Z])?(?:\([^)]+\))*)", raw, flags=re.I)
    if matches:
        return _norm_key(re.sub(r"\s*-\s*", "-", matches[-1]))
    text = _norm_key(raw)
    matches = re.findall(r"\b(?:s|reg|rule|art)\s*([0-9]+(?:-[0-9A-Z]+)*(?:[A-Z])?(?:\([^)]+\))*)", text)
    if matches:
        return matches[-1]
    return text


def _direct_mapping_enabled(row: dict) -> bool:
    value = row.get("direct_mapping_enabled")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y"}


def _classify_reviewer_exception(exc: Exception) -> tuple[str, str]:
    class_name = type(exc).__name__
    message = _sanitize_model_error(str(exc))
    folded = f"{class_name} {message}".casefold()
    if "insufficient_quota" in folded or "insufficient quota" in folded:
        code = "reviewer_insufficient_quota"
    elif "ratelimit" in folded or "rate_limit" in folded or "rate limit" in folded or "429" in folded:
        code = "reviewer_rate_limit"
    elif (
        "authentication" in folded
        or "unauthorized" in folded
        or "incorrect api key" in folded
        or "invalid api key" in folded
        or "401" in folded
    ):
        code = "reviewer_authentication"
    elif (
        "model_not_found" in folded
        or "model not found" in folded
        or "model does not exist" in folded
        or "does not have access to model" in folded
        or "404" in folded
        or "notfounderror" in folded
    ):
        code = "reviewer_model_not_found"
    elif (
        "schema" in folded
        or "parse" in folded
        or "validation" in folded
        or "json" in folded
        or "badrequesterror" in folded
    ):
        code = "reviewer_schema_parse_error"
    elif (
        "connection" in folded
        or "timeout" in folded
        or "network" in folded
        or "ssl" in folded
        or "api_connection" in folded
        or "apiconnection" in folded
        or "apitimeout" in folded
        or "api timeout" in folded
    ):
        code = "reviewer_network_error"
    else:
        code = "reviewer_api_error"
    detail = f"{class_name}: {message}" if message else class_name
    return code, detail[:500]


def _reviewer_repair_action(technical_detail: str) -> str:
    if technical_detail == "reviewer_insufficient_quota":
        return "check_openai_billing_or_quota_then_rerun"
    if technical_detail == "reviewer_rate_limit":
        return "wait_or_reduce_concurrency_then_rerun"
    if technical_detail == "reviewer_authentication":
        return "check_OPENAI_API_KEY_then_rerun"
    if technical_detail == "reviewer_model_not_found":
        return "check_reviewer_model_configuration_then_rerun"
    if technical_detail == "reviewer_schema_parse_error":
        return "inspect_reviewer_schema_or_cached_response_then_rerun"
    if technical_detail == "reviewer_network_error":
        return "check_network_then_rerun"
    return "retry_reviewer_or_check_model_configuration"


def _sanitize_model_error(message: str) -> str:
    text = str(message or "")
    text = re.sub(r"sk-(?:proj|svcacct)-[A-Za-z0-9_-]+", "sk-<redacted>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9]{20,}\b", "sk-<redacted>", text)
    text = re.sub(
        r"(?i)(api[-_ ]?key\s*[:=]\s*)([^\s,;]+)",
        r"\1<redacted>",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()
