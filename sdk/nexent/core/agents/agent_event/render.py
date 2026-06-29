"""Render Layer-1 events into Layer-2 messages (design_overall.md S5).

This is a pure function: ``events + compaction_state + offload_state  ->  messages``.
Truncation, offload markers, and compaction summaries are applied here—not in
the event log.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from .models import (
    CompactionSummaryPayload,
    Event,
    EventType,
    ToolResultPayload,
)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def pick_active_compaction(events: List[Event]) -> Optional[CompactionSummaryPayload]:
    """Find the deepest *is_active* compaction_summary whose
    ``anchor_fingerprint`` matches the fingerprint of its ``covers_event_ids``.

    Returns ``None`` if no valid active compaction is found.
    """
    candidates = [
        ev for ev in events
        if ev.type == EventType.compaction_summary
        and ev.payload.is_active  # type: ignore[union-attr]
    ]
    for ev in reversed(candidates):  # deepest first
        payload: CompactionSummaryPayload = ev.payload  # type: ignore[assignment]
        actual_fp = _fingerprint_event_ids(payload.covers_event_ids)
        if actual_fp == payload.anchor_fingerprint:
            return payload
    return None


def render(
    events: List[Event],
    render_config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Render events into a list of message dicts (``role + content[]``).

    Parameters
    ----------
    events:
        Ordered list of events (by seq).
    render_config:
        Optional snapshot of rendering parameters (from
        ``run_lifecycle(started).render_config``).  Used for truncation
        limits during Level-1 resume re-rendering.

    Returns
    -------
    List of message dicts, each with ``role`` and ``content`` keys.
    Messages sharing the same ``llm_message_id`` are grouped into one.
    """
    max_obs_length = (render_config or {}).get("max_observation_length", 0)

    active = pick_active_compaction(events)
    covers_set: set = set()
    if active is not None:
        covers_set = {eid for eid in active.covers_event_ids}

    out: List[Dict[str, Any]] = []

    for ev in events:
        # Skip events covered by active compaction
        if ev.event_id in covers_set:
            continue

        # Compaction summaries are inserted via _summary_message, not inline
        if ev.type == EventType.compaction_summary:
            continue

        # Offload records are metadata, not rendered as messages
        if ev.type == EventType.offload_record:
            continue

        # Run lifecycle events are metadata, not rendered as messages
        if ev.type == EventType.run_lifecycle:
            continue

        if ev.type == EventType.tool_result:
            payload: ToolResultPayload = ev.payload  # type: ignore[assignment]
            if payload.offload_handle:
                out.append(_offload_marker(ev))
                continue

        out.append(_to_message(ev, max_obs_length))

    # Insert summary message at the position of the first covered event
    if active is not None and covers_set:
        summary_msg = _summary_message(active)
        insert_pos = _find_summary_insert_pos(events, covers_set, out)
        out.insert(insert_pos, summary_msg)

    return _group_by_llm_message(out)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fingerprint_event_ids(ids: List[UUID]) -> str:
    """Deterministic fingerprint for a set of event IDs."""
    sorted_hex = sorted(str(uid) for uid in ids)
    return hashlib.sha256(",".join(sorted_hex).encode()).hexdigest()[:16]


def _offload_marker(ev: Event) -> Dict[str, Any]:
    payload: ToolResultPayload = ev.payload  # type: ignore[assignment]
    desc = payload.offload_handle  # use handle as short description
    return {
        "role": "tool",
        "content": [{"type": "text", "text": f"[[OFFLOAD:{desc}]]"}],
        "tool_call_id": payload.tool_call_id,
    }


def _to_message(ev: Event, max_obs_length: int = 0) -> Dict[str, Any]:
    """Convert a single event to a message dict."""
    role = ev.role
    text = ""

    if ev.type == EventType.user_input:
        text = ev.payload.text  # type: ignore[union-attr]
    elif ev.type == EventType.assistant_message:
        text = ev.payload.text  # type: ignore[union-attr]
    elif ev.type == EventType.assistant_reasoning:
        text = ev.payload.text  # type: ignore[union-attr]
    elif ev.type == EventType.system_prompt:
        text = ev.payload.rendered_prompt  # type: ignore[union-attr]
    elif ev.type == EventType.tool_call:
        return {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": ev.payload.tool_call_id,  # type: ignore[union-attr]
                "name": ev.payload.tool_name,  # type: ignore[union-attr]
                "input": ev.payload.arguments,  # type: ignore[union-attr]
            }],
        }
    elif ev.type == EventType.tool_result:
        payload: ToolResultPayload = ev.payload  # type: ignore[assignment]
        output = payload.output_raw
        if max_obs_length > 0 and len(output) > max_obs_length:
            half = (max_obs_length - 50) // 2
            output = output[:half] + "\n...[truncated]...\n" + output[-half:]
        return {
            "role": "tool",
            "content": [{"type": "text", "text": output}],
            "tool_call_id": payload.tool_call_id,
        }
    elif ev.type == EventType.error:
        text = f"[Error] {ev.payload.message}"  # type: ignore[union-attr]
    elif ev.type == EventType.compaction_summary:
        # Compaction summaries are handled separately (via _summary_message)
        text = ev.payload.structured_summary  # type: ignore[union-attr]
    elif ev.type == EventType.run_lifecycle:
        text = f"[Run {ev.payload.action}]"  # type: ignore[union-attr]
    else:
        text = str(ev.payload)

    return {
        "role": role,
        "content": [{"type": "text", "text": text}],
    }


def _summary_message(summary: CompactionSummaryPayload) -> Dict[str, Any]:
    """Build a message dict for a compaction summary."""
    parts = []
    for key, value in summary.structured_summary.items():
        parts.append(f"**{key}**: {value}")
    text = "\n\n".join(parts)
    return {
        "role": "user",
        "content": [{"type": "text", "text": f"Summary of earlier steps:\n\n{text}"}],
    }


def _find_summary_insert_pos(
    events: List[Event],
    covers_set: set,
    rendered: List[Dict[str, Any]],
) -> int:
    """Find the position in *rendered* to insert the summary message.

    The summary replaces the first covered event; so we insert at the
    position where the first covered event would have been.
    """
    for i, ev in enumerate(events):
        if ev.event_id in covers_set:
            # Count how many non-covered events came before this
            pos = 0
            for earlier_ev in events[:i]:
                if earlier_ev.event_id not in covers_set:
                    pos += 1
            return pos
    return 0


def _group_by_llm_message(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group messages that share the same llm_message_id into a single message
    with merged content list.  Messages without llm_message_id are kept as-is."""
    # For now, return messages as-is since the events already carry role info.
    # Full grouping by llm_message_id requires passing that field through;
    # this is a placeholder that preserves the current structure.
    return messages
