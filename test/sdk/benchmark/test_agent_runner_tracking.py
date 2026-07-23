import json
from types import SimpleNamespace

import pytest

from sdk.benchmark import agent_runner


@pytest.mark.asyncio
async def test_run_agent_with_tracking_builds_model_step_and_metrics(monkeypatch):
    async def fake_agent_run(_):
        messages = [
            ("step_count", "1"),
            ("model_output_thinking", "reason"),
            ("model_output", "answer draft"),
            ("model_output_code", "print('x')"),
            ("parse", "python_interpreter"),
            ("execution_logs", "x"),
            (
                "token_count",
                json.dumps({
                    "estimated_context_tokens": 120,
                    "step_input_tokens": 100,
                    "step_output_tokens": 20,
                    "compression_calls": 1,
                    "compression_input_tokens": 40,
                    "compression_output_tokens": 10,
                    "compression_cache_types": ["current_cache_hit"],
                    "provider_cache_status": "available",
                    "provider_cache_metrics_source": "usage",
                    "provider_cache_hit": True,
                    "provider_cached_input_tokens": 80,
                    "provider_uncached_input_tokens": 20,
                }),
            ),
            ("final_answer", "final"),
        ]
        for message_type, content in messages:
            yield json.dumps({"type": message_type, "content": content})

    monkeypatch.setattr(agent_runner, "agent_run", fake_agent_run)
    result = await agent_runner.run_agent_with_tracking(
        SimpleNamespace(query="question")
    )

    assert result.final_answer == "final"
    assert result.step_count == 1
    assert result.total_input_tokens == 120
    assert result.total_api_input_tokens == 100
    assert result.provider_cache_hit_calls == 1
    assert result.steps[0]["thinking"] == "reason"
    assert result.steps[0]["main_output"] == "answer draft"
    assert result.steps[0]["code"] == "print('x')"
    assert result.steps[0]["tool_call"] == "python_interpreter"
    assert result.steps[0]["observation"] == "x"
    assert result.steps[0]["token_usage"]["output_tokens"] == 20
    assert result.steps[0]["compression"]["calls"] == 1
    assert result.steps[1]["step_number"] == "final_answer"
