 
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
 
from unittest.mock import MagicMock, patch
 
from factories import make_cm, make_pair, make_model
from loader import (
    ActionStep,
    ContextManager,
    CurrentSummaryCache,
    PreviousSummaryCache,
    TaskStep,
)
 
 
# ──────────────────────────────────────────────────────────────
# Tools
# ──────────────────────────────────────────────────────────────
 
def _llm_text(model) -> str:
    """Extract the concatenated user prompt text from the last call of the mock model."""
    call_args = model.call_args[0][0]
    return " ".join(
        b.get("text", "")
        for m in call_args
        for b in (m.content if isinstance(m.content, list) else [])
        if isinstance(b, dict)
    )

def _all_texts(messages):
    return [
        b.get("text", "")
        for m in messages
        for b in (m.content if isinstance(m.content, list) else [])
        if isinstance(b, dict)
    ]
 
 
def _joined(messages):
    return " ".join(_all_texts(messages))
 
 
# ══════════════════════════════════════════════════════════════
# P series: _compress_previous_with_cache supplements
# ══════════════════════════════════════════════════════════════
 
class TestCompressPreviousExtra:
 
    # ── P1: full hit covered_pairs aligned but fp mismatch -> goes directly to fresh ──
 
    def test_P1_full_hit_fp_mismatch_goes_to_fresh(self):
        """covered_pairs == len(pairs) but fingerprint is wrong.
        Should not take the incremental path (condition covered < len not satisfied),
        directly enter fresh full compression.
        """
        cm = make_cm()
        pairs = [make_pair(f"task{i}", f"action{i}", i) for i in range(2)]
        # covered_pairs aligned, but fingerprint deliberately wrong
        cm._previous_summary_cache = PreviousSummaryCache(
            summary_text="old_summary", covered_pairs=2, anchor_fingerprint="WRONG"
        )
        model = make_model('{"task_overview": "fresh_summary"}')
        result = cm._compress_previous_with_cache(pairs, model)
 
        assert result is not None
        model.assert_called_once()
        # fresh path prompt should not contain old summary (no incremental prefix)
        assert "old_summary" not in _llm_text(model)
        # cache should be overwritten by fresh result
        assert cm._previous_summary_cache.covered_pairs == 2
 
    # ── P2: incremental path input_tokens over budget -> fall-through to fresh ──
 
    def test_P2_incremental_over_budget_falls_through_to_fresh(self):
        """Incremental input token count exceeds max_summary_input_tokens,
        should skip incremental and go directly to fresh, still calling LLM once (fresh).
        """
        cm = make_cm()
        # Set budget to 0, making all incremental inputs exceed budget
        cm.config.max_summary_input_tokens = 0
 
        pairs = [make_pair(f"task{i}", f"action{i}", i) for i in range(3)]
        anchor_t, anchor_a = pairs[1]
        fp = cm._pair_fingerprint(anchor_t.task, anchor_a.action_output)
        cm._previous_summary_cache = PreviousSummaryCache("old_summary", 2, fp)
 
        model = make_model('{"task_overview": "fresh_summary"}')
        # import pdb; pdb.set_trace()
 
        result = cm._compress_previous_with_cache(pairs, model)
        assert result is not None
        model.assert_called_once()
        # fresh path does not contain old summary incremental prefix
        assert "old_summary" not in _llm_text(model)
        # max_summary_input_tokens is 0, will enter summarize_pairs in fresh path
        # and will enter L2 trim, since max_summary_input_tokens is 0, almost completely trimmed, but will still keep the last pair
        # so there will be
        assert "task2" in _llm_text(model)
        assert "fresh" in result 
        
    # ── P3: incremental LLM returns None -> fall-through to fresh ──
 
    def test_P3_incremental_llm_none_falls_through_to_fresh(self):
        """When _generate_summary returns None on the incremental path,
        code falls through to fresh, and should ultimately call LLM once more.
        """
        cm = make_cm()
        pairs = [make_pair(f"task{i}", f"action{i}", i) for i in range(3)]
        anchor_t, anchor_a = pairs[1]
        fp = cm._pair_fingerprint(anchor_t.task, anchor_a.action_output)
        cm._previous_summary_cache = PreviousSummaryCache("old_summary", 2, fp)
 
        call_count = [0]
        def side_effect(text, model_, call_type="summary"):
            call_count[0] += 1
            if call_count[0] == 1:
                return None          # incremental call failed
            return '{"task_overview": "fresh_summary"}'  # fresh call succeeded
 
        with patch.object(cm, '_generate_summary', side_effect=side_effect):
            result = cm._compress_previous_with_cache(pairs, MagicMock())
 
        assert call_count[0] == 2    # incremental + fresh once each
        assert result is not None
 
 
    # ── P4: fresh LLM returns None -> return None, old cache not cleared ──
 
    def test_P4_fresh_llm_none_returns_none_and_preserves_old_cache(self):
        """When _summarize_pairs returns (None, False):
        - function returns None
        - existing _previous_summary_cache is not modified
        """
        cm = make_cm()
        pairs = [make_pair(f"task{i}", f"action{i}", i) for i in range(2)]
        # Pre-set an old cache (does not match current pairs, takes fresh path)
        cm._previous_summary_cache = PreviousSummaryCache("old_summary", 99, "bad_fp")
 
        with patch.object(cm, '_summarize_pairs', return_value=(None, False)):
            result = cm._compress_previous_with_cache(pairs, MagicMock())
 
        assert result is None
        # Old cache should not be overwritten (neither branch requires summary_text to be truthy)
        assert cm._previous_summary_cache.summary_text == "old_summary"


    def test_P4_fresh_llm_none_no_cache_remains_none(self):
        """When initially no cache, fresh LLM returns None -> cache remains None."""
        cm = make_cm()
        pairs = [make_pair("task", "action", 0)]
        assert cm._previous_summary_cache is None
 
        with patch.object(cm, '_summarize_pairs', return_value=(None, False)):
            result = cm._compress_previous_with_cache(pairs, MagicMock())
 
        assert result is None
        assert cm._previous_summary_cache is None


# ══════════════════════════════════════════════════════════════
# C series: _compress_current_with_cache supplements
# ══════════════════════════════════════════════════════════════
 
class TestCompressCurrentExtra:
 
    def _make_actions(self, n):
        return [
            ActionStep(step_number=i, model_output=f"output{i}", action_output=f"result{i}")
            for i in range(n)
        ]
 
    # ── C1: full hit end_steps aligned but fp mismatch -> goes directly to fresh ──
 
    def test_C1_full_hit_fp_mismatch_goes_to_fresh(self):
        """end_steps == len(actions) but anchor_fingerprint is wrong.
        Incremental condition 0 < end_steps < len not satisfied, directly go to fresh.
        """
        cm = make_cm()
        actions = self._make_actions(2)
        cm._current_summary_cache = CurrentSummaryCache(
            summary_text="old_summary", end_steps=2, anchor_fingerprint="WRONG"
        )
        model = make_model('{"task_overview": "fresh_summary"}')
        result = cm._compress_current_with_cache(TaskStep(task="t"), actions, model)
 
        assert result is not None
        assert "fresh_summary" in result 
        assert "old_summary" not in result
        model.assert_called_once()
        # cache is overwritten by fresh result, fingerprint updated to real value
        real_fp = ContextManager._action_fingerprint(actions[-1])
        assert cm._current_summary_cache.anchor_fingerprint == real_fp


    # ── C2: incremental anchor action fp mismatch -> goes to fresh ──
 
    def test_C2_incremental_anchor_fp_mismatch_goes_to_fresh(self):
        """cache.end_steps < len(actions) (incremental condition satisfied),
        but anchor action's fingerprint does not match cache -> fall-through to fresh.
        """
        cm = make_cm()
        actions = self._make_actions(3)
        # end_steps=2 (< 3), but fingerprint deliberately wrong
        cm._current_summary_cache = CurrentSummaryCache(
            summary_text="old_summary", end_steps=2, anchor_fingerprint="WRONG"
        )
        model = make_model('{"task_overview": "fresh_summary"}')
        result = cm._compress_current_with_cache(TaskStep(task="t"), actions, model)
 
        assert result is not None
        model.assert_called_once()
        # fresh path prompt does not contain old summary incremental prefix
        assert "old_summary" not in _llm_text(model)
        assert "fresh_summary" in  result

    # ── C4: incremental LLM returns None -> fall-through to fresh ──
 
    def test_C4_incremental_llm_none_falls_through_to_fresh(self):
        cm = make_cm()
        actions = self._make_actions(3)
        fp = ContextManager._action_fingerprint(actions[1])
        cm._current_summary_cache = CurrentSummaryCache("old_summary", 2, fp)
 
        call_count = [0]
        def side_effect(text, model_, call_type="summary"):
            call_count[0] += 1
            if call_count[0] == 1:
                return None
            return '{"task_overview": "fresh_summary"}'
 
        with patch.object(cm, '_generate_summary', side_effect=side_effect):
            result = cm._compress_current_with_cache(TaskStep(task="t"), actions, MagicMock())
 
        assert call_count[0] == 2
        assert result is not None
        # fresh path writes new cache, end_steps is original len
        assert cm._current_summary_cache.end_steps == len(actions)


  # ── C5: fresh actions trimmed (is_full_coverage=False) ──
 
    def test_C5_fresh_actions_trimmed_cache_uses_original_len(self):
        """_trim_actions_to_budget trimmed some actions,
        but end_steps should still record original len(actions_to_compress),
        ensuring cache covers the same range on next call.
        """
        cm = make_cm()
        actions = self._make_actions(4)
 
        # Make _trim_actions_to_budget keep only the last 1
        with patch.object(cm, '_trim_actions_to_budget', return_value=[actions[-1]]):
            model = make_model('{"task_overview": "trimmed_summary"}')
            result = cm._compress_current_with_cache(TaskStep(task="t"), actions, model)
 
        assert result is not None
        # end_steps should be original len=4, not 1 after trim
        assert cm._current_summary_cache.end_steps == 4
        # anchor_fingerprint should be fingerprint of original last action
        real_fp = ContextManager._action_fingerprint(actions[-1])
        assert cm._current_summary_cache.anchor_fingerprint == real_fp

    def test_C5_fresh_partial_trim_still_calls_llm_once(self):
        """After trim occurs, still only call LLM once (no retry)."""
        cm = make_cm()
        actions = self._make_actions(3)
 
        with patch.object(cm, '_trim_actions_to_budget', return_value=[actions[-1]]):
            model = make_model('{"task_overview": "summary"}')
            cm._compress_current_with_cache(TaskStep(task="t"), actions, model)
 
        model.assert_called_once()


    # ── C6: fresh LLM returns None -> cache writes None, return None ──
 
    def test_C6_fresh_llm_none_writes_none_to_cache(self):
        """If LLM call fails on current's fresh path, there will be no cache.
        At this time, only truncate.
        """
        cm = make_cm()
        actions = self._make_actions(2)
 
        with patch.object(cm, '_generate_summary', return_value=None):
            result = cm._compress_current_with_cache(TaskStep(task="t"), actions, MagicMock())
 
        assert "Truncated" in result
        assert cm._current_summary_cache is None



    def test_C6_vs_previous_asymmetry(self):
        """Regression test: clarify the asymmetry between previous and current behavior when LLM=None.
        previous fresh=None -> cache not written (old value kept)
        current fresh=None -> cache not written
        """
        cm = make_cm()
        pairs = [make_pair("task", "action", 0)]
        actions = [ActionStep(step_number=0, model_output="out", action_output="r")]
 
        old_prev_cache = PreviousSummaryCache("old_prev", 99, "bad")
        cm._previous_summary_cache = old_prev_cache
 
        with patch.object(cm, '_summarize_pairs', return_value=(None, False)):
            cm._compress_previous_with_cache(pairs, MagicMock())
        # previous: old cache kept
        assert cm._previous_summary_cache is old_prev_cache
 
        with patch.object(cm, '_generate_summary', return_value=None):
            cm._compress_current_with_cache(TaskStep(task="t"), actions, MagicMock())
        # current: if generate summary fails, _current_summary_cache remains as is, for this test, it is None
        assert cm._current_summary_cache is  None
