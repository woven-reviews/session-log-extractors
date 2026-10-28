"""Public stage contract for harness-specific session extraction.

Readers own source access and return complete snapshots. Interpreters consume
those snapshots without I/O or export policy, returning the shared model.
Discovery, resource ownership, diagnostics, and writing remain in orchestration;
JSON serialization belongs to the shared exporter, not individual adapters.
"""

from typing import Protocol, TypeVar

from session_log_extractors.session_model import Session

RequestT = TypeVar("RequestT", contravariant=True)
SourceT = TypeVar("SourceT")


class Extractor(Protocol[RequestT, SourceT]):
    """Consistent public stages with harness-specific requests and source types."""

    def read_source(self, request: RequestT) -> SourceT:
        """Snapshot records/assets needed for interpretation; propagate read errors."""
        ...

    def interpret_source(self, source: SourceT) -> Session | None:
        """Interpret without source I/O or caps; return None for no exportable events."""
        ...
