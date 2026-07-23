import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from sdk.benchmark.generic.run_context_manager_comparison import (
    GroupSpec,
    aggregate_provider_cache,
    aggregate_summary_cache,
    build_run_name,
    build_runner_command,
    comparison_groups,
    fetch_complete_dataset_run,
    fetch_run_results,
    paired_outcomes,
    validate_runner_args,
)


def test_comparison_groups_isolate_runtime_and_compression_threshold():
    groups = comparison_groups(1_000_000, 10_000)

    assert [group.key for group in groups] == ["A", "B", "C"]
    assert groups[0].runner_args == ("--disable-context-manager",)
    assert groups[1].runner_args[-1] == "1000000"
    assert groups[2].runner_args[-1] == "10000"


def test_runner_command_contains_owned_group_configuration():
    group = GroupSpec("B", "managed-no-compression", ("--enable-context-manager",))

    command = build_runner_command(
        python_executable="python",
        dataset="gaia",
        run_name="comparison-formal-r01-b",
        group=group,
        runner_args=["--evaluators", "gaia_exact_match"],
        item_limit=2,
        experiment_time="2026-07-20T00:00:00+00:00",
    )

    assert command[0] == "python"
    assert command[1].endswith("run_benchmark.py")
    assert command.count("--dataset") == 1
    assert command.count("--run-name") == 1
    assert command[-2:] == ["--item-limit", "2"]
    assert "--experiment-time" in command


@pytest.mark.parametrize(
    "argument",
    ["--dataset", "--run-name=value", "--token-threshold", "--item-limit=2"],
)
def test_validate_runner_args_rejects_comparison_variables(argument):
    with pytest.raises(ValueError):
        validate_runner_args([argument])


def test_paired_outcomes_requires_identical_item_ids():
    with pytest.raises(ValueError, match="dataset item IDs do not match"):
        paired_outcomes(
            {
                "A": {"one": True, "two": True, "missing": False},
                "B": {"one": True, "two": False},
                "C": {"one": False, "two": False},
            }
        )


def test_paired_outcomes_builds_matrix_for_identical_item_ids():
    result = paired_outcomes(
        {
            "A": {"one": True, "two": True},
            "B": {"one": True, "two": False},
            "C": {"one": False, "two": False},
        }
    )

    assert result["paired_item_count"] == 2
    assert result["outcome_matrix"] == {"PPF": 1, "PFF": 1}


def test_fetch_run_results_waits_for_complete_dataset_run(monkeypatch):
    incomplete_run = SimpleNamespace(dataset_run_items=[
        SimpleNamespace(dataset_item_id="one", trace_id="trace-one"),
    ])
    complete_run = SimpleNamespace(dataset_run_items=[
        SimpleNamespace(dataset_item_id="one", trace_id="trace-one"),
        SimpleNamespace(dataset_item_id="two", trace_id="trace-two"),
    ])
    langfuse = Mock()
    langfuse.get_dataset_run.side_effect = [incomplete_run, complete_run]
    langfuse.get_trace.side_effect = [
        SimpleNamespace(scores=[SimpleNamespace(name="exact_match", value=1.0)]),
        SimpleNamespace(scores=[SimpleNamespace(name="exact_match", value=0.0)]),
    ]
    monkeypatch.setattr("sdk.benchmark.generic.run_context_manager_comparison.time.sleep", lambda _: None)

    result = fetch_run_results(
        langfuse,
        "dataset",
        "run",
        "exact_match",
        expected_item_ids=["one", "two"],
        attempts=2,
    )

    assert result == {"one": True, "two": False}
    assert langfuse.get_dataset_run.call_count == 2


def test_fetch_complete_dataset_run_reports_persistent_missing_items(monkeypatch):
    incomplete_run = SimpleNamespace(dataset_run_items=[
        SimpleNamespace(dataset_item_id="one"),
    ])
    langfuse = Mock()
    langfuse.get_dataset_run.return_value = incomplete_run
    monkeypatch.setattr("sdk.benchmark.generic.run_context_manager_comparison.time.sleep", lambda _: None)

    with pytest.raises(TimeoutError, match='"missing": \["two"\]'):
        fetch_complete_dataset_run(
            langfuse,
            "dataset",
            "run",
            expected_item_ids=["one", "two"],
            attempts=2,
        )


def test_build_run_name_is_paired_and_explicit():
    group = comparison_groups(1_000_000, 10_000)[2]

    assert (
        build_run_name("gaia-cm", "formal", 3, group)
        == "gaia-cm-formal-r03-c-managed-compression"
    )


def test_provider_cache_aggregate_uses_only_explicit_provider_metrics():
    result = aggregate_provider_cache({
        "one": {
            "status": "available",
            "available_calls": 2,
            "hit_calls": 1,
            "provider_cached_tokens": 40,
            "provider_input_tokens": 100,
        },
        "two": {
            "status": "unsupported",
            "available_calls": 0,
            "hit_calls": 0,
            "provider_cached_tokens": 0,
            "provider_input_tokens": 0,
        },
    })

    assert result["status"] == "available"
    assert result["provider_prefix_hit_rate"] == 0.5
    assert result["provider_cached_input_ratio"] == 0.4


def test_provider_cache_aggregate_preserves_unavailable_status():
    result = aggregate_provider_cache({
        "one": {"status": "unsupported"},
        "two": {"status": "unavailable"},
    })

    assert result["status"] == "unavailable"
    assert result["provider_prefix_hit_rate"] is None
    assert result["provider_cached_input_ratio"] is None


def test_summary_cache_aggregate_remains_separate():
    result = aggregate_summary_cache({
        "one": {
            "summary_cache_hits": 2,
            "summary_cache_types": ["previous_summary"],
        },
        "two": {
            "summary_cache_hits": 1,
            "summary_cache_types": ["current_summary", "previous_summary"],
        },
    })

    assert result == {
        "summary_cache_hits": 3,
        "summary_cache_types": ["current_summary", "previous_summary"],
    }
