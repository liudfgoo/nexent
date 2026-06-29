"""Abstract base class for the agent event persistence store.

All SDK components (ContextManager, OffloadStore, MemoryReconstructor, etc.)
must only depend on this interface—never on a concrete storage backend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from .models import BranchResult, Event, Run, Session


class EventStore(ABC):
    """Narrow interface for event persistence—SDK's only storage abstraction."""

    # -- write ---------------------------------------------------------------

    @abstractmethod
    def append(self, event: Event) -> None:
        """Append an event.  Idempotent: duplicate (run_id, step_index, type,
        tool_call_id) is silently skipped."""
        ...

    # -- read ----------------------------------------------------------------

    @abstractmethod
    def next_seq(self, session_id: UUID) -> int:
        """Return the next monotonically-increasing seq for *session_id*."""
        ...

    @abstractmethod
    def read_session(self, session_id: UUID) -> BranchResult:
        """Read all events for *session_id* in seq order, with gap detection."""
        ...

    @abstractmethod
    def read_branch(self, leaf_event_id: UUID) -> BranchResult:
        """Read the event chain from *leaf_event_id* back to root, with gap
        detection."""
        ...

    # -- session / run -------------------------------------------------------

    @abstractmethod
    def get_session(self, session_id: UUID) -> Session:
        ...

    @abstractmethod
    def upsert_session(self, s: Session) -> None:
        ...

    @abstractmethod
    def get_run(self, run_id: UUID) -> Run:
        ...

    @abstractmethod
    def upsert_run(self, r: Run) -> None:
        ...

    # -- blob storage --------------------------------------------------------

    @abstractmethod
    def put_blob(self, data: str) -> str:
        """Store *data* as a blob and return a content_ref string."""
        ...

    @abstractmethod
    def get_blob(self, content_ref: str) -> str:
        """Retrieve blob content by *content_ref*."""
        ...
