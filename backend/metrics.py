from __future__ import annotations

from collections import Counter
from threading import Lock


class RuntimeMetrics:
    """Low-cardinality process metrics; export to Prometheus/OTel later."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = Lock()

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[name] += amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)
