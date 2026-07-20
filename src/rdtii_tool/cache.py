"""Filesystem cache helpers for downloaded legal-source HTML."""

from __future__ import annotations

import re
import unicodedata


WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def safe_filename(
    value: str,
    *,
    suffix: str = ".html",
    max_stem_length: int = 100,
) -> str:
    """Convert an arbitrary title into a deterministic, path-safe filename."""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9]+", "_", ascii_value).strip("._ ").lower()
    stem = stem[:max_stem_length].rstrip("._ ")

    if not stem:
        stem = "document"
    if stem.upper() in WINDOWS_RESERVED_NAMES:
        stem = f"document_{stem}"

    normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    return f"{stem}{normalized_suffix}"
