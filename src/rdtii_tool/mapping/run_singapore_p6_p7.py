"""Command-line entry point for Singapore P6/P7 output rebuilds.

This module deliberately exposes only rebuild/cache-replay semantics. Mapper LLM
calls remain disabled inside :class:`MappingPipeline`; final outputs are derived
from mapper cache, valid reviewer cache or refreshed reviewer calls.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .pipeline import MappingPipeline


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild Singapore P6/P7 mapping outputs from versioned caches")
    parser.add_argument("--rebuild-output", action="store_true", required=True)
    parser.add_argument("--reuse-mapper-cache", action="store_true", required=True)
    parser.add_argument("--reuse-valid-reviewer-cache", action="store_true", required=True)
    args = parser.parse_args(argv)
    if not (args.rebuild_output and args.reuse_mapper_cache and args.reuse_valid_reviewer_cache):
        raise RuntimeError("Only full rebuild with mapper-cache replay and valid reviewer-cache reuse is supported")
    summary = MappingPipeline(PROJECT_ROOT / "outputs" / "corpus" / "singapore", pillars={6, 7}).run()
    print("Singapore P6/P7 mapping rebuild completed")
    print(f"Accepted measures: {summary['accepted_measures']}")
    print(f"Technical repair tasks: {summary['technical_repair_tasks']}")
    print(f"Mapper calls: {summary['mapper_calls']}")
    print(f"Mapper cache hits: {summary['mapper_cache_hits']}")
    print(f"Reviewer calls: {summary['reviewer_calls']}")
    print(f"Reviewer cache hits: {summary['reviewer_cache_hits']}")
    print(f"Current output: {PROJECT_ROOT / 'outputs/corpus/singapore/mappings/current/singapore_p6_p7_mapping_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
