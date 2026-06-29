"""MemoryReconstructor: rebuild CoreAgent memory from persisted events.

Level 0 (lossy): equivalent to the old ``add_history_to_agent`` — only
TaskStep + ActionStep text, no tool_calls/observations/compression/offload.

Level 1 (faithful): full reconstruction with compression continuity and
offload recovery.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from smolagents.memory import ActionStep, TaskStep, Timing, TokenUsage, ToolCall

from ..summary_cache import PreviousSummaryCache
from .models import (
    BranchResult,
    CompactionSummaryPayload,
    Event,
    EventType,
    ToolCallPayload,
    ToolResultPayload,
)
from .render import pick_active_compaction

logger = logging.getLogger("agent_event.reconstructor")


class MemoryReconstructor:
    """Rebuild CoreAgent memory / compression caches / offload store from events."""

    def reconstruct(
        self,
        agent,  # CoreAgent — typed loosely to avoid circular import
        events: list[Event] | BranchResult,
        fidelity: Literal["lossy", "faithful"] = "lossy",
        event_store=None,  # EventStore — needed for Level 1 blob loading
    ) -> None:
        """Reconstruct agent memory from persisted events.

        Parameters
        ----------
        agent:
            The CoreAgent whose memory to populate.
        events:
            Either a list of Event or a BranchResult (with gap info).
        fidelity:
            "lossy" = Level 0 (text only), "faithful" = Level 1 (full).
        event_store:
            Required for Level 1 to load offload blobs.
        """
        if isinstance(events, BranchResult):
            branch_result = events
            event_list = branch_result.events
        else:
            branch_result = BranchResult(events=events)
            event_list = events

        if fidelity == "lossy":
            self._reconstruct_level0(agent, event_list)
        elif fidelity == "faithful":
            try:
                self._reconstruct_level1(agent, event_list, branch_result, event_store)
            except Exception:
                logger.warning(
                    "Level 1 reconstruction failed, falling back to Level 0",
                    exc_info=True,
                )
                self._reconstruct_level0(agent, event_list)
        else:
            raise ValueError(f"Unknown fidelity: {fidelity}")

    # ------------------------------------------------------------------
    # Level 0 — lossy (equivalent to old add_history_to_agent)
    # ------------------------------------------------------------------

    @staticmethod
    def _reconstruct_level0(agent, events: list[Event]) -> None:
        """Populate memory with TaskStep/ActionStep text only.

        Reconstructed ActionSteps preserve the original step_index as their
        step_number so that the resumed agent continues step_number from the
        correct point (max original step_index + 1) rather than from a
        sequentially-assigned number that creates gaps or overlaps in the
        stepIndex space.
        """
        agent.memory.reset()
        for ev in events:
            if ev.type == EventType.user_input:
                agent.memory.steps.append(TaskStep(task=ev.payload.text))  # type: ignore[union-attr]
            elif ev.type == EventType.assistant_message:
                if ev.payload.is_final_answer or True:  # type: ignore[union-attr]
                    # In Level 0, every assistant_message becomes an ActionStep.
                    # Use original step_index as step_number to preserve continuity.
                    original_step_index = ev.step_index if ev.step_index is not None else 1
                    agent.memory.steps.append(
                        ActionStep(
                            step_number=original_step_index,
                            timing=Timing(start_time=0),
                            action_output=ev.payload.text,  # type: ignore[union-attr]
                            model_output=ev.payload.text,  # type: ignore[union-attr]
                        )
                    )
        agent._history_step_count = len(agent.memory.steps)

    # ------------------------------------------------------------------
    # Level 1 — faithful reconstruction
    # ------------------------------------------------------------------

    @staticmethod
    def _reconstruct_level1(
        agent,
        events: list[Event],
        branch_result: BranchResult,
        event_store,
    ) -> None:
        """Full reconstruction from events.

        1. Group events by step_index / llm_message_id
        2. Rebuild ActionSteps with tool_calls, observations, token_usage
        3. Restore PreviousSummaryCache from valid compaction_summary
        4. Restore OffloadStore from offload_record events
        5. Handle structural gaps: degrade after gap_event_id to Level 0
        """
        # -- Gap detection: determine split point --
        gap_idx = None
        if branch_result.has_gap and branch_result.gap_event_id is not None:
            for i, ev in enumerate(events):
                if ev.event_id == branch_result.gap_event_id:
                    gap_idx = i
                    break

        # Split events: faithful part + degraded part
        if gap_idx is not None:
            faithful_events = events[:gap_idx + 1]
            degraded_events = events[gap_idx + 1:]
        else:
            faithful_events = events
            degraded_events = []

        agent.memory.reset()

        # -- Extract render_config from run_lifecycle(started) --
        render_config = _extract_render_config(events)

        # -- Group faithful events by step_index --
        step_groups: Dict[int, List[Event]] = defaultdict(list)
        current_user_input: Optional[str] = None
        step_counter = 0

        for ev in faithful_events:
            if ev.type == EventType.user_input:
                # Start a new TaskStep
                if current_user_input is not None or step_groups:
                    # Flush previous group
                    step_counter = _flush_step_group(
                        agent, step_groups, step_counter,
                        current_user_input, render_config, event_store,
                    )
                    step_groups = defaultdict(list)
                current_user_input = ev.payload.text  # type: ignore[union-attr]
            elif ev.type == EventType.system_prompt:
                # System prompt is not part of memory steps
                continue
            elif ev.type == EventType.run_lifecycle:
                continue
            elif ev.type == EventType.compaction_summary:
                # Handled separately after all steps
                continue
            elif ev.type == EventType.offload_record:
                # Handled separately for OffloadStore recovery
                continue
            else:
                si = ev.step_index
                if si is not None:
                    step_groups[si].append(ev)
                else:
                    # Events without step_index get their own group
                    step_groups[step_counter + len(step_groups)].append(ev)

        # Flush remaining step group
        if current_user_input is not None or step_groups:
            step_counter = _flush_step_group(
                agent, step_groups, step_counter,
                current_user_input, render_config, event_store,
            )

        # -- Handle degraded events (after gap) at Level 0 --
        for ev in degraded_events:
            if ev.type == EventType.user_input:
                agent.memory.steps.append(TaskStep(task=ev.payload.text))  # type: ignore[union-attr]
            elif ev.type == EventType.assistant_message:
                original_step_index = ev.step_index if ev.step_index is not None else 1
                agent.memory.steps.append(
                    ActionStep(
                        step_number=original_step_index,
                        timing=Timing(start_time=0),
                        action_output=ev.payload.text,  # type: ignore[union-attr]
                        model_output=ev.payload.text,  # type: ignore[union-attr]
                    )
                )

        # -- Restore compaction summary → PreviousSummaryCache --
        _restore_compaction(agent, events)

        # -- Restore OffloadStore from offload_record events --
        _restore_offload(agent, events, event_store)

        agent._history_step_count = len(agent.memory.steps)


# ---------------------------------------------------------------------------
# Internal helpers for Level 1
# ---------------------------------------------------------------------------

def _extract_render_config(events: list[Event]) -> Dict[str, Any]:
    """Extract render_config from the last run_lifecycle(started) event."""
    for ev in reversed(events):
        if ev.type == EventType.run_lifecycle:
            payload = ev.payload  # type: ignore[assignment]
            if payload.action == "started" and payload.render_config:  # type: ignore[union-attr]
                return payload.render_config  # type: ignore[union-attr]
    return {}


def _flush_step_group(
    agent,
    step_groups: Dict[int, List[Event]],
    step_counter: int,
    user_input: Optional[str],
    render_config: Dict[str, Any],
    event_store,
) -> int:
    """Build and append TaskStep + ActionStep(s) from grouped events.

    Returns the updated step_counter.
    """
    # Add the TaskStep
    if user_input is not None:
        agent.memory.steps.append(TaskStep(task=user_input))
        step_counter += 1

    # Build ActionSteps from each step group.
    # Use the original step_index (si) as step_number to preserve stepIndex
    # continuity when the agent resumes — the resumed agent needs
    # step_number values that match the original event step_index space.
    for si in sorted(step_groups.keys()):
        group_events = step_groups[si]
        action = _build_action_step(group_events, si, render_config, event_store)
        if action is not None:
            agent.memory.steps.append(action)
            step_counter += 1

    return step_counter


def _build_action_step(
    group_events: List[Event],
    step_number: int,
    render_config: Dict[str, Any],
    event_store,
) -> Optional[ActionStep]:
    """Reconstruct a single ActionStep from its constituent events.

    step_number is set to the original step_index value from the persisted
    events, preserving stepIndex continuity on resume.

    Per-step degrade: if tool_call/tool_result events are missing or
    malformed, fall back to text-only reconstruction for that step.
    """
    model_output = None
    model_output_message = None
    tool_calls = []
    observations = None
    token_usage = None
    error = None
    is_final_answer = False
    code_action = None
    invoked_tool_signatures = []

    for ev in group_events:
        if ev.type == EventType.assistant_message:
            payload = ev.payload  # type: ignore[assignment]
            model_output = payload.text
            is_final_answer = getattr(payload, 'is_final_answer', False)
            # Reconstruct a minimal model_output_message
            from smolagents.models import ChatMessage, MessageRole
            model_output_message = ChatMessage(
                role=MessageRole.ASSISTANT,
                content=[{"type": "text", "text": payload.text}],
            )
        elif ev.type == EventType.assistant_reasoning:
            # Reasoning is part of model_output for code agents
            pass
        elif ev.type == EventType.tool_call:
            payload: ToolCallPayload = ev.payload  # type: ignore[assignment]
            tc = ToolCall(
                name=payload.tool_name,
                arguments=payload.arguments,
                id=payload.tool_call_id,
            )
            tool_calls.append(tc)
            if payload.tool_name == "python_interpreter":
                code_action = payload.arguments
            invoked_tool_signatures.append(f"{payload.tool_name}(...)")
        elif ev.type == EventType.tool_result:
            payload: ToolResultPayload = ev.payload  # type: ignore[assignment]
            if payload.offload_handle and event_store is not None:
                # Try to load blob from offload storage
                try:
                    content_ref = getattr(payload, 'content_ref', None)
                    blob = None
                    if content_ref:
                        blob = event_store.get_blob(content_ref)
                    if blob:
                        observations = blob.decode('utf-8', errors='replace')
                    else:
                        observations = f"[[OFFLOADED:{payload.offload_handle}]]"
                except Exception:
                    observations = f"[[OFFLOADED:{payload.offload_handle}]]"
            else:
                observations = payload.output_raw
            # Truncate observations using render_config
            max_obs = render_config.get("max_observation_length", 0)
            if max_obs > 0 and observations and len(observations) > max_obs:
                half = (max_obs - 50) // 2
                observations = observations[:half] + "\n...[truncated]...\n" + observations[-half:]
        elif ev.type == EventType.error:
            payload = ev.payload  # type: ignore[assignment]
            error = payload.message
        # token_usage is on the assistant_message event
        if ev.type == EventType.assistant_message and ev.usage:
            tu = ev.usage
            token_usage = TokenUsage(
                input_tokens=tu.get("input_tokens", 0),
                output_tokens=tu.get("output_tokens", 0),
                total_tokens=tu.get("total_tokens", 0),
            )

    # If we have no useful data, skip this step
    if model_output is None and not tool_calls and observations is None:
        return None

    step = ActionStep(
        step_number=step_number,
        timing=Timing(start_time=0),
        model_output=model_output,
        model_output_message=model_output_message,
        code_action=code_action,
        tool_calls=tool_calls if tool_calls else None,
        observations=observations,
        token_usage=token_usage,
        is_final_answer=is_final_answer,
    )
    # Set invoked_tool_signatures if available (used by context manager)
    if invoked_tool_signatures:
        step.invoked_tool_signatures = invoked_tool_signatures

    return step


def _restore_compaction(agent, events: list[Event]) -> None:
    """Restore PreviousSummaryCache from valid compaction_summary events."""
    ctx_mgr = getattr(agent, 'context_manager', None)
    if ctx_mgr is None:
        return

    valid_summary = pick_valid_summary(events)
    if valid_summary is None:
        return

    payload: CompactionSummaryPayload = valid_summary.payload  # type: ignore[assignment]
    summary_text = payload.structured_summary.get("summary", "")
    if not summary_text:
        # Try to concatenate all values
        summary_text = "\n".join(str(v) for v in payload.structured_summary.values())

    ctx_mgr._previous_summary_cache = PreviousSummaryCache(
        summary_text=summary_text,
        covered_pairs=payload.covered_pairs,
        anchor_fingerprint=payload.anchor_fingerprint,
    )

    # Insert a SummaryTaskStep at the beginning of memory if not already present
    from ..agent_context.summary_step import SummaryTaskStep
    if ctx_mgr._previous_summary_cache is not None:
        # Check if first step is already a SummaryTaskStep
        has_summary = False
        for step in agent.memory.steps:
            if isinstance(step, SummaryTaskStep):
                has_summary = True
                break
        if not has_summary and agent.memory.steps:
            # Insert before the first ActionStep that follows a TaskStep
            insert_pos = 0
            for i, step in enumerate(agent.memory.steps):
                if isinstance(step, TaskStep) and not isinstance(step, SummaryTaskStep):
                    insert_pos = i + 1
                    break
            summary_step = SummaryTaskStep(
                task=summary_text,
                prefix="Summary of earlier steps in this task:",
            )
            agent.memory.steps.insert(insert_pos, summary_step)


def _restore_offload(agent, events: list[Event], event_store) -> None:
    """Restore OffloadStore from offload_record events."""
    ctx_mgr = getattr(agent, 'context_manager', None)
    if ctx_mgr is None:
        return

    offload_store = getattr(ctx_mgr, '_offload_store', None)
    if offload_store is None:
        return

    for ev in events:
        if ev.type != EventType.offload_record:
            continue
        payload = ev.payload  # type: ignore[assignment]
        handle = payload.handle
        description = payload.description
        # Try to load blob content
        content = None
        content_ref = getattr(payload, 'content_ref', None)
        if content_ref and event_store is not None:
            try:
                content = event_store.get_blob(content_ref)
            except Exception:
                logger.debug("Blob load failed for handle %s", handle, exc_info=True)

        if content is None:
            # Use preview as fallback content
            content = getattr(payload, 'preview', '') or description or ""

        # Directly insert into OffloadStore's internal dict to bypass
        # size limits (the original content was already accepted once)
        with offload_store._lock:
            offload_store._store[handle] = type(offload_store)._Entry(  # type: ignore[attr-defined]
                content=content,
                description=description,
                tokens=type(offload_store)._tokenize(description),  # type: ignore[attr-defined]
            )
            offload_store._current_total += len(content)


# ---------------------------------------------------------------------------
# Shared utility: pick_valid_summary (used by both Level 1 and compression)
# ---------------------------------------------------------------------------

def pick_valid_summary(
    events: list[Event],
    branch_event_ids: set[UUID] | None = None,
) -> Optional[Event]:
    """Walk the compaction chain from tail to oldest, return the *deepest*
    summary whose ``covers_event_ids`` is a subset of *branch_event_ids*
    (fingerprint validation).

    If *branch_event_ids* is None, validate against all event IDs in *events*.

    Returns None if no valid summary is found.
    """
    if branch_event_ids is None:
        branch_event_ids = {ev.event_id for ev in events}

    summaries = [
        ev for ev in events
        if ev.type == EventType.compaction_summary
    ]

    for ev in reversed(summaries):  # deepest first
        payload = ev.payload  # type: ignore[assignment]
        covers_set = set(payload.covers_event_ids)
        if covers_set <= branch_event_ids:
            # Verify fingerprint
            from .render import _fingerprint_event_ids
            actual_fp = _fingerprint_event_ids(payload.covers_event_ids)
            if actual_fp == payload.anchor_fingerprint:
                return ev

    return None
