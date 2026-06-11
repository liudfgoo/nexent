"""Working Memory runtime context message regression tests.

Maps to working-memory-test-matrix.md sections 2 & 4: prove that
``runtime_context_messages`` survive every compression path the ContextManager
takes (early return, stable bypass, full compression) and that they never leak
into the summary input.
"""

from factories import (
    make_cm,
    make_memory_mixed,
    make_model,
    make_original_messages,
    make_pair,
)
from loader import (
    AgentMemory,
    ChatMessage,
    ContextManager,
    MessageRole,
    PreviousSummaryCache,
    SystemPromptStep,
    TaskStep,
)


def _rt_messages(text="### Session State\n- task_target: prepare report"):
    return [
        ChatMessage(
            role=MessageRole.SYSTEM,
            content=[{"type": "text", "text": text}],
        )
    ]


def _flat_texts(messages):
    return [
        block.get("text", "")
        for msg in messages
        for block in (msg.content if isinstance(msg.content, list) else [])
        if isinstance(block, dict)
    ]


def _index_containing(texts, needle):
    for idx, txt in enumerate(texts):
        if needle in txt:
            return idx
    raise AssertionError(f"{needle!r} not found in {texts!r}")


class TestBuildMessagesRuntimeContext:
    def test_runtime_context_inserted_between_system_and_summary(self):
        cm = make_cm()
        prev_t, prev_a = make_pair("prev_task", "prev_action", 0)
        curr_t, curr_a = make_pair("curr_task", "curr_action", 1)
        memory = AgentMemory(
            steps=[prev_t, prev_a, curr_t, curr_a],
            system_prompt=SystemPromptStep(system_prompt="system prompt"),
        )
        from loader import SummaryTaskStep
        prev_summary = SummaryTaskStep(task="prev_summary_text")

        msgs = cm._build_messages(
            memory,
            prev_summary,
            [],
            [curr_t, curr_a],
            runtime_context_messages=_rt_messages("WM-MARK"),
        )

        texts = _flat_texts(msgs)
        sys_idx = _index_containing(texts, "system prompt")
        wm_idx = _index_containing(texts, "WM-MARK")
        sum_idx = _index_containing(texts, "prev_summary_text")
        curr_idx = _index_containing(texts, "curr_task")
        assert sys_idx < wm_idx < sum_idx < curr_idx

    def test_build_messages_without_runtime_context_unchanged(self):
        cm = make_cm()
        memory = AgentMemory(
            steps=[],
            system_prompt=SystemPromptStep(system_prompt="system prompt"),
        )
        msgs = cm._build_messages(memory, None, [], [])
        texts = _flat_texts(msgs)
        assert texts == ["system prompt"]


class TestCompressIfNeededRuntimeContext:
    def test_under_threshold_returns_original_unchanged(self):
        """Early return must not strip runtime context — caller is expected to
        have already included it via write_memory_to_messages."""
        cm = make_cm(enabled=True, threshold=999999)
        memory = make_memory_mixed(n_prev_pairs=1, n_curr_actions=1)
        original = make_original_messages(memory)
        runtime_msgs = _rt_messages()
        # Caller's contract: runtime messages live inside ``original`` already.
        original_with_rt = (
            memory.system_prompt.to_messages()
            + runtime_msgs
            + [m for m in original if m not in memory.system_prompt.to_messages()]
        )
        result = cm.compress_if_needed(
            None,
            memory,
            original_with_rt,
            current_run_start_idx=2,
            runtime_context_messages=runtime_msgs,
        )
        assert result is original_with_rt
        assert "### Session State" in " ".join(_flat_texts(result))

    def test_stable_bypass_preserves_runtime_context(self):
        cm = make_cm(enabled=True, threshold=10, keep_recent_pairs=0)
        pairs = [make_pair(f"task{i}", f"action{i}", i) for i in range(2)]
        steps = []
        for t, a in pairs:
            steps.extend([t, a])
        steps.append(TaskStep(task="new task"))
        memory = AgentMemory(
            steps=steps,
            system_prompt=SystemPromptStep(system_prompt="system prompt"),
        )
        last_t, last_a = pairs[-1]
        fp = cm._pair_fingerprint(last_t.task, last_a.action_output)
        cm._previous_summary_cache = PreviousSummaryCache("prev_sum", 2, fp)

        model = make_model()
        original = make_original_messages(memory)
        current_run_start_idx = 2 * len(pairs)

        result = cm.compress_if_needed(
            model,
            memory,
            original,
            current_run_start_idx,
            runtime_context_messages=_rt_messages("WM-STABLE"),
        )

        model.assert_not_called()
        texts = _flat_texts(result)
        # Order: system prompt, runtime context, prev summary, current task.
        sys_idx = _index_containing(texts, "system prompt")
        wm_idx = _index_containing(texts, "WM-STABLE")
        sum_idx = _index_containing(texts, "prev_sum")
        assert sys_idx < wm_idx < sum_idx

    def test_full_compression_preserves_runtime_context_and_skips_summary_input(self):
        cm = make_cm(
            enabled=True,
            threshold=10,
            keep_recent_pairs=1,
            keep_recent_steps=2,
        )
        memory = make_memory_mixed(n_prev_pairs=3, n_curr_actions=2)
        original = make_original_messages(memory)
        model = make_model('{"task_overview": "summary"}')
        runtime_msgs = _rt_messages("WM-FULL")

        result = cm.compress_if_needed(
            model,
            memory,
            original,
            current_run_start_idx=2 * 3,
            runtime_context_messages=runtime_msgs,
        )

        model.assert_called()
        texts = _flat_texts(result)
        sys_idx = _index_containing(texts, "system prompt")
        wm_idx = _index_containing(texts, "WM-FULL")
        sum_idx = _index_containing(texts, "Summary of earlier steps")
        assert sys_idx < wm_idx < sum_idx

        # Compression LLM input must NOT contain the runtime context
        # (otherwise the summary could absorb Working Memory state).
        for call in model.call_args_list:
            llm_messages = call.args[0]
            llm_texts = " ".join(_flat_texts(llm_messages))
            assert "WM-FULL" not in llm_texts

    def test_build_compressed_snapshot_passes_runtime_context(self):
        cm = make_cm(
            enabled=True,
            threshold=10,
            keep_recent_pairs=1,
            keep_recent_steps=2,
        )
        memory = make_memory_mixed(n_prev_pairs=3, n_curr_actions=2)
        model = make_model('{"task_overview": "summary"}')
        runtime_msgs = _rt_messages("WM-SNAPSHOT")

        compressed, metadata = cm.build_compressed_snapshot(
            model,
            memory,
            current_run_start_idx=2 * 3,
            runtime_context_messages=runtime_msgs,
        )

        texts = _flat_texts(compressed)
        assert any("WM-SNAPSHOT" in t for t in texts)
        assert metadata["token_counts"]["last_compressed"] is not None
