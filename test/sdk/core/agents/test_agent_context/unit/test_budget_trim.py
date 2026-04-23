from factories import make_cm, make_pair
from loader import ActionStep

class TestBudgetTrimming:

    def test_trim_pairs_within_budget_returns_all(self):
        cm = make_cm()
        pairs = [make_pair(f"task{i}", f"action{i}", i) for i in range(3)]
        result = cm._trim_pairs_to_budget(pairs, max_tokens=99999)
        assert len(result) == 3

    def test_trim_pairs_empty_input(self):
        cm = make_cm()
        assert cm._trim_pairs_to_budget([], max_tokens=1000) == []

    def test_trim_pairs_keeps_at_least_last_when_all_overflow(self):
        """Even with a minimal budget, at least the last pair is kept"""
        cm = make_cm()
        pairs = [make_pair("Very long task description" * 50, "Very long answer content" * 50, i) for i in range(3)]
        result = cm._trim_pairs_to_budget(pairs, max_tokens=1, keep_first=False)
        assert len(result) == 1

    def test_trim_pairs_keep_first_true_keeps_first_pair(self):
        """When keep_first=True, the first pair must be kept"""
        cm = make_cm()
        pairs = [make_pair(f"task{i}", f"action{i}", i) for i in range(5)]
        # Set an extremely small budget that only fits 1 pair
        first_pair_tokens = cm._estimate_text_tokens(cm._pairs_to_text([pairs[0]]))
        result = cm._trim_pairs_to_budget(pairs, max_tokens=first_pair_tokens + 5, keep_first=True)
        assert result[0] == pairs[0]

    def test_trim_actions_within_budget_returns_all(self):
        cm = make_cm()
        actions = [ActionStep(step_number=i, model_output=f"output{i}") for i in range(3)]
        result = cm._trim_actions_to_budget(actions, task_text="", max_tokens=99999)
        assert len(result) == 3

    def test_trim_actions_empty_returns_empty(self):
        cm = make_cm()
        assert cm._trim_actions_to_budget([], task_text="", max_tokens=1000) == []

    def test_trim_actions_keeps_last_when_overflow(self):
        """Under extreme budgets, at least the last action is kept"""
        cm = make_cm()
        actions = [
            ActionStep(step_number=i, model_output="X" * 500, action_output="Y" * 500)
            for i in range(4)
        ]
        result = cm._trim_actions_to_budget(actions, task_text="", max_tokens=1)
        assert len(result) >= 1
        assert result[-1] is actions[-1]

    def test_trim_actions_skips_drop_that_splits_tool_call_and_observation(self):
        """When a truncation point would split tool_calls and observations, the truncation point should be skipped"""
        cm = make_cm()
        actions = [
            ActionStep(step_number=0, model_output="A" * 400, tool_calls=[{"name": "tool1"}]),
            ActionStep(step_number=1, model_output="B" * 400, observations="obs1"),
            ActionStep(step_number=2, model_output="C" * 400),
        ]
        # Budget is set between 2 actions and 3 actions:
        # Normally drop=1 can keep [action1, action2], but because action0 has tool_calls
        # and action1 has observations, this truncation point is skipped, so finally drop=2, keeping [action2]
        two_act_tokens = cm._estimate_text_tokens(cm._actions_to_text(actions[1:]))
        three_act_tokens = cm._estimate_text_tokens(cm._actions_to_text(actions))
        max_tokens = two_act_tokens + (three_act_tokens - two_act_tokens) // 2

        result = cm._trim_actions_to_budget(actions, task_text="", max_tokens=max_tokens)
        assert result == [actions[2]]

    def test_trim_actions_allows_drop_when_no_tool_call_before_observation(self):
        """remaining[0] has observations, but the previous action has no tool_calls, so truncation should be allowed"""
        cm = make_cm()
        actions = [
            ActionStep(step_number=0, model_output="A" * 400),  # no tool_calls
            ActionStep(step_number=1, model_output="B" * 400, observations="obs1"),
            ActionStep(step_number=2, model_output="C" * 400),
        ]
        two_act_tokens = cm._estimate_text_tokens(cm._actions_to_text(actions[1:]))
        three_act_tokens = cm._estimate_text_tokens(cm._actions_to_text(actions))
        max_tokens = two_act_tokens + (three_act_tokens - two_act_tokens) // 2

        result = cm._trim_actions_to_budget(actions, task_text="", max_tokens=max_tokens)
        assert result == [actions[1], actions[2]]

    def test_trim_actions_allows_drop_when_no_observation_after_tool_call(self):
        """actions[drop-1] has tool_calls, but remaining[0] has no observations, so truncation should be allowed"""
        cm = make_cm()
        actions = [
            ActionStep(step_number=0, model_output="A" * 400, tool_calls=[{"name": "tool1"}]),
            ActionStep(step_number=1, model_output="B" * 400),  # no observations
            ActionStep(step_number=2, model_output="C" * 400),
        ]
        two_act_tokens = cm._estimate_text_tokens(cm._actions_to_text(actions[1:]))
        three_act_tokens = cm._estimate_text_tokens(cm._actions_to_text(actions))
        max_tokens = two_act_tokens + (three_act_tokens - two_act_tokens) // 2

        result = cm._trim_actions_to_budget(actions, task_text="", max_tokens=max_tokens)
        assert result == [actions[1], actions[2]]

    def test_trim_actions_chain_pairs_fallback_returns_complete_pair(self):
        """When continuous pairing causes all suffix truncation points to be illegal or over budget, fallback returns the last complete tool_call+observation pair"""
        cm = make_cm()
        actions = [
            ActionStep(step_number=0, model_output="A" * 400, tool_calls=[{"name": "t1"}]),
            ActionStep(step_number=1, model_output="B" * 400, observations="obs1"),
            ActionStep(step_number=2, model_output="C" * 400, tool_calls=[{"name": "t2"}]),
            ActionStep(step_number=3, model_output="D" * 400, observations="obs2"),
        ]
        result = cm._trim_actions_to_budget(actions, task_text="", max_tokens=1)
        # drop=1 skip(T0→O1), drop=2 [T2,O3] over budget, drop=3 skip(T2→O3)
        # fallback keeps the last complete pair [T2, O3]
        assert result == [actions[2], actions[3]]

    def test_trim_actions_fallback_returns_pair_when_last_is_observation(self):
        """During fallback, if the last action is an observation and the previous one has tool_calls, return the complete pair"""
        cm = make_cm()
        actions = [
            ActionStep(step_number=0, model_output="A" * 400),
            ActionStep(step_number=1, model_output="B" * 400, tool_calls=[{"name": "t1"}]),
            ActionStep(step_number=2, model_output="C" * 400, observations="obs1"),
        ]
        result = cm._trim_actions_to_budget(actions, task_text="", max_tokens=1)
        assert result == [actions[1], actions[2]]

    def test_trim_actions_fallback_returns_single_when_last_has_no_observation(self):
        """During fallback, if the last action has no observations, only return the single last one"""
        cm = make_cm()
        actions = [
            ActionStep(step_number=0, model_output="A" * 400),
            ActionStep(step_number=1, model_output="B" * 400),
            ActionStep(step_number=2, model_output="C" * 400),
        ]
        result = cm._trim_actions_to_budget(actions, task_text="", max_tokens=1)
        assert result == [actions[-1]]
