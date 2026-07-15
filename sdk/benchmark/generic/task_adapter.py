# -*- coding: utf-8 -*-
"""Task adapter: bridges Langfuse DatasetItem to NexentAgent execution.

Provides a factory function that creates a Langfuse-compatible task function
bound to a specific agent configuration. The task function:
1. Extracts the question/query from the DatasetItem input
2. Builds an AgentRunInfo via agent_runner.py
3. Runs the agent and returns structured results
"""
import asyncio
import os
import sys

# Path setup — must happen before agent_runner import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: F401

from agent_runner import (
    build_agent_run_info,
    build_agent_run_info_with_custom_prompt,
    run_agent_with_tracking,
    AgentRunResult,
)


def make_nexent_task(
    system_prompt: str = "",
    duty_prompt: str = "",
    constraint_prompt: str = "",
    few_shots_prompt: str = "",
    max_steps: int = 10,
    temperature: float = 0.1,
    language: str = "en",
    tools: list = None,
    managed_agents: list = None,
    input_key: str = "question",
    max_tokens: int = None,
    context_manager_config = None,
):
    """Factory: create a Langfuse task function bound to agent config.

    Args:
        system_prompt: Custom system prompt (bypasses template engine if set).
        duty_prompt: Duty prompt for template-based system prompt.
        constraint_prompt: Constraint prompt for template-based system prompt.
        few_shots_prompt: Few-shot examples prompt.
        max_steps: Max agent execution steps.
        temperature: LLM temperature.
        language: Language for prompts (en/zh).
        tools: Tool list (ToolConfig objects).
        managed_agents: Managed sub-agents.
        input_key: Key in DatasetItem.input that contains the question.
        max_tokens: Per-call output token cap.

    Returns:
        A function compatible with Langfuse's TaskFunction protocol.
    """
    tools = tools or []
    managed_agents = managed_agents or []
    minio_bucket = os.getenv("MINIO_DEFAULT_BUCKET", "nexent")
    file_prefix = os.getenv("GAIA_FILE_PREFIX", "gaia")

    def task(*, item, **kwargs):
        """Execute NexentAgent on a single DatasetItem.

        Args:
            item: Langfuse DatasetItem (has .input, .expected_output, .metadata)
                  or a plain dict with "input" / "expected_output" keys.

        Returns:
            dict with final_answer, step_count, token counts, and errors.
        """
        # Extract input — support both DatasetItem and plain dict
        if isinstance(item, dict):
            inp = item.get("input", {})
        else:
            inp = item.input if hasattr(item, "input") else {}

        question = inp.get(input_key, "") if isinstance(inp, dict) else str(inp)

        # Inject file attachment S3 URL when DatasetItem has file_name,
        # matching production behavior in create_agent_info.py
        file_name = inp.get("file_name") if isinstance(inp, dict) else None
        if file_name and question:
            s3_url = f"s3:/{minio_bucket}/{file_prefix}/{file_name}"
            question = (
                f"User uploaded files. The file information is as follows:\n\n"
                f"File name: {file_name}, S3 URL: {s3_url}  [permanent]\n\n"
                f"User wants to answer questions based on the information in the above files: {question}"
            )

        if not question:
            return {
                "final_answer": "",
                "step_count": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "errors": ["No question found in input"],
            }

        # Build agent run info
        if system_prompt:
            agent_run_info = build_agent_run_info_with_custom_prompt(
                query=question,
                system_prompt=system_prompt,
                history=[],
                tools=tools,
                managed_agents=managed_agents,
                max_steps=max_steps,
                temperature=temperature,
                language=language,
                context_manager_config=context_manager_config,
            )
        else:
            agent_run_info = build_agent_run_info(
                query=question,
                history=[],
                duty_prompt=duty_prompt,
                constraint_prompt=constraint_prompt,
                few_shots_prompt=few_shots_prompt,
                tools=tools,
                managed_agents=managed_agents,
                max_steps=max_steps,
                temperature=temperature,
                language=language,
                max_tokens=max_tokens,
                context_manager_config=context_manager_config,
            )

        # Run agent (sync wrapper for Langfuse's sync task protocol)
        loop = asyncio.new_event_loop()
        try:
            result: AgentRunResult = loop.run_until_complete(
                run_agent_with_tracking(agent_run_info)
            )
        finally:
            loop.close()

        system_prompt_text = agent_run_info.agent_config.prompt_templates.get("system_prompt", "") if agent_run_info.agent_config.prompt_templates else ""
        model_config = agent_run_info.model_config_list[0] if agent_run_info.model_config_list else None

        return {
            "final_answer": result.final_answer,
            "step_count": result.step_count,
            "total_input_tokens": result.total_input_tokens,
            "total_api_input_tokens": result.total_api_input_tokens,
            "total_output_tokens": result.total_output_tokens,
            "errors": result.errors,
            "message_type_count": result.message_type_count,
            "steps": result.steps,
            "system_prompt": system_prompt_text,
            "model_config": {
                "model_name": model_config.model_name if model_config else "",
                "temperature": model_config.temperature if model_config else None,
                "max_tokens": model_config.max_tokens if model_config else None,
            } if model_config else {},
            "agent_config": {
                "name": agent_run_info.agent_config.name,
                "max_steps": agent_run_info.agent_config.max_steps,
                "tools": [t.class_name if hasattr(t, "class_name") else str(t) for t in (agent_run_info.agent_config.tools or [])],
                "managed_agents": [a.name for a in (agent_run_info.agent_config.managed_agents or [])],
                "context_manager_enabled": context_manager_config is not None and getattr(context_manager_config, "enabled", False),
            },
            "compression": {
                "calls": result.compression_calls,
                "input_tokens": result.compression_input_tokens,
                "output_tokens": result.compression_output_tokens,
                "cache_hits": result.compression_cache_hits,
                "cache_types": result.compression_cache_types,
                "total_uncompressed_est_tokens": result.total_uncompressed_est_tokens,
            },
        }

    return task


def make_run_evaluators(metric_names: list[str] = None):
    """Factory: create run-level aggregate evaluators.

    Args:
        metric_names: List of per-item metric names to aggregate.
                      Defaults to common metrics.

    Returns:
        A list of run evaluator functions.
    """
    if metric_names is None:
        metric_names = ["exact_match", "em", "f1", "numeric_answer", "keyword_match"]

    def aggregate(*, item_results, **kwargs):
        """Compute aggregate metrics across all items."""
        from langfuse.experiment import Evaluation

        if not item_results:
            return []

        n = len(item_results)
        results = []

        # Aggregate each known metric
        for metric in metric_names:
            values = []
            for r in item_results:
                for ev in r.evaluations:
                    if ev.name == metric and isinstance(ev.value, (int, float)):
                        values.append(ev.value)
            if values:
                avg = sum(values) / len(values)
                results.append(Evaluation(
                    name=f"avg_{metric}",
                    value=round(avg, 4),
                    comment=f"over {len(values)} items",
                ))

        # Always aggregate token stats from output dict
        input_tokens = [
            r.output.get("total_input_tokens", 0)
            for r in item_results
            if isinstance(r.output, dict)
        ]
        output_tokens = [
            r.output.get("total_output_tokens", 0)
            for r in item_results
            if isinstance(r.output, dict)
        ]
        steps = [
            r.output.get("step_count", 0)
            for r in item_results
            if isinstance(r.output, dict)
        ]

        if input_tokens:
            results.append(Evaluation(
                name="avg_input_tokens",
                value=round(sum(input_tokens) / len(input_tokens), 0),
            ))
        if output_tokens:
            results.append(Evaluation(
                name="avg_output_tokens",
                value=round(sum(output_tokens) / len(output_tokens), 0),
            ))
        if steps:
            results.append(Evaluation(
                name="avg_steps",
                value=round(sum(steps) / len(steps), 1),
            ))

        return results

    return [aggregate]
