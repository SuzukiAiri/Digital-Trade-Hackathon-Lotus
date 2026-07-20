"""Minimal model/input validation for Zone 2."""

from __future__ import annotations

from dataclasses import dataclass

MODEL_POLICY_VERSION = "rdtii-model-policy-v3-minimal"


@dataclass(frozen=True)
class StagePolicy:
    stage: str
    model: str
    max_paid_calls_per_task: int
    allow_model: bool
    reason: str = ""


def resolve_model(stage: str, *, input_type: str = "provision", failure_reason: str | None = None) -> StagePolicy:
    stage_key = stage.casefold().strip()
    input_key = input_type.casefold().strip()
    if stage_key in {"mapper", "provision_mapper", "framework_mapper"}:
        return StagePolicy(stage=stage_key, model="", max_paid_calls_per_task=2, allow_model=True)
    if stage_key == "reviewer":
        return StagePolicy(stage=stage_key, model="", max_paid_calls_per_task=2, allow_model=True)
    if stage_key == "pdf_mapper":
        allowed = input_key in {"document_direct", "pdf"}
        return StagePolicy(stage=stage_key, model="", max_paid_calls_per_task=2, allow_model=allowed, reason="" if allowed else "Document-direct mapper is only allowed for document_direct inputs")
    if stage_key == "citation_text":
        return StagePolicy(stage=stage_key, model="text_layer", max_paid_calls_per_task=0, allow_model=True)
    return StagePolicy(stage=stage_key, model="", max_paid_calls_per_task=0, allow_model=False, reason=f"Unknown model stage: {stage}")


def assert_model_allowed(stage: str, model: str, *, input_type: str = "provision") -> None:
    policy = resolve_model(stage, input_type=input_type)
    if not policy.allow_model:
        raise RuntimeError(f"MODEL_POLICY_VIOLATION: {policy.reason}")
    if not str(model or "").strip():
        raise RuntimeError(f"MODEL_POLICY_VIOLATION: empty model name for stage={stage}")
