"""Minimal structured OpenAI client for RDTII mapping."""
from __future__ import annotations
import os
from openai import OpenAI

def parse(model_class: type, prompt: str):
    return OpenAI().responses.parse(model=os.environ["RDTII_LLM_MODEL"], input=prompt, text_format=model_class).output_parsed
