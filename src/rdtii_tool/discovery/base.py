"""Context shared by configured known-URL discovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rdtii_tool.sources.registry import SourceRegistry


@dataclass(slots=True)
class DiscoveryContext:
    """Country configuration and normalized source registry."""

    country_config: dict[str, Any]
    registry: SourceRegistry
