"""Shared Zone 1 progress reporting."""

from __future__ import annotations

import time
from collections import Counter


class Zone1Progress:
    def __init__(self, *, label: str, total: int, interval_seconds: float = 5.0) -> None:
        self.label = label
        self.total = total
        self.interval_seconds = interval_seconds
        self.started = time.monotonic()
        self.last = 0.0
        self.counts: Counter[str] = Counter()

    def add(self, key: str, amount: int = 1) -> None:
        self.counts[key] += amount
        self.maybe_print()

    def maybe_print(self, *, force: bool = False) -> None:
        now = time.monotonic()
        completed = self.counts["success"] + self.counts["failed"] + self.counts["existing_normalized"] + self.counts["existing_raw"]
        if not force and now - self.last < self.interval_seconds and completed < self.total:
            return
        elapsed = max(now - self.started, 0.001)
        speed = completed / elapsed
        print(
            f"{self.label} progress: Discovered={self.total} "
            f"Existing normalized={self.counts['existing_normalized']} "
            f"Existing raw={self.counts['existing_raw']} "
            f"Queued={self.counts['queued']} "
            f"Downloaded={self.counts['downloaded']} "
            f"Normalized={self.counts['normalized']} "
            f"Final failed={self.counts['failed']} "
            f"Pending={max(self.total - completed, 0)} "
            f"Failure events={self.counts['failure_events']} "
            f"speed={speed:.2f}/s",
            flush=True,
        )
        self.last = now

