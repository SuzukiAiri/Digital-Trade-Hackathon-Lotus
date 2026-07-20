"""Persist configured official SSO URLs."""

from __future__ import annotations

from collections.abc import Iterable

from rdtii_tool.discovery.base import DiscoveryContext
from rdtii_tool.discovery.known_url import KnownURLDiscovery
from rdtii_tool.document_models import CandidateURL
from rdtii_tool.sources.registry import SourceRegistry
from rdtii_tool.storage.candidate_store import CandidateURLStore


class DiscoveryOrchestrator:
    """Run the single active known-URL discovery method."""

    def __init__(
        self,
        registry: SourceRegistry,
        store: CandidateURLStore,
    ) -> None:
        self.registry = registry
        self.store = store
        self.adapter = KnownURLDiscovery()

    def discover(
        self,
        context: DiscoveryContext,
        *,
        methods: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[CandidateURL]:
        selected_methods = tuple(methods or ("known_url",))
        if selected_methods != ("known_url",):
            raise ValueError(
                "The active Singapore pipeline supports only known_url discovery"
            )

        self.store.add_many(self.adapter.discover(context))
        candidates = self.store.candidates
        if limit is not None:
            candidates = self.store.retain_first(limit)
        self.store.write()
        return candidates
