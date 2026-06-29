import asyncio
import logging
from contextvars import copy_context
from threading import Thread
from typing import Any, Dict, Union

from smolagents import ToolCollection

from .agent_model import AgentRunInfo
from .nexent_agent import NexentAgent, ProcessType

logger = logging.getLogger("run_agent")
logger.setLevel(logging.DEBUG)


def _detect_transport(url: str) -> str:
    """
    Auto-detect MCP transport type based on URL format.

    Args:
        url: MCP server URL

    Returns:
        Transport type: 'sse' or 'streamable-http'
    """
    url_stripped = url.strip()

    if url_stripped.endswith("/sse"):
        return "sse"
    elif url_stripped.endswith("/mcp"):
        return "streamable-http"

    return "streamable-http"


def _normalize_mcp_config(mcp_host_item: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Normalize MCP host configuration to a dictionary format.

    Args:
        mcp_host_item: Either a string URL or a dict with 'url', optional 'transport',
                       and optional 'headers' or 'authorization'

    Returns:
        Dictionary with 'url', 'transport', and optionally 'headers' keys
    """
    if isinstance(mcp_host_item, str):
        url = mcp_host_item
        transport = _detect_transport(url)
        return {"url": url, "transport": transport}
    elif isinstance(mcp_host_item, dict):
        url = mcp_host_item.get("url")
        if not url:
            raise ValueError("MCP host dict must contain 'url' key")
        transport = mcp_host_item.get("transport")
        if not transport:
            transport = _detect_transport(url)
        if transport not in ("sse", "streamable-http"):
            raise ValueError(f"Invalid transport type: {transport}. Must be 'sse' or 'streamable-http'")

        result = {"url": url, "transport": transport}

        if "authorization" in mcp_host_item and "headers" in mcp_host_item:
            headers = mcp_host_item["headers"].copy() if isinstance(mcp_host_item["headers"], dict) else {}
            headers["Authorization"] = mcp_host_item["authorization"]
            result["headers"] = headers
        elif "authorization" in mcp_host_item:
            result["headers"] = {"Authorization": mcp_host_item["authorization"]}
        elif "headers" in mcp_host_item:
            result["headers"] = mcp_host_item["headers"]

        return result
    else:
        raise ValueError(f"Invalid MCP host item type: {type(mcp_host_item)}. Must be str or dict")


def _wire_persistence_callbacks(agent, event_store) -> None:
    """Wire compaction/offload callbacks on agent.context_manager.

    Must be called AFTER the context_manager is finalized (i.e. after any
    external CM swap), so callbacks are always wired on the CM that is
    actually in use.

    The callback receives expanded parameters from ContextManager:
      (cache_type, summary_text, covered_pairs, anchor_fingerprint,
       model_id, records, [end_steps for current type])
    """
    from .agent_event.models import CompactionSummaryPayload, OffloadRecordPayload

    ctx_manager = getattr(agent, 'context_manager', None)
    if ctx_manager is None:
        return

    # Inject event_store into CM and its offload_store
    ctx_manager._event_store = event_store
    offload_store = getattr(ctx_manager, '_offload_store', None)
    if offload_store is not None:
        offload_store._event_store = event_store

    # Wire compaction callback
    def _on_compaction(cache_type, summary_text, covered_pairs,
                       anchor_fingerprint, model_id=None, records=None,
                       end_steps=None, compressed_step_numbers=None):
        try:
            call_type = "full" if cache_type == "previous" else "incremental"
            # Aggregate token/char stats from CompressionCallRecords
            input_tokens = 0
            output_tokens = 0
            input_chars = 0
            output_chars = 0
            if records:
                for r in records:
                    if not getattr(r, 'cache_hit', False):
                        input_tokens += getattr(r, 'input_tokens', 0) or 0
                        output_tokens += getattr(r, 'output_tokens', 0) or 0
                        input_chars += getattr(r, 'input_chars', 0) or 0
                        output_chars += getattr(r, 'output_chars', 0) or 0
            # Resolve compressed step_numbers → event_ids via CoreAgent._step_event_map
            covers_event_ids = []
            step_map = getattr(agent, '_step_event_map', {})
            logger.debug(
                "_on_compaction: cache_type=%s, compressed_step_numbers=%s, "
                "step_map_keys=%s, step_map_total=%d",
                cache_type, compressed_step_numbers, list(step_map.keys()),
                sum(len(v) for v in step_map.values()),
            )
            if compressed_step_numbers and step_map:
                for sn in compressed_step_numbers:
                    covers_event_ids.extend(step_map.get(sn, []))
            # For cache-hit (bypass) paths where compressed_step_numbers is empty,
            # fall back to the covers_event_ids stored in the cache object.
            if not covers_event_ids and cache_type == "previous":
                prev_cache = getattr(
                    getattr(agent, 'context_manager', None),
                    '_previous_summary_cache', None,
                )
                if prev_cache is not None and getattr(prev_cache, 'covers_event_ids', None):
                    covers_event_ids = list(prev_cache.covers_event_ids)
            # Store resolved covers_event_ids back into the cache so that
            # subsequent cache-hit calls can reuse them without re-resolving.
            if covers_event_ids and cache_type == "previous":
                prev_cache = getattr(
                    getattr(agent, 'context_manager', None),
                    '_previous_summary_cache', None,
                )
                if prev_cache is not None:
                    prev_cache.covers_event_ids = list(covers_event_ids)
            # Same for current cache: fall back to stored covers_event_ids on cache hit
            if not covers_event_ids and cache_type == "current":
                curr_cache = getattr(
                    getattr(agent, 'context_manager', None),
                    '_current_summary_cache', None,
                )
                if curr_cache is not None and getattr(curr_cache, 'covers_event_ids', None):
                    covers_event_ids = list(curr_cache.covers_event_ids)
            if covers_event_ids and cache_type == "current":
                curr_cache = getattr(
                    getattr(agent, 'context_manager', None),
                    '_current_summary_cache', None,
                )
                if curr_cache is not None:
                    curr_cache.covers_event_ids = list(covers_event_ids)
            agent._emit_event(
                "compaction_summary", "system",
                CompactionSummaryPayload(
                    structured_summary={"summary": summary_text},
                    covers_event_ids=covers_event_ids,
                    covered_pairs=covered_pairs,
                    end_steps=end_steps if end_steps is not None else covered_pairs,
                    anchor_fingerprint=anchor_fingerprint,
                    call_type=call_type,
                    summarizer_model_id=model_id,
                    input_tokens=input_tokens or None,
                    output_tokens=output_tokens or None,
                    input_chars=input_chars or None,
                    output_chars=output_chars or None,
                    is_active=True,
                ),
            )
        except Exception:
            logger.debug("compaction event emit failed", exc_info=True)

    ctx_manager._on_compaction = _on_compaction

    # Wire offload callback
    if offload_store is not None:
        def _on_offload(handle, description, original_chars, preview):
            try:
                content_ref = ""
                if agent._event_store is not None:
                    actual_content = offload_store.reload(handle)
                    if actual_content:
                        content_ref = agent._event_store.put_blob(actual_content)
                agent._emit_event(
                    "offload_record", "system",
                    OffloadRecordPayload(
                        handle=handle, description=description,
                        original_chars=original_chars,
                        preview=preview[:500] if preview else "",
                        content_ref=content_ref, source_event_id=None,
                    ),
                )
            except Exception:
                logger.debug("offload event emit failed", exc_info=True)

        offload_store._on_offload = _on_offload


def _inject_persistence_early(nexent: NexentAgent, agent_run_info: AgentRunInfo) -> None:
    """Wire event_store / session_id into NexentAgent BEFORE create_single_agent.

    This must run before create_single_agent so that the agent's
    _event_store / _session_id are set (needed by _emit_event in
    agent_run_with_observer).  CM callbacks are wired later in
    _finalize_persistence, after the CM is finalized.
    """
    event_store = getattr(agent_run_info, 'event_store', None)
    if event_store is None:
        return
    session_id = getattr(agent_run_info, 'session_id', None)
    nexent.configure_persistence(event_store=event_store, session_id=session_id)


def _finalize_persistence(nexent: NexentAgent, agent_run_info: AgentRunInfo) -> None:
    """Finalize persistence after CM is settled: wire callbacks + resume/history.

    Must run after set_agent AND after any external CM swap, so that
    agent.context_manager is the one that will actually be used.
    """
    event_store = getattr(agent_run_info, 'event_store', None)

    # Wire CM/offload callbacks if persistence is active
    if event_store is not None:
        _wire_persistence_callbacks(nexent.agent, event_store)

    if event_store is None:
        # No persistence — still add history the old way
        nexent.add_history_to_agent(agent_run_info.history)
        return

    # Resume path: reconstruct memory from persisted events instead of using history
    resume_from = getattr(agent_run_info, 'resume_from', None)
    if resume_from is not None:
        from .agent_event.reconstructor import MemoryReconstructor
        fidelity = getattr(agent_run_info, 'resume_fidelity', None) or 'lossy'
        try:
            from uuid import UUID as _UUID
            leaf_id = _UUID(resume_from) if isinstance(resume_from, str) else resume_from
            branch_result = event_store.read_branch(leaf_event_id=leaf_id)
            reconstructor = MemoryReconstructor()
            reconstructor.reconstruct(
                nexent.agent, branch_result, fidelity=fidelity, event_store=event_store,
            )
            # Register ALL session events (not just the branch chain) into
            # _step_event_map so that compaction_summary.covers_event_ids can
            # resolve step_numbers to event_ids.  read_branch only returns the
            # parent_event_id chain, missing sibling events like tool_call and
            # tool_result that share the same parent — these carry step_index
            # and are needed for covers_event_ids resolution.
            session_id_val = getattr(agent_run_info, 'session_id', None)
            if session_id_val is not None:
                full_session = event_store.read_session(
                    _UUID(session_id_val) if isinstance(session_id_val, str) else session_id_val
                )
                _register_step_event_map(nexent.agent, full_session)
            else:
                _register_step_event_map(nexent.agent, branch_result)
        except Exception:
            logger.warning("Event resume failed, falling back to history", exc_info=True)
            nexent.add_history_to_agent(agent_run_info.history)
        return  # resume path complete — skip add_history_to_agent below

    # Normal path: write history as events to EventStore, then reconstruct memory.
    # This ensures history events are persisted and registered in _step_event_map
    # so that covers_event_ids can reference them in compaction_summary events.
    _persist_and_reconstruct_history(nexent, agent_run_info, event_store)


def _register_step_event_map(agent, branch_result) -> None:
    """Register event_ids from reconstructed events into agent._step_event_map.

    After resume, compaction_summary.covers_event_ids needs to resolve
    step_numbers to event_ids for the old (reconstructed) steps.
    """
    from .agent_event.models import EventType

    events = branch_result.events if hasattr(branch_result, 'events') else branch_result
    registered = 0
    for ev in events:
        step_idx = ev.step_index
        if step_idx is not None:
            agent._step_event_map.setdefault(step_idx, []).append(ev.event_id)
            registered += 1
    logger.debug(
        "_register_step_event_map: %d events scanned, %d step_index entries registered, "
        "map keys=%s, total_map_entries=%d",
        len(events), registered, list(agent._step_event_map.keys()),
        sum(len(v) for v in agent._step_event_map.values()),
    )


def _persist_and_reconstruct_history(nexent, agent_run_info, event_store) -> None:
    """Write history entries as events to EventStore and reconstruct agent memory."""
    from .agent_model import AgentHistory
    from .agent_event.models import (
        Event as AgentEvent, EventType,
        UserInputPayload, AssistantMessagePayload,
    )
    from uuid import uuid4 as _uuid4
    from datetime import datetime as _dt, timezone as _tz

    history = agent_run_info.history
    if not history:
        return

    # Build synthetic events and write them to EventStore
    # Assign stepIndex=1 to all history events since Level-0 reconstruction
    # gives all history ActionSteps step_number=1, and compaction needs
    # the step_index on these events to populate covers_event_ids.
    events = []
    for msg in history:
        if msg.role == 'user':
            ev = AgentEvent(
                uuid=_uuid4(),
                sessionId=nexent._session_id or _uuid4(),
                agentId=nexent.agent.agent_name,
                seq=0,  # store assigns
                type=EventType.user_input,
                role='user',
                stepIndex=1,
                timestamp=_dt.now(_tz.utc),
                payload=UserInputPayload(text=msg.content),
            )
            events.append(ev)
        elif msg.role == 'assistant':
            ev = AgentEvent(
                uuid=_uuid4(),
                sessionId=nexent._session_id or _uuid4(),
                agentId=nexent.agent.agent_name,
                seq=0,
                type=EventType.assistant_message,
                role='assistant',
                stepIndex=1,
                timestamp=_dt.now(_tz.utc),
                payload=AssistantMessagePayload(text=msg.content),
            )
            events.append(ev)

    # Append to EventStore and register in _step_event_map
    for ev in events:
        event_store.append(ev)

    # Reconstruct memory from the synthetic events
    from .agent_event.reconstructor import MemoryReconstructor
    reconstructor = MemoryReconstructor()
    reconstructor.reconstruct(nexent.agent, events, fidelity="lossy")

    # Register history step event_ids in _step_event_map.
    # After Level 0 reconstruction, each ActionStep has a step_number.
    # We map step_number → [event_id] so compaction_summary can reference them.
    hist_steps = nexent.agent.memory.steps[:nexent.agent._history_step_count]
    ev_idx = 0
    for step in hist_steps:
        step_num = getattr(step, 'step_number', None)
        if step_num is not None and ev_idx < len(events):
            # Each pair of events (user+assistant) maps to one ActionStep
            # Register ALL event_ids associated with this step
            ids_for_step = []
            while ev_idx < len(events):
                ids_for_step.append(events[ev_idx].event_id)
                ev_idx += 1
                # After a user_input + assistant_message pair, we've covered one step
                if events[ev_idx - 1].type == EventType.assistant_message:
                    break
            if ids_for_step:
                nexent.agent._step_event_map.setdefault(step_num, []).extend(ids_for_step)


def agent_run_thread(agent_run_info: AgentRunInfo):
    try:
        mcp_host = agent_run_info.mcp_host
        if mcp_host is None or len(mcp_host) == 0:
            nexent = NexentAgent(
                observer=agent_run_info.observer,
                model_config_list=agent_run_info.model_config_list,
                stop_event=agent_run_info.stop_event
            )
            # Inject persistence BEFORE create_single_agent so that
            # agent._event_store / _session_id are available during agent creation.
            _inject_persistence_early(nexent, agent_run_info)

            agent = nexent.create_single_agent(agent_run_info.agent_config)
            nexent.set_agent(agent)

            # Swap external CM if provided
            if getattr(agent_run_info, 'context_manager', None) is not None:
                agent.context_manager = agent_run_info.context_manager
                # Sync reload tool to the swapped store
                if 'reload_original_context_messages' in agent.tools:
                    agent.tools['reload_original_context_messages']._offload_store = \
                        agent.context_manager.offload_store

            # Finalize persistence: wire callbacks on the final CM + resume/history
            _finalize_persistence(nexent, agent_run_info)
            nexent.agent_run_with_observer(
                query=agent_run_info.query, reset=False)
        else:
            agent_run_info.observer.add_message(
                "", ProcessType.AGENT_NEW_RUN, "<MCP_START>")
            mcp_client_list = [_normalize_mcp_config(item) for item in mcp_host]

            with ToolCollection.from_mcp(mcp_client_list, trust_remote_code=True) as tool_collection:
                nexent = NexentAgent(
                    observer=agent_run_info.observer,
                    model_config_list=agent_run_info.model_config_list,
                    stop_event=agent_run_info.stop_event,
                    mcp_tool_collection=tool_collection
                )
                _inject_persistence_early(nexent, agent_run_info)

                agent = nexent.create_single_agent(agent_run_info.agent_config)
                nexent.set_agent(agent)

                # Swap external CM if provided
                if getattr(agent_run_info, 'context_manager', None) is not None:
                    agent.context_manager = agent_run_info.context_manager
                    if 'reload_original_context_messages' in agent.tools:
                        agent.tools['reload_original_context_messages']._offload_store = \
                            agent.context_manager.offload_store

                # Finalize persistence: wire callbacks on the final CM + resume/history
                _finalize_persistence(nexent, agent_run_info)
                nexent.agent_run_with_observer(
                    query=agent_run_info.query, reset=False)

    except Exception as e:
        if "Couldn't connect to the MCP server" in str(e):
            mcp_connect_error_str = "MCP服务器连接超时。" if agent_run_info.observer.lang == "zh" else "Couldn't connect to the MCP server."
            agent_run_info.observer.add_message(
                "", ProcessType.FINAL_ANSWER, mcp_connect_error_str)
        else:
            agent_run_info.observer.add_message(
                "", ProcessType.FINAL_ANSWER, f"Run Agent Error: {e}")
        raise ValueError(f"Error in agent_run_thread: {e}")


async def agent_run(agent_run_info: AgentRunInfo):
    observer = agent_run_info.observer

    ctx = copy_context()
    thread_agent = Thread(target=ctx.run, args=(agent_run_thread, agent_run_info))
    thread_agent.start()

    while thread_agent.is_alive():
        cached_message = observer.get_cached_message()
        for message in cached_message:
            yield message
            if len(cached_message) < 8:
                await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)

    cached_message = observer.get_cached_message()
    for message in cached_message:
        yield message
