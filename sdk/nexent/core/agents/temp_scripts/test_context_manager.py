import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_utils import (
    build_agent_run_info,
    run_agent_with_tracking,
    parse_conversation_to_history,
    print_history_stats,
    AgentHistory,
    ContextManagerConfig,
)
from nexent.core.agents.agent_context import ContextManager
from nexent.core.utils.token_estimation import estimate_tokens_text
from uuid import UUID
from collections import Counter


async def run_multi_turn(
    queries: list[str],
    base_history: list[AgentHistory],
    cm_config: ContextManagerConfig,
    max_steps: int = 5,
    debug: bool = False,
    event_store=None,
    session_id: str = None,
) -> list:
    """
    执行自定义多轮对话测试。

    每轮对话会基于累积的 conversation_history（包含 base_history + 前几轮对话）
    运行 Agent，并将当前轮的 query 与 assistant 回复追加到 history 中，
    供下一轮使用。

    当 cm_config.enabled 为 True 时，会创建一个 conversation 级别的 ContextManager
    并在多轮之间复用，以验证跨 run 的摘要缓存机制。

    当 event_store 不为 None 时，事件持久化开启，对话事件写入 JSONL。
    """
    conversation_history = list(base_history)  # 深拷贝避免污染原始历史
    results = []

    # 创建 conversation 级别的 ContextManager（若启用）
    shared_cm = None
    if cm_config and cm_config.enabled:
        shared_cm = ContextManager(config=cm_config, max_steps=max_steps)

    print(f"\n{'='*60}")
    print(
        f"开始多轮对话测试 | context_manager={'启用' if cm_config.enabled else '禁用(baseline)'}"
    )
    print(f"初始历史轮数: {len(base_history)//2} | 预定义 query 数: {len(queries)}")
    print(f"{'='*60}")

    for turn_idx, query in enumerate(queries, start=1):
        print(f"\n--- 第 {turn_idx}/{len(queries)} 轮 ---")
        print(f"用户: {query}")

        agent_run_info = build_agent_run_info(
            query,
            conversation_history,
            max_steps=max_steps,
            context_manager_config=cm_config,
            event_store=event_store,
            session_id=session_id,
        )

        # 挂载 conversation 级别的 ContextManager，实现跨 run 复用
        if shared_cm is not None:
            agent_run_info.context_manager = shared_cm

        result = await run_agent_with_tracking(agent_run_info, debug=debug)
        results.append(result)

        print(f"助手: {result.final_answer[:200]}...")
        print(f"[本轮统计] {result.message_type_count}")

        # 将本轮对话追加到累积历史
        conversation_history.append(AgentHistory(role="user", content=query))
        conversation_history.append(
            AgentHistory(role="assistant", content=result.final_answer)
        )

    # 打印 ContextManager 缓存统计（若启用）
    if shared_cm is not None:
        print(f"\n[ContextManager 全局统计]")
        print(f"  {shared_cm.get_all_compression_stats()}")

    print(f"\n{'='*60}")
    print(f"多轮对话结束 | 总对话轮数: {len(conversation_history)//2}")
    print(f"{'='*60}")
    return results


def create_event_store(persistence_dir: str = None):
    """Create a JsonlEventStore for testing. Returns (store, session_id).

    If persistence_dir is None, defaults to ../persistence/ (relative to
    temp_scripts/), which maps to sdk/nexent/core/agents/persistence/.
    The directory is NOT cleaned up after the test so you can inspect the
    JSONL files.
    """
    from nexent.core.agents.agent_event.jsonl_store import JsonlEventStore
    from nexent.core.agents.agent_event.models import Session
    from uuid import uuid4
    from datetime import datetime, timezone
    from pathlib import Path

    if persistence_dir is None:
        persistence_dir = Path(__file__).parent.parent / "persistence"
    else:
        persistence_dir = Path(persistence_dir)

    persistence_dir.mkdir(parents=True, exist_ok=True)
    store = JsonlEventStore(persistence_dir)
    sid = uuid4()
    now = datetime.now(timezone.utc)

    store.upsert_session(Session(
        session_id=sid, agent_id="test_agent",
        created_at=now, updated_at=now,
    ))

    print(f"[EventPersistence] dir={persistence_dir}, session_id={sid}")
    return store, sid


def find_leaf_event_id(store, session_id) -> str:
    """Return the event_id of the last assistant_message(is_final_answer=True) in a session."""
    sid = UUID(session_id) if isinstance(session_id, str) else session_id
    branch = store.read_session(sid)
    for ev in reversed(branch.events):
        if ev.type.value == "assistant_message" and getattr(ev.payload, 'is_final_answer', False):
            return str(ev.event_id)
    # fallback: last event
    return str(branch.events[-1].event_id)


def validate_step_continuity(events) -> dict:
    """Validate seq and stepIndex continuity across events."""
    seqs = [ev.seq for ev in events]
    step_indices = [ev.step_index for ev in events if ev.step_index is not None]
    result = {
        "total_events": len(events),
        "seq_continuous": seqs == list(range(seqs[0], seqs[-1] + 1)) if seqs else True,
        "seq_range": f"{seqs[0]}..{seqs[-1]}" if seqs else "empty",
        "unique_step_indices": sorted(set(step_indices)) if step_indices else [],
    }
    return result


async def test_t1_basic_resume():
    """T1/T3: Basic resume + multi-turn continuous resume.

    T1: verify step_number continuation and event appending (Run1→Run2).
    T3: extend to 3 runs, verify JSONL integrity across multiple resumes.
    """
    from pathlib import Path as _Path
    _SCRIPTS_DIR = _Path(__file__).parent
    _HISTORY_FILE = str(_SCRIPTS_DIR / "small_history.md")

    store, sid = create_event_store()
    cm_config = ContextManagerConfig(
        enabled=True, token_threshold=3000, keep_recent_pairs=1,
    )
    shared_cm = ContextManager(config=cm_config, max_steps=10)

    # --- Run 1: normal run with history ---
    agent_history = parse_conversation_to_history(_HISTORY_FILE)
    query1 = "总结之前对话的主题是什么"
    run1_info = build_agent_run_info(
        query1, agent_history,
        max_steps=5,
        context_manager_config=cm_config,
        event_store=store,
        session_id=str(sid),
    )
    run1_info.context_manager = shared_cm

    print(f"\n{'='*60}")
    print("Run 1 (normal)")
    print(f"{'='*60}")
    result1 = await run_agent_with_tracking(run1_info)
    print(f"助手: {result1.final_answer[:200]}...")
    print(f"[Run1 统计] {result1.message_type_count}")

    session_after_run1 = store.read_session(sid)
    run1_events = session_after_run1.events
    run1_event_count = len(run1_events)
    run1_max_step = max(
        (ev.step_index for ev in run1_events if ev.step_index is not None),
        default=0,
    )
    leaf_id_1 = find_leaf_event_id(store, str(sid))
    print(f"\n[Run1] events={run1_event_count}, max_stepIndex={run1_max_step}, leaf_id={leaf_id_1[:16]}...")

    # --- Run 2: resume from leaf_id_1 ---
    query2 = "复数表示旋转，请使用python最基础的功能演示下"
    run2_info = build_agent_run_info(
        query2, [],
        max_steps=5,
        context_manager_config=cm_config,
        event_store=store,
        session_id=str(sid),
        resume_from=leaf_id_1,
        resume_fidelity="lossy",
    )
    run2_info.context_manager = shared_cm

    print(f"\n{'='*60}")
    print("Run 2 (resume from leaf_id_1)")
    print(f"{'='*60}")
    result2 = await run_agent_with_tracking(run2_info)
    print(f"助手: {result2.final_answer[:200]}...")
    print(f"[Run2 统计] {result2.message_type_count}")

    session_after_run2 = store.read_session(sid)
    run2_events = session_after_run2.events
    run2_event_count = len(run2_events)
    run2_max_step = max(
        (ev.step_index for ev in run2_events if ev.step_index is not None),
        default=0,
    )
    leaf_id_2 = find_leaf_event_id(store, str(sid))
    print(f"\n[Run2] total_events={run2_event_count}, new={run2_event_count - run1_event_count}, "
          f"max_stepIndex={run2_max_step}, leaf_id={leaf_id_2[:16]}...")

    # --- Run 3: resume from leaf_id_2 (T3 extension) ---
    query3 = "请用 Python 计算 2 的 10 到 12 次方，并告诉我哪些是质数"
    run3_info = build_agent_run_info(
        query3, [],
        max_steps=5,
        context_manager_config=cm_config,
        event_store=store,
        session_id=str(sid),
        resume_from=leaf_id_2,
        resume_fidelity="lossy",
    )
    run3_info.context_manager = shared_cm

    print(f"\n{'='*60}")
    print("Run 3 (resume from leaf_id_2)")
    print(f"{'='*60}")
    result3 = await run_agent_with_tracking(run3_info)
    print(f"助手: {result3.final_answer[:200]}...")
    print(f"[Run3 统计] {result3.message_type_count}")

    session_after_run3 = store.read_session(sid)
    all_events = session_after_run3.events
    run3_event_count = len(all_events)

    # ====== Validate ======
    # a. Seq continuity across all 3 runs
    validation = validate_step_continuity(all_events)
    print(f"\n[验证T3-a] total_events={run3_event_count}, seq_continuous={validation['seq_continuous']}, "
          f"range={validation['seq_range']}")

    # b. step_number monotonic: each run's min step > previous run's max step, no overlap
    run1_step_set = set(ev.step_index for ev in run1_events if ev.step_index is not None)
    run2_step_set = set(ev.step_index for ev in run2_events[run1_event_count:] if ev.step_index is not None)
    run3_step_set = set(ev.step_index for ev in all_events[run2_event_count:] if ev.step_index is not None)

    run2_min_step = min(run2_step_set) if run2_step_set else None
    run3_min_step = min(run3_step_set) if run3_step_set else None

    overlap_12 = run1_step_set & run2_step_set
    overlap_23 = run2_step_set & run3_step_set
    overlap_13 = run1_step_set & run3_step_set
    no_overlap = (not overlap_12) and (not overlap_23) and (not overlap_13)

    run2_step_ok = run2_min_step is not None and run2_min_step > run1_max_step
    run3_step_ok = run3_min_step is not None and run3_min_step > run2_max_step

    print(f"[验证T3-b] run1_max={run1_max_step}, run2_min={run2_min_step}/max={run2_max_step}, "
          f"run3_min={run3_min_step}")
    print(f"[验证T3-b] run2_continuation={'OK' if run2_step_ok else 'FAIL'}, "
          f"run3_continuation={'OK' if run3_step_ok else 'FAIL'}")
    print(f"[验证T3-b] no_step_overlap={'OK' if no_overlap else 'FAIL'} "
          f"(1∩2={overlap_12 or 'none'}, 2∩3={overlap_23 or 'none'}, 1∩3={overlap_13 or 'none'})")

    # c. runs.jsonl has 3 Run records
    import json
    from pathlib import Path
    runs_path = Path(store._root) / str(sid) / "runs.jsonl"
    runs = []
    with open(runs_path, encoding='utf-8') as f:
        for line in f:
            runs.append(json.loads(line))
    runs_ok = len(runs) == 3
    print(f"[验证T3-c] runs_count={len(runs)} (expected 3), statuses={[r['status'] for r in runs]}, "
          f"{'OK' if runs_ok else 'FAIL'}")

    # d. read_branch(leaf_id_2) returns Run2's event chain without gaps
    #    Note: parent_event_id chain does NOT cross run boundaries (each run's
    #    system_prompt has parentUuid=None), so read_branch only covers a single
    #    run. This is expected — cross-run continuity is via read_session.
    from uuid import UUID as _UUID
    branch_from_leaf2 = store.read_branch(leaf_event_id=_UUID(leaf_id_2))
    branch_ids = set(str(ev.event_id) for ev in branch_from_leaf2.events)
    run2_new_ids = set(str(ev.event_id) for ev in run2_events[run1_event_count:])
    run2_in_branch = run2_new_ids & branch_ids
    branch_has_run2 = len(run2_in_branch) > 0
    branch_no_gap = not branch_from_leaf2.has_gap

    print(f"[验证T3-d] branch_from_leaf2: events={len(branch_from_leaf2.events)}, "
          f"has_gap={branch_from_leaf2.has_gap}, "
          f"run2_in_branch={len(run2_in_branch)}/{len(run2_new_ids)} ({'OK' if branch_has_run2 else 'FAIL'})")

    # Type distribution
    type_counts = Counter(ev.type.value for ev in all_events)
    print(f"[验证] types={dict(type_counts)}")

    all_ok = (validation['seq_continuous'] and no_overlap and runs_ok
              and branch_has_run2 and branch_no_gap)
    print(f"\n{'='*60}")
    print(f"T1/T3 测试{'通过' if all_ok else '未通过'}")
    print(f"{'='*60}")
    return result3


async def test_t2_resume_compaction():
    """T2: Resume + compaction — verify covers_event_ids correctly references old step events.

    Steps:
    1. Create EventStore + Session
    2. Run 1 with long history + low threshold → trigger compaction
    3. Get leaf_id
    4. Run 2 resume from leaf_id, query2 long enough to trigger compaction again
    5. Verify:
       a. Run2 compaction_summary has non-empty covers_event_ids
       b. covers_event_ids are findable in events.jsonl
       c. Covered events include step_numbers from Run1
    """
    from pathlib import Path as _Path
    _SCRIPTS_DIR = _Path(__file__).parent
    _HISTORY_FILE = str(_SCRIPTS_DIR / "small_history.md")

    store, sid = create_event_store()

    # --- Run 1: long history + low threshold to ensure compaction ---
    agent_history = parse_conversation_to_history(_HISTORY_FILE)
    cm_config = ContextManagerConfig(
        enabled=True, token_threshold=2000, keep_recent_pairs=1,
    )
    shared_cm = ContextManager(config=cm_config, max_steps=10)

    query1 = "总结之前对话的主题是什么"
    run1_info = build_agent_run_info(
        query1, agent_history,
        max_steps=5,
        context_manager_config=cm_config,
        event_store=store,
        session_id=str(sid),
    )
    run1_info.context_manager = shared_cm

    print(f"\n{'='*60}")
    print("T2: Run 1 (normal, expect compaction)")
    print(f"{'='*60}")
    result1 = await run_agent_with_tracking(run1_info)
    print(f"助手: {result1.final_answer[:200]}...")
    print(f"[Run1 统计] {result1.message_type_count}")

    # Inspect run1 compaction events
    session_after_run1 = store.read_session(sid)
    run1_events = session_after_run1.events
    run1_compaction_events = [
        ev for ev in run1_events if ev.type.value == "compaction_summary"
    ]
    print(f"\n[Run1] events={len(run1_events)}, compaction_count={len(run1_compaction_events)}")
    for cev in run1_compaction_events:
        p = cev.payload
        print(f"  covers_event_ids count={len(p.covers_event_ids)}, "
              f"call_type={getattr(p, 'call_type', None)}, "
              f"covered_pairs={getattr(p, 'covered_pairs', None)}, "
              f"anchor_fp={getattr(p, 'anchor_fingerprint', None)[:16]}")

    # Find leaf event for resume
    leaf_id = find_leaf_event_id(store, str(sid))
    run1_event_count = len(run1_events)
    run1_max_step = max(
        (ev.step_index for ev in run1_events if ev.step_index is not None),
        default=0,
    )
    print(f"[Run1] leaf_event_id={leaf_id}, max_stepIndex={run1_max_step}")

    # --- Run 2: resume from leaf_id, query2 triggers compaction again ---
    query2 = "请用 Python 计算 2 的 10 到 15 次方，并告诉我哪些是质数。请分步执行。"
    run2_info = build_agent_run_info(
        query2, [],  # no history — resume path
        max_steps=10,
        context_manager_config=cm_config,
        event_store=store,
        session_id=str(sid),
        resume_from=leaf_id,
        resume_fidelity="lossy",
    )
    run2_info.context_manager = shared_cm

    print(f"\n{'='*60}")
    print("T2: Run 2 (resume, expect compaction again)")
    print(f"{'='*60}")
    result2 = await run_agent_with_tracking(run2_info)
    print(f"助手: {result2.final_answer[:200]}...")
    print(f"[Run2 统计] {result2.message_type_count}")

    # --- Validate ---
    session_after_run2 = store.read_session(sid)
    all_events = session_after_run2.events
    run2_new_events = all_events[run1_event_count:]
    run2_new_count = len(run2_new_events)

    # Seq continuity
    validation = validate_step_continuity(all_events)
    print(f"\n[验证] total_events={len(all_events)}, run1={run1_event_count}, run2_new={run2_new_count}")
    print(f"[验证] seq_continuous={validation['seq_continuous']}, range={validation['seq_range']}")

    # Step continuity
    run2_step_indices = [
        ev.step_index for ev in run2_new_events if ev.step_index is not None
    ]
    if run2_step_indices:
        run2_min_step = min(run2_step_indices)
        step_continuation_ok = run2_min_step > run1_max_step
        print(f"[验证] run2_min_stepIndex={run2_min_step}, run1_max_stepIndex={run1_max_step}, "
              f"continuation={'OK' if step_continuation_ok else 'FAIL'}")
    else:
        print("[验证] run2 has no stepIndex events")

    # Build lookup: event_id -> event
    event_id_map = {str(ev.event_id): ev for ev in all_events}

    # a. Run2 compaction_summary events have non-empty covers_event_ids
    run2_compaction_events = [
        ev for ev in run2_new_events if ev.type.value == "compaction_summary"
    ]
    print(f"\n[验证T2-a] Run2 compaction_count={len(run2_compaction_events)}")
    has_non_empty_covers = False
    all_covers_found = True
    covers_run1_steps = False
    all_covered_step_indices = []

    for i, cev in enumerate(run2_compaction_events):
        p = cev.payload
        covers_ids = p.covers_event_ids
        covers_count = len(covers_ids)
        print(f"  compaction[{i}]: covers_event_ids count={covers_count}, "
              f"call_type={getattr(p, 'call_type', None)}, "
              f"covered_pairs={getattr(p, 'covered_pairs', None)}")

        if covers_count > 0:
            has_non_empty_covers = True

        # b. covers_event_ids are findable in events.jsonl
        for eid in covers_ids:
            eid_str = str(eid)
            if eid_str not in event_id_map:
                all_covers_found = False
                print(f"  [WARN] covers_event_id={eid_str} NOT found in events!")

        # c. Covered events include step_numbers from Run1
        covered_step_indices = []
        for eid in covers_ids:
            eid_str = str(eid)
            if eid_str in event_id_map:
                covered_ev = event_id_map[eid_str]
                if covered_ev.step_index is not None:
                    covered_step_indices.append(covered_ev.step_index)
        all_covered_step_indices.extend(covered_step_indices)

        # Check if any covered step belongs to Run1
        run1_step_set = set(
            ev.step_index for ev in run1_events if ev.step_index is not None
        )
        if covered_step_indices and set(covered_step_indices) & run1_step_set:
            covers_run1_steps = True

    print(f"\n[验证T2-a] has_non_empty_covers={'OK' if has_non_empty_covers else 'FAIL'}")
    print(f"[验证T2-b] all_covers_found={'OK' if all_covers_found else 'FAIL'}")
    print(f"[验证T2-c] covers_run1_steps={'OK' if covers_run1_steps else 'FAIL (or no Run1 steps covered)'}")
    print(f"[验证T2-c] all_covered_step_indices={sorted(set(all_covered_step_indices))}")

    # Run1 compaction details for cross-reference
    print(f"\n[ContextManager 全局统计] {shared_cm.get_all_compression_stats()}")

    # Runs.jsonl check
    import json
    from pathlib import Path
    runs_path = Path(store._root) / str(sid) / "runs.jsonl"
    runs = []
    with open(runs_path, encoding='utf-8') as f:
        for line in f:
            runs.append(json.loads(line))
    print(f"[验证] runs_count={len(runs)}, statuses={[r['status'] for r in runs]}")

    print(f"\n{'='*60}")
    print("T2 测试完成")
    print(f"{'='*60}")
    return result2


async def test_t4_faithful_resume():
    """T4: Level 1 (faithful) resume — verify SummaryTaskStep, OffloadStore repopulation,
    _step_event_map coverage, and step_number continuation.

    Run1: multi-step with low threshold + offload enabled → triggers compaction + offload
    Run2: resume_from=leaf_id, resume_fidelity="faithful" → Level 1 reconstruction
    Verify:
      a. agent.memory.steps contains SummaryTaskStep
      b. OffloadStore has entries matching Run1's offload_record events
      c. _step_event_map has entries for pre-compaction steps
      d. Run2 new step_number correctly continues from Run1 max
    """
    from pathlib import Path as _Path
    _SCRIPTS_DIR = _Path(__file__).parent
    _HISTORY_FILE = str(_SCRIPTS_DIR / "small_history.md")

    store, sid = create_event_store()
    # Enable compaction (low threshold) + offload (per_step_render_limit + enable_reload)
    cm_config = ContextManagerConfig(
        enabled=True,
        token_threshold=2000,
        keep_recent_pairs=1,
        per_step_render_limit=500,
        enable_reload=True,
    )
    shared_cm = ContextManager(config=cm_config, max_steps=20)

    # ====== Run 1: multi-step with history, trigger compaction + offload ======
    agent_history = parse_conversation_to_history(_HISTORY_FILE)
    query1 = "请用 Python 计算 2 的 10 到 12 次方，并告诉我哪些是质数"
    run1_info = build_agent_run_info(
        query1, agent_history,
        max_steps=8,
        context_manager_config=cm_config,
        event_store=store,
        session_id=str(sid),
    )
    run1_info.context_manager = shared_cm

    print(f"\n{'='*60}")
    print("T4: Run 1 (normal, expect compaction + offload)")
    print(f"{'='*60}")
    result1 = await run_agent_with_tracking(run1_info)
    print(f"助手: {result1.final_answer[:200]}...")
    print(f"[Run1 统计] {result1.message_type_count}")

    # Inspect Run1 events
    session_after_run1 = store.read_session(sid)
    run1_events = session_after_run1.events
    run1_event_count = len(run1_events)
    run1_max_step = max(
        (ev.step_index for ev in run1_events if ev.step_index is not None),
        default=0,
    )

    # Count compaction and offload events
    run1_compaction = [ev for ev in run1_events if ev.type.value == "compaction_summary"]
    run1_offload = [ev for ev in run1_events if ev.type.value == "offload_record"]
    print(f"\n[Run1] events={run1_event_count}, compaction={len(run1_compaction)}, "
          f"offload={len(run1_offload)}, max_stepIndex={run1_max_step}")

    # Capture Run1 offload handles for later comparison
    run1_offload_handles = [ev.payload.handle for ev in run1_offload]
    print(f"[Run1] offload_handles={run1_offload_handles}")

    # Capture Run1 OffloadStore state
    run1_active_handles = []
    if shared_cm._offload_store is not None:
        run1_active_handles = [h for h, _ in shared_cm._offload_store.list_active()]
    print(f"[Run1] offload_store active={run1_active_handles}")

    # Capture Run1 SummaryTaskStep existence
    from nexent.core.agents.agent_context.summary_step import SummaryTaskStep
    run1_has_summary = any(isinstance(s, SummaryTaskStep) for s in shared_cm.agent.memory.steps
                          ) if hasattr(shared_cm, 'agent') else False
    # Note: shared_cm doesn't hold agent reference; we check after Run2 resume

    # Find leaf_id for resume
    leaf_id = find_leaf_event_id(store, str(sid))
    print(f"[Run1] leaf_event_id={leaf_id[:16]}...")

    # ====== Run 2: Level 1 (faithful) resume ======
    query2 = "简单解释一下什么是复数，用一句话回答"
    run2_info = build_agent_run_info(
        query2, [],
        max_steps=5,
        context_manager_config=cm_config,
        event_store=store,
        session_id=str(sid),
        resume_from=leaf_id,
        resume_fidelity="faithful",
    )
    run2_info.context_manager = shared_cm

    print(f"\n{'='*60}")
    print("T4: Run 2 (faithful resume)")
    print(f"{'='*60}")
    result2 = await run_agent_with_tracking(run2_info)
    print(f"助手: {result2.final_answer[:200]}...")
    print(f"[Run2 统计] {result2.message_type_count}")

    # ====== Validate ======
    # We need to inspect the agent that was used in Run2.
    # Since agent_run creates a new CoreAgent each run, we need to get it
    # from the session events or reconstruct state from the observer.
    # The most reliable way: read session events and check what was persisted.

    session_after_run2 = store.read_session(sid)
    all_events = session_after_run2.events

    # a. Verify compaction_summary + offload_record events exist from Run1
    all_compaction = [ev for ev in all_events if ev.type.value == "compaction_summary"]
    all_offload = [ev for ev in all_events if ev.type.value == "offload_record"]
    print(f"\n[验证T4-a] compaction_events={len(all_compaction)}, offload_events={len(all_offload)}")

    # Check that Run1 had at least one compaction and one offload (precondition)
    has_compaction = len(run1_compaction) > 0
    has_offload = len(run1_offload) > 0
    print(f"[验证T4-a] Run1 had compaction={'OK' if has_compaction else 'SKIP (no compaction triggered)'}")
    print(f"[验证T4-a] Run1 had offload={'OK' if has_offload else 'SKIP (no offload triggered)'}")

    # b. OffloadStore repopulation: check active handles after resume
    #    After Run2 completes, the shared_cm was swapped to Run2's agent.
    #    We check the offload_store on shared_cm directly.
    run2_active_handles = []
    run2_reloaded_ok = True
    if shared_cm._offload_store is not None:
        run2_active_handles = [h for h, _ in shared_cm._offload_store.list_active()]
        print(f"[验证T4-b] offload_store active after resume={run2_active_handles}")
        # Try reload each Run1 handle
        for handle in run1_offload_handles:
            content = shared_cm._offload_store.reload(handle)
            if content is None:
                run2_reloaded_ok = False
                print(f"  [WARN] handle={handle[:16]}... reload returned None")
            else:
                print(f"  handle={handle[:16]}... reload OK, content_len={len(content)}")
    else:
        print("[验证T4-b] offload_store is None")

    # Offload handles from Run1 should be present after Level 1 resume
    if run1_offload_handles and shared_cm._offload_store is not None:
        handles_restored = all(h in run2_active_handles for h in run1_offload_handles)
        print(f"[验证T4-b] Run1 handles restored in Run2={'OK' if handles_restored else 'FAIL'}")
        print(f"[验证T4-b] reload_all_ok={'OK' if run2_reloaded_ok else 'FAIL'}")
    else:
        print("[验证T4-b] skipped (no offload in Run1 or no offload_store)")

    # c. Verify Run2 step_number continuation
    run2_step_indices = [
        ev.step_index for ev in all_events[run1_event_count:]
        if ev.step_index is not None
    ]
    if run2_step_indices:
        run2_min_step = min(run2_step_indices)
        step_ok = run2_min_step > run1_max_step
        print(f"[验证T4-d] run2_min_stepIndex={run2_min_step}, run1_max_stepIndex={run1_max_step}, "
              f"continuation={'OK' if step_ok else 'FAIL'}")
    else:
        print("[验证T4-d] run2 has no stepIndex events")

    # d. Seq continuity
    validation = validate_step_continuity(all_events)
    print(f"[验证] seq_continuous={validation['seq_continuous']}, range={validation['seq_range']}")

    # e. Runs.jsonl check
    import json
    from pathlib import Path
    runs_path = Path(store._root) / str(sid) / "runs.jsonl"
    runs = []
    with open(runs_path, encoding='utf-8') as f:
        for line in f:
            runs.append(json.loads(line))
    print(f"[验证] runs_count={len(runs)}, statuses={[r['status'] for r in runs]}")

    # f. Blob store: verify offload blobs are readable
    blobs_dir = Path(store._root) / "blobs"
    if blobs_dir.exists() and run1_offload:
        for ev in run1_offload[:3]:
            content_ref = getattr(ev.payload, 'content_ref', None)
            if content_ref:
                blob_content = store.get_blob(content_ref)
                blob_ok = blob_content is not None
                print(f"[验证T4-f] blob ref={content_ref[:16]}... readable={'OK' if blob_ok else 'FAIL'}")

    # Type distribution
    type_counts = Counter(ev.type.value for ev in all_events)
    print(f"[验证] types={dict(type_counts)}")

    # ContextManager stats
    print(f"[ContextManager] {shared_cm.get_all_compression_stats()}")

    print(f"\n{'='*60}")
    print("T4 测试完成")
    print(f"{'='*60}")
    return result2


async def test_t1_resume_from_existing():
    """T1 (offline): Resume from an existing completed session.
    Uses session 0b86666e which has 2 completed runs with compaction (18 events, seq 0-17).
    Only runs the resume part — no new Run1 needed.
    """
    from pathlib import Path as _Path
    EXISTING_SESSION_ID = "0b86666e-0950-4dda-95c5-f062aa752d39"
    LEAF_EVENT_UUID = "20fc1c5c-58d0-4c2f-ba6e-65a85f60ef94"

    persistence_dir = _Path(__file__).parent.parent / "persistence"
    from nexent.core.agents.agent_event.jsonl_store import JsonlEventStore
    store = JsonlEventStore(persistence_dir)

    from uuid import UUID as _UUID
    sid = _UUID(EXISTING_SESSION_ID)

    # Read existing events
    session_data = store.read_session(sid)
    existing_count = len(session_data.events)
    existing_step_indices = [ev.step_index for ev in session_data.events if ev.step_index is not None]
    max_existing_step = max(existing_step_indices) if existing_step_indices else 0
    print(f"\n{'='*60}")
    print("T1 (offline): Resume from existing session")
    print(f"{'='*60}")
    print(f"Session: {EXISTING_SESSION_ID}")
    print(f"Existing events: {existing_count}, max_stepIndex={max_existing_step}")

    # Run resume
    query = "简单解释一下什么是复数，用一句话回答"
    cm_config = ContextManagerConfig(
        enabled=True, token_threshold=3000, keep_recent_pairs=1,
    )
    shared_cm = ContextManager(config=cm_config, max_steps=3)

    run_info = build_agent_run_info(
        query, [],  # no history — resume path
        max_steps=10,
        context_manager_config=cm_config,
        event_store=store,
        session_id=EXISTING_SESSION_ID,
        resume_from=LEAF_EVENT_UUID,
        resume_fidelity="lossy",
    )
    run_info.context_manager = shared_cm

    print(f"\n--- Resume run ---")
    result = await run_agent_with_tracking(run_info)
    print(f"助手: {result.final_answer[:200]}")
    print(f"[Resume 统计] {result.message_type_count}")

    # Validate
    session_after = store.read_session(sid)
    all_events = session_after.events
    new_count = len(all_events) - existing_count

    # Seq continuity
    validation = validate_step_continuity(all_events)
    print(f"\n[验证] total_events={len(all_events)}, new_events={new_count}")
    print(f"[验证] seq_continuous={validation['seq_continuous']}, range={validation['seq_range']}")

    # stepIndex: resume agent steps (stepIndex >= 1) should continue from existing max.
    # Preamble events (stepIndex=0) are expected in each run's step 0.
    new_agent_step_indices = [
        ev.step_index for ev in all_events[existing_count:]
        if ev.step_index is not None and ev.step_index >= 1
    ]
    existing_agent_step_indices = [
        ev.step_index for ev in all_events[:existing_count]
        if ev.step_index is not None and ev.step_index >= 1
    ]
    max_existing_agent_step = max(existing_agent_step_indices) if existing_agent_step_indices else 0
    if new_agent_step_indices:
        min_new_agent_step = min(new_agent_step_indices)
        continuation_ok = min_new_agent_step > max_existing_agent_step
        print(f"[验证] resume_min_agent_stepIndex={min_new_agent_step}, "
              f"existing_max_agent_stepIndex={max_existing_agent_step}, "
              f"continuation={'OK' if continuation_ok else 'FAIL'}")
    else:
        print("[验证] resume has no agent step events (possible if only preamble)")

    # Type distribution
    type_counts = Counter(ev.type.value for ev in all_events)
    print(f"[验证] types={dict(type_counts)}")

    # Runs check
    import json
    runs_path = persistence_dir / EXISTING_SESSION_ID / "runs.jsonl"
    runs = []
    with open(runs_path, encoding='utf-8') as f:
        for line in f:
            runs.append(json.loads(line))
    print(f"[验证] runs_count={len(runs)}, statuses={[r['status'] for r in runs]}")

    print(f"\n{'='*60}")
    print("T1 (offline) 测试完成")
    print(f"{'='*60}")
    return result


async def test_p1_with_persistence():
    """P1 test with event persistence — same as test_p1_first_comp_and_sub_new_run_hit
    but events are written to persistence/ directory."""
    store, sid = create_event_store()

    agent_history = parse_conversation_to_history("./small_history.md")
    cm_config = ContextManagerConfig(
        enabled=True, token_threshold=3000, keep_recent_pairs=1,
        per_step_render_limit=500,
    )
    queries = [
        "请用 Python 生成一个 10x10 的随机矩阵，然后计算其行列式和所有特征值",
    ]
    results = await run_multi_turn(
        queries=queries,
        base_history=agent_history,
        cm_config=cm_config,
        max_steps=20,
        debug=False,
        event_store=store,
        session_id=str(sid),
    )

    # Inspect results
    from collections import Counter
    from pathlib import Path
    session_result = store.read_session(sid)
    type_counts = Counter(ev.type.value for ev in session_result.events)
    print(f"\n[EventPersistence] events={len(session_result.events)}, "
          f"has_gap={session_result.has_gap}, types={dict(type_counts)}")

    # Check offload and blobs
    offload_events = [ev for ev in session_result.events if ev.type.value == "offload_record"]
    print(f"[Offload] offload_record count={len(offload_events)}")
    blobs_dir = Path(store._root) / "blobs"
    if blobs_dir.exists():
        blob_files = list(blobs_dir.iterdir())
        print(f"[Blobs] dir={blobs_dir}, file_count={len(blob_files)}")
        for bf in blob_files[:5]:
            print(f"  {bf.name} ({bf.stat().st_size} bytes)")
    else:
        print("[Blobs] directory does not exist — no offload triggered")

    # Print offload event details
    for ev in offload_events:
        p = ev.payload
        print(f"  handle={getattr(p,'handle',None)}, originalChars={getattr(p,'original_chars',None)}, "
              f"contentRef={getattr(p,'content_ref',None)}, preview={getattr(p,'preview','')[:80]}")

    print_history_stats(agent_history)
    return results


# P1: 首次压缩且首次压缩后，后续命中[New的一步或多步]
async def test_p1_first_comp_and_sub_new_run_hit():
    """Previous Run 压缩开启（opt）——基于 history.md 的 3 轮对话。"""
    agent_history = parse_conversation_to_history("./small_history.md")
    cm_config = ContextManagerConfig(
        enabled=True, token_threshold=9000, keep_recent_pairs=1
    )
    queries = [
        "总结之前对话的主题是什么",
        "复数表示旋转，请使用python最基础的功能演示下",
    ]
    results = await run_multi_turn(
        queries=queries,
        base_history=agent_history,
        cm_config=cm_config,
        max_steps=5,
        debug=False,
    )
    print_history_stats(agent_history)
    return results


# P2: 增量压缩：先后两次压缩，且合理命中
async def test_p2_inc_comp_and_hit_valid():
    """Previous Run 压缩开启（opt）——基于 history.md 的 3 轮对话。"""
    agent_history = parse_conversation_to_history("./small_history.md")
    # import pdb; pdb.set_trace()
    cm_config = ContextManagerConfig(
        enabled=True, token_threshold=3600, keep_recent_pairs=1
    )
    queries = [
        "总结之前对话的主题是什么",
        "复数表示旋转，请使用python最基础的功能演示下",
        "请用 Python 计算 2 的 10 到 15 次方，并告诉我哪些是质数。",
    ]
    results = await run_multi_turn(
        queries=queries,
        base_history=agent_history,
        cm_config=cm_config,
        max_steps=5,
        debug=False,
    )
    print_history_stats(agent_history)
    return results


async def test_previous_run_overflow_baseline():
    """Previous Run 压缩禁用（baseline）——与 opt 做对照。"""
    agent_history = parse_conversation_to_history("./small_history.md")
    cm_config = ContextManagerConfig(
        enabled=False, token_threshold=10000, keep_recent_pairs=1
    )
    queries = [
        "总结之前对话的主题是什么",
    ]
    results = await run_multi_turn(
        queries=queries,
        base_history=agent_history,
        cm_config=cm_config,
        max_steps=5,
        debug=False,
    )
    print_history_stats(agent_history)
    return results


async def test_current_run_complex_baseline():
    """Current Run 压缩禁用（baseline）——复杂多步问题。"""
    base_history = parse_conversation_to_history("./small_history.md")
    cm_config = ContextManagerConfig(
        enabled=False,
        token_threshold=800,
        keep_recent_steps=1,
    )
    queries = ["请用 Python 计算 2 的 10 到 15 次方，并告诉我哪些是质数。请分步执行。接下来，已知复数表示旋转，请使用python最基础的功能演示下"]
    results = await run_multi_turn(
        queries=queries,
        base_history=base_history,
        cm_config=cm_config,
        max_steps=10,
        debug=False,
    )
    return results


async def test_current_run_complex_opt():
    """Current Run 压缩开启（opt）——同上复杂问题，断言发生压缩。"""
    base_history = parse_conversation_to_history("./small_history.md")
    cm_config = ContextManagerConfig(
        enabled=True,
        token_threshold=3600,
        keep_recent_steps=1,
    )
    shared_cm = ContextManager(config=cm_config, max_steps=10)
    queries = ["请用 Python 计算 2 的 10 到 15 次方，并告诉我哪些是质数。请分步执行。接下来，已知复数表示旋转，请使用python最基础的功能演示下"]
    results = await run_multi_turn(
        queries=queries,
        base_history=base_history,
        cm_config=cm_config,
        max_steps=10,
        debug=False,
    )
    stats = shared_cm.get_all_compression_stats()
    print(f"[CurrentRunOpt] Stats: {stats}")
    # assert stats["total_calls"] >= 1, "Current Run 应触发至少一次压缩"
    return results


async def test_current_run_complex_followup():
    """Current Run 压缩开启——两轮复杂任务，验证缓存复用。"""
    base_history = parse_conversation_to_history("./history.md")
    cm_config = ContextManagerConfig(
        enabled=True,
        token_threshold=800,
        keep_recent_steps=1,
        chars_per_token=1.0,
    )
    shared_cm = ContextManager(config=cm_config, max_steps=10)
    queries = [
        "请用 Python 计算 2 的 10 到 12 次方，并告诉我哪些是质数。请分步执行。",
        "生成一个4维的随机矩阵，元素服从[0,1)分布，计算其行列式、迹、秩、逆矩阵(如果可逆)，以及所有特征值和特征向量",
    ]
    results = await run_multi_turn(
        queries=queries,
        base_history=base_history,
        cm_config=cm_config,
        max_steps=10,
        debug=False,
    )
    stats = shared_cm.get_all_compression_stats()
    print(f"[CurrentRunFollowup] Stats: {stats}")
    return results


# =============================================================================
# 主入口：可按需选择运行
# =============================================================================

if __name__ == "__main__":
    # T4: Faithful resume with compaction + offload
    asyncio.run(test_t4_faithful_resume())
