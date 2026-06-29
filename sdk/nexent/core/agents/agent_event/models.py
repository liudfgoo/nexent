"""Pydantic models for the agent event persistence layer.

Defines the Event envelope, discriminated-union payload types, Session, and Run
models.  Serialized form uses camelCase aliases (matching Claude Code / industry
convention); Python attributes use snake_case with ``populate_by_name=True``.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# EventType enum
# ---------------------------------------------------------------------------

class EventType(str, enum.Enum):
    user_input = "user_input"
    system_prompt = "system_prompt"
    assistant_reasoning = "assistant_reasoning"
    assistant_message = "assistant_message"
    tool_call = "tool_call"
    tool_result = "tool_result"
    compaction_summary = "compaction_summary"
    offload_record = "offload_record"
    error = "error"
    run_lifecycle = "run_lifecycle"


# ---------------------------------------------------------------------------
# Payload models  (each carries a ``kind`` discriminator literal)
# ---------------------------------------------------------------------------

class _PayloadBase(BaseModel):
    """Base for all payload models—allows both snake_case and camelCase input."""
    model_config = ConfigDict(populate_by_name=True)


class UserInputPayload(_PayloadBase):
    kind: Literal["user_input"] = "user_input"
    text: str
    attachments: Optional[List[Any]] = None


class SystemPromptPayload(_PayloadBase):
    kind: Literal["system_prompt"] = "system_prompt"
    rendered_prompt: str = Field(alias="renderedPrompt")
    tool_defs_hash: str = Field(alias="toolDefsHash")
    injected_components: List[str] = Field(default_factory=list, alias="injectedComponents")


class AssistantReasoningPayload(_PayloadBase):
    kind: Literal["assistant_reasoning"] = "assistant_reasoning"
    text: str


class AssistantMessagePayload(_PayloadBase):
    kind: Literal["assistant_message"] = "assistant_message"
    text: str
    is_final_answer: bool = Field(False, alias="isFinalAnswer")


class ToolCallPayload(_PayloadBase):
    kind: Literal["tool_call"] = "tool_call"
    tool_call_id: str = Field(alias="toolCallId")
    tool_name: str = Field(alias="toolName")
    arguments: Dict[str, Any]
    source: Literal["local", "mcp"] = "local"
    mcp_server: Optional[str] = Field(None, alias="mcpServer")


class ToolResultPayload(_PayloadBase):
    kind: Literal["tool_result"] = "tool_result"
    tool_call_id: str = Field(alias="toolCallId")
    output_raw: str = Field(alias="outputRaw")
    output_chars: int = Field(alias="outputChars")
    is_error: bool = Field(False, alias="isError")
    duration_ms: int = Field(0, alias="durationMs")
    offload_handle: Optional[str] = Field(None, alias="offloadHandle")


class CompactionSummaryPayload(_PayloadBase):
    kind: Literal["compaction_summary"] = "compaction_summary"
    structured_summary: Dict[str, str] = Field(alias="structuredSummary")
    covers_event_ids: List[UUID] = Field(alias="coversEventIds")
    covered_pairs: int = Field(alias="coveredPairs")
    end_steps: int = Field(alias="endSteps")
    anchor_fingerprint: str = Field(alias="anchorFingerprint")
    call_type: Literal["full", "incremental"] = Field(alias="callType")
    previous_summary_event_id: Optional[UUID] = Field(None, alias="previousSummaryEventId")
    summarizer_model_id: Optional[str] = Field(None, alias="summarizerModelId")
    input_tokens: Optional[int] = Field(None, alias="inputTokens")
    output_tokens: Optional[int] = Field(None, alias="outputTokens")
    input_chars: Optional[int] = Field(None, alias="inputChars")
    output_chars: Optional[int] = Field(None, alias="outputChars")
    is_active: bool = Field(True, alias="isActive")


class OffloadRecordPayload(_PayloadBase):
    kind: Literal["offload_record"] = "offload_record"
    handle: str
    description: str
    original_chars: int = Field(alias="originalChars")
    preview: str
    content_ref: str = Field(alias="contentRef")
    source_event_id: Optional[UUID] = Field(None, alias="sourceEventId")


class ErrorPayload(_PayloadBase):
    kind: Literal["error"] = "error"
    error_type: str = Field(alias="errorType")
    message: str
    traceback: str = ""
    step_index: Optional[int] = Field(None, alias="stepIndex")


class RunLifecyclePayload(_PayloadBase):
    kind: Literal["run_lifecycle"] = "run_lifecycle"
    action: Literal["started", "ended", "interrupted", "resumed"]
    stop_reason: Optional[str] = Field(None, alias="stopReason")
    render_config: Optional[Dict[str, Any]] = Field(None, alias="renderConfig")


# ---------------------------------------------------------------------------
# Discriminated union
# ---------------------------------------------------------------------------

from typing import Annotated

PayloadUnion = Annotated[
    Union[
        UserInputPayload,
        SystemPromptPayload,
        AssistantReasoningPayload,
        AssistantMessagePayload,
        ToolCallPayload,
        ToolResultPayload,
        CompactionSummaryPayload,
        OffloadRecordPayload,
        ErrorPayload,
        RunLifecyclePayload,
    ],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Event envelope
# ---------------------------------------------------------------------------

class Event(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # -- identity / grouping --
    schema_version: int = Field(SCHEMA_VERSION, alias="schemaVersion")
    event_id: UUID = Field(alias="uuid")
    parent_event_id: Optional[UUID] = Field(None, alias="parentUuid")
    session_id: UUID = Field(alias="sessionId")
    run_id: Optional[UUID] = Field(None, alias="runId")
    turn_id: Optional[UUID] = Field(None, alias="turnId")
    agent_id: str = Field(alias="agentId")
    llm_message_id: Optional[str] = Field(None, alias="llmMessageId")
    seq: int = Field(alias="seq")

    # -- type / role --
    type: EventType
    role: str  # user | assistant | tool | system
    is_sidechain: bool = Field(False, alias="isSidechain")
    sidechain_parent_id: Optional[UUID] = Field(None, alias="sidechainParentId")
    step_index: Optional[int] = Field(None, alias="stepIndex")
    created_at: datetime = Field(alias="timestamp")

    # -- observability --
    model_id: Optional[str] = Field(None, alias="model")
    usage: Optional[Dict[str, Any]] = None
    latency_ms: Optional[int] = Field(None, alias="latencyMs")
    stop_reason: Optional[str] = Field(None, alias="stopReason")

    # -- rendering layer view state --
    superseded_by: Optional[UUID] = Field(None, alias="supersededBy")
    visibility: str = "normal"  # normal | internal | compressed_out

    # -- content --
    payload: PayloadUnion
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> "Event":
        assert self.type.value == self.payload.kind, (
            f"type={self.type.value} does not match payload.kind={self.payload.kind}"
        )
        return self


# ---------------------------------------------------------------------------
# Session & Run
# ---------------------------------------------------------------------------

class Session(BaseModel):
    session_id: UUID
    title: Optional[str] = None
    head_event_id: Optional[UUID] = None
    status: str = "active"
    created_at: datetime
    updated_at: datetime


class Run(BaseModel):
    run_id: UUID
    session_id: UUID
    turn_id: UUID
    agent_id: str
    query: str
    status: str = "running"  # running | completed | interrupted | error
    stop_reason: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    final_answer: Optional[str] = None
    model_config_snapshot: Optional[Dict[str, Any]] = None
    token_totals: Optional[Dict[str, Any]] = None
    log_complete: bool = True


# ---------------------------------------------------------------------------
# BranchResult  (returned by read_branch / read_session with gap detection)
# ---------------------------------------------------------------------------

class BranchResult(BaseModel):
    events: List[Event]
    has_gap: bool = False
    gap_event_id: Optional[UUID] = None
