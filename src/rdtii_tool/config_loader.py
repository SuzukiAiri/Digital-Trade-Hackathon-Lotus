"""Configuration loading helpers for country and indicator registries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "configs"

COUNTRY_CONFIGS = {
    "SG": "singapore.yaml",
    "AU": "australia.yaml",
}


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Load a YAML file and require a mapping at its root."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)

    if not isinstance(data, dict):
        raise ValueError(f"Configuration root must be a mapping: {config_path}")
    return data


def load_country_config(
    country_code: str,
    config_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Load the registry for a supported ISO alpha-2 country code."""
    normalized_code = country_code.upper()
    try:
        filename = COUNTRY_CONFIGS[normalized_code]
    except KeyError as exc:
        supported = ", ".join(sorted(COUNTRY_CONFIGS))
        raise ValueError(
            f"Unsupported country code {country_code!r}. Supported codes: {supported}"
        ) from exc

    directory = Path(config_dir) if config_dir is not None else CONFIG_DIR
    config = load_yaml(directory / filename)

    configured_code = str(config.get("country", {}).get("code", "")).upper()
    if configured_code != normalized_code:
        raise ValueError(
            f"Country config code mismatch: expected {normalized_code}, "
            f"found {configured_code or 'missing'}"
        )
    return config


def load_indicator_config(
    config_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Load the RDTII Pillar 6/7 indicator registry."""
    directory = Path(config_dir) if config_dir is not None else CONFIG_DIR
    config = load_yaml(directory / "indicators_p6_p7.yaml")
    indicators = config.get("indicators")
    if not isinstance(indicators, list):
        raise ValueError("Indicator config must contain an 'indicators' list")
    return config
