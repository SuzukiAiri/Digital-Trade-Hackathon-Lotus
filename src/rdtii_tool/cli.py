"""Command-line entry point for RDTII corpus and mapping workflows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from rdtii_tool.config_loader import PROJECT_ROOT
from rdtii_tool.zone1_standardizer import standardize_zone1_corpus


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rdtii_tool",
        description="RDTII corpus and mapping tooling.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("build-corpus", "map-rdtii", "final-audit", "export-submission", "demo"),
        help="Use `build-corpus`, `map-rdtii`, `final-audit`, `export-submission`, or `demo`.",
    )
    return parser


def _corpus_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rdtii_tool build-corpus",
        description="Build one economy's complete Zone 1 official legal-source corpus.",
    )
    parser.add_argument("--economy", required=True, choices=("singapore", "australia", "malaysia"))
    parser.add_argument("--zone", type=int, choices=(1,), default=1)
    return parser

def _mapping_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rdtii_tool map-rdtii")
    parser.add_argument("--economy", required=True, choices=("singapore", "australia", "malaysia"))
    parser.add_argument("--pillars", nargs="+", type=int, default=[6, 7])
    parser.add_argument("--live", action="store_true", help="Compatibility alias; standard map-rdtii already processes stale/cache-miss tasks when an API key is available.")
    parser.add_argument("--cache-only", action="store_true", help="Replay using existing Mapper/Reviewer/PDF caches only; never call online models.")
    return parser


def _final_audit_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rdtii_tool final-audit")
    parser.add_argument("--economy", required=True, choices=("singapore", "australia", "malaysia"))
    parser.add_argument("--pillars", nargs="+", type=int, default=[6, 7])
    return parser


def _export_submission_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rdtii_tool export-submission")
    parser.add_argument("--economies", nargs="+", required=True, choices=("singapore", "australia", "malaysia"))
    parser.add_argument("--pillars", nargs="+", type=int, default=[6, 7])
    return parser


def _demo_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rdtii_tool demo")
    parser.add_argument("--mode", choices=("offline", "pdf"), default="offline")
    parser.add_argument("--output-dir", required=True)
    return parser


def _supported_pillar_combination(pillars: set[int]) -> bool:
    return pillars in ({4}, {6, 7})


def _run_build_corpus(argv: Sequence[str]) -> int:
    args = _corpus_parser().parse_args(argv)
    if args.economy == "malaysia":
        if not _zone1_cache_exists("malaysia"):
            from rdtii_tool.malaysia_corpus import MalaysiaZone1Builder

            MalaysiaZone1Builder(PROJECT_ROOT).build_zone1()
        summary = standardize_zone1_corpus(PROJECT_ROOT, "malaysia").as_dict()
        _assert_zone1_ready("malaysia", summary)
        print("Malaysia Zone 1 corpus build completed")
        print(f"zone1_documents: {summary['documents_count']}")
        print(f"zone1_provision_files: {summary['provision_files_count']}")
        print(f"zone1_mapping_records: {summary['provisions_count']}")
        print(f"Excluded source_unavailable: {summary['excluded_source_unavailable_count']}")
        print(f"Fallback chunks: {summary['fallback_chunk_count']}")
        print("Zone 2 invoked: no")
        print(f"Output: {PROJECT_ROOT / 'outputs/corpus/malaysia'}")
        return 0

    if args.economy == "australia":
        if not _zone1_cache_exists("australia"):
            from rdtii_tool.australia_corpus import AustraliaFRLCorpusBuilder

            AustraliaFRLCorpusBuilder(PROJECT_ROOT, download_all=True).build_zone1()
        if not _australia_legacy_provisions_exist():
            from rdtii_tool.australia_corpus import AustraliaFRLCorpusBuilder

            AustraliaFRLCorpusBuilder(PROJECT_ROOT).build_zone1_provisions()
        summary = standardize_zone1_corpus(PROJECT_ROOT, "australia").as_dict()
        _assert_zone1_ready("australia", summary)
        print("Australia Zone 1 corpus build completed")
        print(f"zone1_documents: {summary['documents_count']}")
        print(f"zone1_provision_files: {summary['provision_files_count']}")
        print(f"zone1_mapping_records: {summary['provisions_count']}")
        print(f"Fallback chunks: {summary['fallback_chunk_count']}")
        print(f"Excluded stale/extra artifacts: {summary['excluded_stale_extra_artifact_count']}")
        print("Zone 2 invoked: no")
        print(f"Output: {PROJECT_ROOT / 'outputs/corpus/australia'}")
        return 0

    if not _zone1_cache_exists("singapore"):
        from rdtii_tool.corpus_builder import SingaporeCorpusBuilder

        SingaporeCorpusBuilder(PROJECT_ROOT / "outputs" / "corpus" / "singapore").build({6, 7})
    summary = standardize_zone1_corpus(PROJECT_ROOT, "singapore").as_dict()
    _assert_zone1_ready("singapore", summary)
    print("Singapore corpus build completed")
    print(f"zone1_documents: {summary['documents_count']}")
    print(f"zone1_provision_files: {summary['provision_files_count']}")
    print(f"zone1_mapping_records: {summary['provisions_count']}")
    print(f"Excluded duplicate/alias: {summary['excluded_duplicate_alias_count']}")
    print(f"Excluded known gaps: {summary['excluded_known_gaps_count']}")
    return 0

def _run_mapping(argv: Sequence[str]) -> int:
    args = _mapping_parser().parse_args(argv)
    pillars = set(args.pillars)
    if not _supported_pillar_combination(pillars):
        raise ValueError("unsupported pillar combination")
    root = _ensure_mapping_corpus(args.economy, pillars)
    from rdtii_tool.mapping.pipeline import MappingPipeline
    economy_title = {"australia": "Australia", "singapore": "Singapore", "malaysia": "Malaysia"}[args.economy]
    try:
        summary = MappingPipeline(root, economy=economy_title, pillars=pillars, live=not args.cache_only).run()
    except RuntimeError as exc:
        message = str(exc)
        marker = "Run validation failed; staging moved to "
        if marker not in message:
            raise
        failed_dir = message.split(marker, 1)[1].strip()
        print("Mapping failed validation.", file=sys.stderr)
        print("Failed run saved to:", file=sys.stderr)
        print(failed_dir, file=sys.stderr)
        print("See run_validation_report.json for details.", file=sys.stderr)
        return 1
    scope_label = "P4" if pillars == {4} else "P6/P7"
    prefix = f"{args.economy}_{'p4' if pillars == {4} else 'p6_p7'}"
    warnings = _mapping_warning_count(summary)
    status = "completed_with_warnings" if warnings else "completed"
    print(f"{economy_title} {scope_label} mapping {status}")
    print(f"Provisions loaded: {summary['provisions_loaded']}")
    print(f"Provisions scanned: {summary['provisions_scanned']}")
    print(f"Candidate tasks: {summary['candidate_tasks']}")
    print(f"Accepted tasks: {summary['accepted_tasks']}")
    print(f"Accepted measures: {summary['accepted_measures']}")
    print(f"Review tasks: {summary['human_legal_review_tasks']}")
    print(f"Technical repair tasks: {summary['technical_repair_tasks']}")
    print(f"External source tasks: {summary['external_source_tasks']}")
    print(f"Rejected tasks: {summary['rejected_tasks']}")
    print(f"Framework measures: {summary['framework_measures']}")
    print(f"Mapper calls: {summary['mapper_calls']}")
    print(f"Mapper cache hits: {summary['mapper_cache_hits']}")
    print(f"Reviewer calls: {summary['reviewer_calls']}")
    print(f"Reviewer cache hits: {summary['reviewer_cache_hits']}")
    if warnings:
        print(f"Warnings: {warnings}")
        print(f"Technical repair queue: {root / ('mappings/p4/current' if pillars == {4} else 'mappings/current') / 'technical_repair_queue.jsonl'}")
    if pillars == {4}:
        print(f"Atomic evidence: {root / 'mappings/p4/current/atomic_evidence.jsonl'}")
        print(f"Submission CSV: {root / 'submission/p4' / f'{prefix}.csv'}")
    else:
        print(f"Atomic evidence: {root / 'mappings/current/atomic_evidence.jsonl'}")
        print(f"Submission CSV: {root / 'submission' / f'{prefix}.csv'}")
    return 0


def _mapping_warning_count(summary: dict) -> int:
    return (
        int(summary.get("technical_repair_tasks") or 0)
        + int(summary.get("pdf_documents_failed") or 0)
        + int(summary.get("pdf_documents_pending_live_processing") or 0)
        + int(summary.get("model_review_pending_tasks") or 0)
    )


def _assert_zone1_ready(economy: str, summary: dict) -> None:
    documents = int(summary.get("documents_count") or 0)
    provisions = int(summary.get("provisions_count") or 0)
    failures: list[str] = []
    if documents <= 0:
        failures.append("zero_documents")
    if provisions <= 0:
        failures.append("zero_provisions")
    report = PROJECT_ROOT / "outputs" / "corpus" / economy / "download_report.json"
    if report.exists():
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failures.append("download_report_unreadable")
    if failures:
        raise RuntimeError(
            f"{economy} Zone 1 corpus is not complete enough for mapping: {', '.join(failures)}"
        )


def _run_final_audit(argv: Sequence[str]) -> int:
    args = _final_audit_parser().parse_args(argv)
    if not _supported_pillar_combination(set(args.pillars)):
        raise ValueError("unsupported pillar combination")
    from rdtii_tool.mapping.final_audit import run_final_audit

    summary = run_final_audit(PROJECT_ROOT, args.economy, set(args.pillars))
    print(f"{args.economy} final audit prepared")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _run_export_submission(argv: Sequence[str]) -> int:
    args = _export_submission_parser().parse_args(argv)
    if not _supported_pillar_combination(set(args.pillars)):
        raise ValueError("unsupported pillar combination")
    from rdtii_tool.mapping.offline_export import export_completed_submissions

    summary = export_completed_submissions(PROJECT_ROOT, list(args.economies), set(args.pillars))
    print("Offline submission export completed")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _run_demo(argv: Sequence[str]) -> int:
    args = _demo_parser().parse_args(argv)
    from rdtii_tool.demo import run_demo

    summary = run_demo(mode=args.mode, output_dir=Path(args.output_dir))
    print("RDTII demo completed")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _ensure_mapping_corpus(economy: str, pillars: set[int]) -> Path:
    root = PROJECT_ROOT / "outputs" / "corpus" / economy
    if economy in {"singapore", "australia", "malaysia"}:
        provision_manifest = root / "zone1_provisions_manifest.jsonl"
        if not provision_manifest.exists() or not provision_manifest.stat().st_size:
            raise RuntimeError(
                "Zone 1 per-document provision manifest missing. Run: "
                f"python -m rdtii_tool build-corpus --economy {economy} --zone 1"
            )
        return root

    raise ValueError(f"Unsupported mapping economy: {economy}")


def _zone1_cache_exists(economy: str) -> bool:
    root = PROJECT_ROOT / "outputs" / "corpus" / economy
    data_root = PROJECT_ROOT / "data" / "legal_sources" / economy / "manifests"
    if economy == "singapore":
        return (
            (root / "zone1_input_manifest.jsonl").exists()
            or (root / "manifests" / "acts_manifest.jsonl").exists()
            or (data_root / "zone1_input_manifest.jsonl").exists()
        )
    if economy == "australia":
        return (
            (data_root / "australia_downloaded_manifest.jsonl").exists()
            or (root / "zone1_documents.jsonl").exists()
        )
    if economy == "malaysia":
        return (
            (data_root / "zone1_input_manifest.jsonl").exists()
            or (data_root / "malaysia_zone1_manifest.jsonl").exists()
            or (root / "zone1_input_manifest.jsonl").exists()
        )
    return False


def _australia_legacy_provisions_exist() -> bool:
    root = PROJECT_ROOT / "outputs" / "corpus" / "australia"
    return (
        (root / "zone1_provisions_manifest.jsonl").exists()
        or (root / "manifests" / "acts_manifest.jsonl").exists()
        or (root / "manifests" / "subsidiary_manifest.jsonl").exists()
    )


def main(argv: Sequence[str] | None = None) -> int:
    argv = tuple(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "build-corpus":
        return _run_build_corpus(argv[1:])
    if argv and argv[0] == "map-rdtii":
        return _run_mapping(argv[1:])
    if argv and argv[0] == "final-audit":
        return _run_final_audit(argv[1:])
    if argv and argv[0] == "export-submission":
        return _run_export_submission(argv[1:])
    if argv and argv[0] == "demo":
        return _run_demo(argv[1:])
    parser = build_parser()
    if argv and argv[0] in {"-h", "--help"}:
        parser.print_help()
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
