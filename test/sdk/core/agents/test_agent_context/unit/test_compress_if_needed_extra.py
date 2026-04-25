"""
unit/test_compress_if_needed_extra.py
──────────────────────────────────────
Supplement missing branch coverage for TestCompressIfNeeded.
 
Existing tests already cover:
  G1 disabled / under-threshold / run-boundary / G2 both-cache / G2 prev-only /
  G2 curr-only / main-path prev+curr both compress / main-path mixed
 
This file supplements (corresponding to M1-M13 in the branch diagram):
  M1  First call _last_run_start_idx=None -> no exception, no cache clear
  M2  G2 shortcut with no cache returns raw messages (no LLM call)
  M3  compress_prev=True but pairs_to_compress is empty (keep_n >= all pairs)
  M4  compress_prev=True, LLM returns None -> raw prev shown as usual, no crash
  M5  compress_prev=False and prev cache valid -> main path stable phase applies cache (non-G2)
  M6  compress_curr=True but actions_to_compress is empty
  M7  compress_curr=True, LLM returns None -> raw curr shown as usual, no crash
  M8  compress_curr=False and curr cache valid -> main path stable phase applies cache (non-G2)
  M9  Only current-run (current_run_start_idx=0), no previous, over threshold, no cache
  M10 keep_recent_pairs exceeds total pairs boundary handling
  M11 prev+curr both LLM fail -> return value is still list, no crash
  M12 No system_prompt -> no system message in result
  M13 Each compress call clears _step_local_log
"""
 
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
 
from unittest.mock import MagicMock, patch
 
from factories import make_cm, make_pair, make_model, make_original_messages
from loader import (
    ActionStep,
    AgentMemory,
    ContextManager,
    ContextManagerConfig,
    CurrentSummaryCache,
    PreviousSummaryCache,
    SummaryTaskStep,
    TaskStep,
)
from stubs import _SystemPromptStep as SystemPromptStep
 
 
# ──────────────────────────────────────────────────────────────
# Utility: Extract all text blocks from message list
# ──────────────────────────────────────────────────────────────
 
def _all_texts(messages):
    return [
        b.get("text", "")
        for m in messages
        for b in (m.content if isinstance(m.content, list) else [])
        if isinstance(b, dict)
    ]
 
 
def _joined(messages):
    return " ".join(_all_texts(messages))
 
 
# ──────────────────────────────────────────────────────────────
# M1  First call _last_run_start_idx=None -> no exception, no cache clear
# ──────────────────────────────────────────────────────────────
 
class TestM1FirstCall:
 
    def test_first_call_no_exception_and_no_cache_clear(self):
        """Initial state _last_run_start_idx=None, first call should not clear current cache."""
        cm = make_cm(enabled=True, threshold=999999)  # High threshold, directly takes G1 under-threshold
        cm._current_summary_cache = CurrentSummaryCache("existing_summary", 1, "fp")
        assert cm._last_run_start_idx is None
 
        t, a = make_pair("task", "action", 0)
        memory = AgentMemory(steps=[t, a], system_prompt=None)
        original = make_original_messages(memory)
 
        result = cm.compress_if_needed(None, memory, original, current_run_start_idx=2)
 
        # G1 short-circuit returns directly, current cache should not be cleared
        assert result is original
        assert cm._current_summary_cache is not None
# ──────────────────────────────────────────────────────────────
# M2  G2 shortcut no cache: effective <= threshold, but no valid cache
# ──────────────────────────────────────────────────────────────
 
class TestM2G2NoCacheRawReturn:
 
    def test_g2_shortcut_no_cache_returns_raw_messages(self):
        """effective <= threshold but no cache available, should assemble raw steps directly using _build_messages."""
        cm = make_cm(enabled=True, threshold=10)
        # Very short content, effective tokens must be <= 10
        t, a = make_pair("x", "y", 0)
        memory = AgentMemory(steps=[t, a], system_prompt=None)
        original = make_original_messages(memory)

        # Ensure _estimate_tokens > threshold (triggers entering function body), but _effective_tokens <= threshold
        with patch.object(cm, '_estimate_tokens', return_value=50):
            with patch.object(cm, '_effective_tokens', return_value=5):
                model = make_model()
                result = cm.compress_if_needed(model, memory, original, current_run_start_idx=2)
 
        model.assert_not_called()
        assert isinstance(result, list)
        # No summary: result should not contain "Summary of earlier steps"
        assert "Summary of earlier steps" not in _joined(result)
        # Raw step content should still appear
        assert "x" in _joined(result)

# ──────────────────────────────────────────────────────────────
# M3  compress_prev=True but pairs_to_compress is empty (keep_n >= all pairs)
# ──────────────────────────────────────────────────────────────
 
class TestM3PairsToCompressEmpty:
 
    def test_compress_prev_true_but_all_pairs_kept_no_llm(self):
        """keep_recent_pairs >= len(pairs), pairs_to_compress=[], LLM should not be called.
        All previous pairs should be retained (in raw form) in the result.
        """
        # keep_recent_pairs=10 is much larger than actual pair count 2
        cm = make_cm(enabled=True, threshold=1, keep_recent_pairs=10)
        t0, a0 = make_pair("task0 " + "X" * 50, "action0 " + "Y" * 50, 0)
        t1, a1 = make_pair("task1 " + "X" * 50, "action1 " + "Y" * 50, 1)
        memory = AgentMemory(steps=[t0, a0, t1, a1], system_prompt=None)
        original = make_original_messages(memory)

        model = make_model('{"task_overview": "summary"}')
        # All are previous-run
        result = cm.compress_if_needed(model, memory, original, current_run_start_idx=4)

        model.assert_not_called()
        assert isinstance(result, list)
        # Both task contents should appear
        assert "task0" in _joined(result)
        assert "task1" in _joined(result)

# ──────────────────────────────────────────────────────────────
# M4  compress_prev=True, LLM returns None -> graceful degradation
# ──────────────────────────────────────────────────────────────
 
class TestM4PrevLLMReturnsNone:
 
    def test_prev_llm_returns_none_raw_steps_shown(self):
        """When _compress_previous_with_cache returns None, prev_summary_step=None,
        raw prev steps should still appear in the result, no crash.
        """
        cm = make_cm(enabled=True, threshold=1, keep_recent_pairs=1)
        t0, a0 = make_pair("task0 " + "X" * 50, "action0 " + "Y" * 50, 0)
        t1, a1 = make_pair("task1 " + "X" * 50, "action1 " + "Y" * 50, 1)
        memory = AgentMemory(steps=[t0, a0, t1, a1], system_prompt=None)
        original = make_original_messages(memory)
 
        with patch.object(cm, '_compress_previous_with_cache', return_value=None):
            model = make_model()
            result = cm.compress_if_needed(model, memory, original, current_run_start_idx=4)
 
        assert isinstance(result, list)
        # No summary step
        assert "Summary of earlier steps" not in _joined(result)
        # keep_recent_pairs=1: pairs_to_keep=[pair1], pair0 is compressed but LLM returns None so prev_summary_step=None
        # Actual result should contain task1 content
        assert "task1" in _joined(result)

 
 
# ──────────────────────────────────────────────────────────────
# M5  compress_prev=False and prev cache valid -> main path stable phase applies cache
# ──────────────────────────────────────────────────────────────
 
class TestM5PrevCacheInMainPath:
 
    def test_compress_prev_false_with_valid_cache_applied_in_main_path(self):
        """
        Scenario: effective_tokens > threshold (enters main path),
        but prev_tokens <= threshold*0.6 (compress_prev=False),
        and prev cache is valid -> takes elif branch to apply prev cache.
        This differs from G2 shortcut: G2 short-circuits when effective <= threshold.
        """
        cm = make_cm(enabled=True, threshold=100, keep_recent_pairs=1)

        # Ensure the example prev length is significantly larger than the previous_summary_cache content built below
        t, a = make_pair("prev_task" + "X" * 200, "prev_action" + "Y" * 200, 0)
        curr_t, curr_a = make_pair("curr_task " + "X" * 200, "curr_action " + "Y" * 200, 1)
        memory = AgentMemory(
            steps=[t, a, curr_t, curr_a],
            system_prompt=SystemPromptStep(system_prompt="sys"),
        )
 
        fp = cm._pair_fingerprint(t.task, a.action_output)
        cm._previous_summary_cache = PreviousSummaryCache("prev_cached_summary", 1, fp)

        # Control token distribution: prev is small (does not trigger compress_prev), curr is large (triggers compress_curr)
        # effective total > threshold (G2 does not short-circuit), prev <= 60 (compress_prev=False)
        def mock_effective_prev(steps):
            return 40  # <= 60 = 100*0.6

        def mock_effective_curr(steps):
            return 80  # > 40 = 100*0.4, triggers compress_curr

        with patch.object(cm, '_effective_prev_tokens', side_effect=mock_effective_prev):
            with patch.object(cm, '_effective_curr_tokens', side_effect=mock_effective_curr):
                # effective_tokens = sys(~2) + 40 + 80 = ~122 > 100, G2 does not trigger
                model = make_model('{"task_overview": "curr_summary"}')
                original = make_original_messages(memory)
                result = cm.compress_if_needed(model, memory, original, current_run_start_idx=2)
        texts = _all_texts(result)
        # prev cache should be applied: summary content should appear
        assert any("prev_cached_summary" in t for t in texts)
        # curr is compressed: Summary of earlier steps should appear
        assert any("Summary of earlier steps" in t for t in texts)
 
 
# ──────────────────────────────────────────────────────────────
# M6  compress_curr=True but actions_to_compress is empty
# ──────────────────────────────────────────────────────────────
 
class TestM6ActionsToCompressEmpty:
 
    def test_compress_curr_true_but_all_actions_kept_no_llm(self):
        """keep_recent_steps >= len(action_steps), actions_to_compress=[], LLM should not be called."""
        cm = make_cm(enabled=True, threshold=1, keep_recent_steps=10)
        curr_t = TaskStep(task="current_task")
        curr_a0 = ActionStep(step_number=0, model_output="output0 " + "Y" * 50, action_output="r0")
        curr_a1 = ActionStep(step_number=1, model_output="output1 " + "Y" * 50, action_output="r1")
        memory = AgentMemory(steps=[curr_t, curr_a0, curr_a1], system_prompt=None)
        original = make_original_messages(memory)
 
        model = make_model('{"task_overview": "summary"}')
        # current_run_start_idx=0: all are current-run
        result = cm.compress_if_needed(model, memory, original, current_run_start_idx=0)
 
        model.assert_not_called()
        assert isinstance(result, list)
        assert "output0" in _joined(result)
        assert "output1" in _joined(result)

# ──────────────────────────────────────────────────────────────
# M7  compress_curr=True, LLM returns None -> graceful degradation
# ──────────────────────────────────────────────────────────────
 
class TestM7CurrLLMReturnsNone:
 
    def test_curr_llm_returns_none_raw_curr_shown(self):
        """When _compress_current_with_cache returns None, curr_kept_steps=list(curr_steps), no crash."""
        cm = make_cm(enabled=True, threshold=1, keep_recent_steps=1)
        curr_t = TaskStep(task="current_task")
        curr_a0 = ActionStep(step_number=0, model_output="output0 " + "Y" * 50, action_output="r0")
        curr_a1 = ActionStep(step_number=1, model_output="output1 " + "Y" * 50, action_output="r1")
        memory = AgentMemory(steps=[curr_t, curr_a0, curr_a1], system_prompt=None)
        original = make_original_messages(memory)
 
        with patch.object(cm, '_compress_current_with_cache', return_value=None):
            model = make_model()
            result = cm.compress_if_needed(model, memory, original, current_run_start_idx=0)
 
        assert isinstance(result, list)
        # No summary, raw curr steps displayed directly
        assert "Summary of earlier steps" not in _joined(result)
        assert "output0" in _joined(result)
        assert "output1" in _joined(result)

 
# ──────────────────────────────────────────────────────────────
# M8  compress_curr=False and curr cache valid -> main path stable phase applies cache
# ──────────────────────────────────────────────────────────────
 
class TestM8CurrCacheInMainPath:
 
    def test_compress_curr_false_with_valid_cache_applied_in_main_path(self):
        """
        Scenario: effective_tokens > threshold,
        prev_tokens > threshold*0.6 (compress_prev=True),
        curr_tokens <= threshold*0.4 (compress_curr=False),
        and curr cache is valid -> takes elif branch to apply curr cache.
        """
        cm = make_cm(enabled=True, threshold=100, keep_recent_pairs=1)
 
        t0, a0 = make_pair("prev0 " + "X" * 100, "pa0 " + "Y" * 100, 0)
        t1, a1 = make_pair("prev1 " + "X" * 100, "pa1 " + "Y" * 100, 1)
        curr_t = TaskStep(task="curr_task")
        curr_a = ActionStep(step_number=2, model_output="curr_out", action_output="curr_r")
        memory = AgentMemory(
            steps=[t0, a0, t1, a1, curr_t, curr_a],
            system_prompt=SystemPromptStep(system_prompt="sys"),
        )
 
        fp = ContextManager._action_fingerprint(curr_a)
        cm._current_summary_cache = CurrentSummaryCache("curr_cached_summary", 1, fp)

        def mock_effective_prev(steps):
            return 80  # > 60 = 100*0.6 -> compress_prev=True

        def mock_effective_curr(steps):
            return 30  # <= 40 = 100*0.4 -> compress_curr=False

        with patch.object(cm, '_effective_prev_tokens', side_effect=mock_effective_prev):
            with patch.object(cm, '_effective_curr_tokens', side_effect=mock_effective_curr):
                model = make_model('{"task_overview": "prev_summary"}')
                original = make_original_messages(memory)
                result = cm.compress_if_needed(model, memory, original, current_run_start_idx=4)
 
        texts = _all_texts(result)
        # curr cache should be applied
        assert any("curr_cached_summary" in t for t in texts)
        # prev is compressed by LLM
        model.assert_called_once()
        assert "prev_summary" in _joined(result)
 
 
# ──────────────────────────────────────────────────────────────
# M9  Only current-run, no previous, over threshold, no cache
# ──────────────────────────────────────────────────────────────
 
class TestM9OnlyCurrentNoCache:
 
    def test_only_current_run_over_threshold_triggers_curr_compression(self):
        """current_run_start_idx=0: all are current-run, no prev, over threshold, no cache.
        Should only compress curr and call LLM once.
        """
        cm = make_cm(enabled=True, threshold=1, keep_recent_steps=1)
        curr_t = TaskStep(task="current_task " + "X" * 50)
        actions = [
            ActionStep(step_number=i, model_output=f"output{i} " + "Y" * 50, action_output=f"r{i}")
            for i in range(3)
        ]
        memory = AgentMemory(steps=[curr_t] + actions, system_prompt=None)
        original = make_original_messages(memory)
 
        model = make_model('{"task_overview": "curr_summary"}')
        result = cm.compress_if_needed(model, memory, original, current_run_start_idx=0)
 
        assert result is not None
        assert isinstance(result, list)
        assert len(result) < len(original)
        model.assert_called_once()
        assert "Summary of earlier steps" in _joined(result)

 
# ──────────────────────────────────────────────────────────────
# M10 keep_recent_pairs boundary: exceeding actual pair count is equivalent to not dropping any pair
# ──────────────────────────────────────────────────────────────
 
class TestM10KeepRecentPairsBoundary:
 
    def test_keep_recent_pairs_larger_than_total_pairs_keeps_all(self):
        """When keep_recent_pairs=999, pairs_to_compress=[], all pairs are retained as-is."""
        cm = make_cm(enabled=True, threshold=1, keep_recent_pairs=999)
        pairs = [make_pair(f"task{i} " + "X" * 20, f"action{i} " + "Y" * 20, i) for i in range(3)]
        steps = [s for t, a in pairs for s in (t, a)]
        memory = AgentMemory(steps=steps, system_prompt=None)
        original = make_original_messages(memory)
 
        model = make_model('{"task_overview": "summary"}')
        result = cm.compress_if_needed(model, memory, original, current_run_start_idx=6)
 
        model.assert_not_called()
        for i in range(3):
            assert f"task{i}" in _joined(result)

 
 
# ──────────────────────────────────────────────────────────────
# M11 prev+curr both LLM fail -> return value is still list, no crash
# ──────────────────────────────────────────────────────────────
 
class TestM11BothLLMFail:
 
    def test_both_llm_calls_return_none_still_returns_list(self):
        """When both compression calls return None, the result is still a valid list, no exception thrown."""
        cm = make_cm(enabled=True, threshold=1, keep_recent_pairs=1, keep_recent_steps=1)
 
        t0, a0 = make_pair("prev " + "X" * 50, "pa " + "Y" * 50, 0)
        t1, a1 = make_pair("prev1 " + "X" * 50, "pa1 " + "Y" * 50, 1)
        curr_t = TaskStep(task="curr_task " + "X" * 50)
        curr_a0 = ActionStep(step_number=2, model_output="cout0 " + "Y" * 50, action_output="r0")
        curr_a1 = ActionStep(step_number=3, model_output="cout1 " + "Y" * 50, action_output="r1")
        memory = AgentMemory(
            steps=[t0, a0, t1, a1, curr_t, curr_a0, curr_a1],
            system_prompt=SystemPromptStep(system_prompt="sys"),
        )
        original = make_original_messages(memory)
 
        with patch.object(cm, '_compress_previous_with_cache', return_value=None):
            with patch.object(cm, '_compress_current_with_cache', return_value=None):
                result = cm.compress_if_needed(None, memory, original, current_run_start_idx=4)
 
        assert isinstance(result, list)
        assert len(result) > 0
 
 # ──────────────────────────────────────────────────────────────
# M12 No system_prompt -> no system message in result
# ──────────────────────────────────────────────────────────────
 
class TestM12NoSystemPrompt:
 
    def test_no_system_prompt_no_system_message_in_result(self):
        """When memory.system_prompt=None, _build_messages should not produce system role messages."""
        from stubs import _MessageRole
        cm = make_cm(enabled=True, threshold=1, keep_recent_pairs=1)
        t, a = make_pair("task " + "X" * 50, "action " + "Y" * 50, 0)
        t1, a1 = make_pair("task1 " + "X" * 50, "action1 " + "Y" * 50, 1)
        memory = AgentMemory(steps=[t, a, t1, a1], system_prompt=None)
        original = make_original_messages(memory)
 
        model = make_model('{"task_overview": "summary"}')
        result = cm.compress_if_needed(model, memory, original, current_run_start_idx=4)
 
        roles = [m.role for m in result]
        assert _MessageRole.SYSTEM not in roles

 
 
# ──────────────────────────────────────────────────────────────
# M13 Each compress call clears _step_local_log (does not accumulate across steps)
# ──────────────────────────────────────────────────────────────
 
class TestM13StepLocalLogCleared:
 
    def test_step_local_log_cleared_at_start_of_each_compress_call(self):
        """Two consecutive compression calls, the second _step_local_log should not contain records from the first."""
        cm = make_cm(enabled=True, threshold=1, keep_recent_pairs=1)
 
        def _make_mem():
            t0, a0 = make_pair("task0 " + "X" * 50, "action0 " + "Y" * 50, 0)
            t1, a1 = make_pair("task1 " + "X" * 50, "action1 " + "Y" * 50, 1)
            return AgentMemory(steps=[t0, a0, t1, a1], system_prompt=None)
 
        model = make_model('{"task_overview": "summary"}')
 
        mem1 = _make_mem()
        cm.compress_if_needed(model, mem1, make_original_messages(mem1), current_run_start_idx=4)
        count_after_first = len(cm._step_local_log)
        assert count_after_first == 1
        assert cm._step_local_log[0].call_type == "previous_summary"
 
        mem2 = _make_mem()
        cm.compress_if_needed(model, mem2, make_original_messages(mem2), current_run_start_idx=4)
        count_after_second = len(cm._step_local_log)
        # reuse Previous_summary_cache; cache hit is still recorded in _step_local_log
        assert count_after_second == 1
        assert cm._step_local_log[0].call_type == "previous_cache_hit"
