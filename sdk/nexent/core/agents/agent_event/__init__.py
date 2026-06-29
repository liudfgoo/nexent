"""agent_event — Event persistence sub-package for agent conversation logs."""

from .models import (
    AssistantMessagePayload,
    AssistantReasoningPayload,
    BranchResult,
    CompactionSummaryPayload,
    ErrorPayload,
    Event,
    EventType,
    OffloadRecordPayload,
    PayloadUnion,
    Run,
    RunLifecyclePayload,
    SCHEMA_VERSION,
    Session,
    SystemPromptPayload,
    ToolCallPayload,
    ToolResultPayload,
    UserInputPayload,
)
from .render import pick_active_compaction, render
from .store import EventStore

# Lazy import to avoid circular dependency at import time
__all__ = [
    "AssistantMessagePayload",
    "AssistantReasoningPayload",
    "BranchResult",
    "CompactionSummaryPayload",
    "ErrorPayload",
    "Event",
    "EventType",
    "EventStore",
    "JsonlEventStore",
    "OffloadRecordPayload",
    "PayloadUnion",
    "Render",
    "Run",
    "RunLifecyclePayload",
    "SCHEMA_VERSION",
    "Session",
    "SystemPromptPayload",
    "ToolCallPayload",
    "ToolResultPayload",
    "UserInputPayload",
    "pick_active_compaction",
    "render",
]


def __getattr__(name: str):
    if name == "JsonlEventStore":
        from .jsonl_store import JsonlEventStore
        return JsonlEventStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
