"""Central model and API-key configuration for Zone 2 mapping."""

from __future__ import annotations

import os
from pathlib import Path

from openai import OpenAI


MAPPER_MODEL = "gpt-5.4-nano"
REVIEWER_MODEL = "gpt-5.4-mini"
PDF_MAPPER_MODEL = "gpt-5.4-mini"

def mapper_model_name() -> str:
    return os.environ.get("RDTII_MAPPER_MODEL") or os.environ.get("MAPPER_MODEL") or os.environ.get("RDTII_LLM_MODEL") or MAPPER_MODEL


def reviewer_model_name(default_model: str | None = None) -> str:
    return os.environ.get("RDTII_REVIEW_MODEL") or os.environ.get("REVIEWER_MODEL") or REVIEWER_MODEL or default_model or MAPPER_MODEL


def pdf_mapper_model_name() -> str:
    return os.environ.get("RDTII_PDF_MAPPER_MODEL") or os.environ.get("PDF_MAPPER_MODEL") or PDF_MAPPER_MODEL


def openai_api_key(project_root: Path | None = None) -> str | None:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    return None


def openai_client(project_root: Path | None = None) -> OpenAI:
    key = openai_api_key(project_root)
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required for online model calls.")
    return OpenAI(api_key=key)


def api_key_available(project_root: Path | None = None) -> bool:
    return bool(openai_api_key(project_root))
