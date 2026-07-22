#!/usr/bin/env python3
"""
Unified benchmark runner for Nexent Agent evaluation.

This script replaces run_experiment.py and re_evaluate.py, providing a single
entry point for running benchmark experiments with Nexent Agents.

It can load agent configuration from a YAML file (exported by export_agent_config.py)
and allows CLI arguments to override YAML values.

Usage:
    # Run new experiment with agent config
    python run_benchmark.py \\
        --agent-config configs/agent_7.yaml \\
        --dataset gsm8k-n10 \\
        --evaluators numeric_answer \\
        --run-name gsm8k-with-math-assistant
    
    # Override YAML config with CLI args
    python run_benchmark.py \\
        --agent-config configs/agent_7.yaml \\
        --dataset gsm8k-n10 \\
        --max-steps 20 \\
        --temperature 0.2 \\
        --evaluators numeric_answer em f1
    
    # Rescore existing traces (no LLM calls)
    python run_benchmark.py \\
        --rescore \\
        --dataset gsm8k-n10 \\
        --existing-run gsm8k-deepseek-v4-flash-n10 \\
        --evaluators em f1 exact_match
    
    # Upload dataset and run experiment
    python run_benchmark.py \\
        --agent-config configs/agent_7.yaml \\
        --dataset my-benchmark \\
        --upload data/test.jsonl \\
        --evaluators numeric_answer

YAML config file structure (see export_agent_config.py):
    agent_info:
      display_name: 数学解答助手
      ...
    agent_config:
      max_steps: 15
      enable_context_manager: true
      ...
    prompts:
      duty_prompt: |
        你是一个专业的数学解题助手...
      constraint_prompt: ""
      few_shots_prompt: ""
    tools: [...]
    sub_agents: [...]
    skills: [...]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Add current directory (generic/) and parent (sdk/benchmark/) to path for imports
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load environment variables
load_dotenv()
load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")


def load_agent_config(config_path: str) -> dict:
    """Load agent configuration from YAML file."""
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def upload_jsonl(dataset_name: str, jsonl_path: str,
                 input_key: str = "question", output_key: str = "answer") -> int:
    """Upload a JSONL file as a Langfuse dataset."""
    from langfuse import Langfuse
    lf = Langfuse()
    
    try:
        lf.create_dataset(name=dataset_name)
        print(f"  Created dataset '{dataset_name}'")
    except Exception:
        print(f"  Dataset '{dataset_name}' already exists")
    
    count = 0
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                print(f"  WARNING: skipping line {line_num} (invalid JSON)")
                continue
            
            inp = {input_key: obj.get(input_key, "")}
            for k, v in obj.items():
                if k not in (input_key, output_key):
                    inp[k] = v
            
            exp_out = {output_key: obj.get(output_key, "")} if output_key in obj else None
            
            lf.create_dataset_item(
                dataset_name=dataset_name,
                input=inp,
                expected_output=exp_out,
            )
            count += 1
    
    lf.flush()
    print(f"  Uploaded {count} items from {jsonl_path}")
    return count


def run_experiment(dataset_name: str, task_fn, evaluator_fns: list,
                   run_name: str, max_concurrency: int = 1,
                   manifest_context: dict | None = None,
                   item_limit: int | None = None):
    """Run experiment using Langfuse v2 SDK: trace → score → link pattern."""
    from langfuse import Langfuse
    lf = Langfuse()
    
    dataset = lf.get_dataset(dataset_name)
    items = dataset.items[:item_limit] if item_limit is not None else dataset.items
    n = len(items)
    print(f"  {n} items loaded")
    
    if n == 0:
        print("ERROR: Dataset is empty.")
        return
    
    print(f"\n{'='*60}")
    print(f"Running experiment: {run_name}")
    print(f"  Dataset:      {dataset_name} ({n} items)")
    print(f"  Model:        {os.environ.get('LLM_MODEL_NAME', 'unknown')}")
    print(f"{'='*60}\n")
    
    total_scores = {}
    passed = 0
    failed = 0
    agg_compression_calls = 0
    agg_compression_input_tokens = 0
    agg_summary_cache_hits = 0
    agg_provider_cache_available_calls = 0
    agg_provider_cache_hit_calls = 0
    agg_provider_cached_tokens = 0
    agg_provider_input_tokens = 0
    manifest = None
    manifest_path = None
    dataset_item_ids = [str(item.id) for item in items]
    dataset_version = str(getattr(dataset, "version", "") or "") or None

    if manifest_context is not None:
        from experiment_manifest import manifest_path

        artifact_path = manifest_path(
            Path(__file__).parent / "artifacts" / "manifests",
            run_name,
        )
        if artifact_path.exists():
            raise FileExistsError(
                f"Run '{run_name}' already has a manifest: {artifact_path}"
            )

    for i, item in enumerate(items):
        q_preview = str(item.input)[:60] if item.input else ""
        print(f"[{i+1}/{n}] {q_preview}...", end=" ", flush=True)
        
        trace = lf.trace(
            name=f"benchmark-{dataset_name}",
            input=item.input,
            metadata={"run_name": run_name, "item_index": i},
        )
        
        try:
            output = task_fn(item=item)
        except Exception as e:
            print(f"ERROR: {e}")
            output = {"final_answer": "", "errors": [str(e)]}

        if manifest is None and manifest_context is not None:
            from experiment_manifest import build_manifest, write_manifest_exclusive

            agent_config = output.get("agent_config", {})
            manifest = build_manifest(
                dataset_name=dataset_name,
                dataset_version=dataset_version,
                dataset_item_ids=dataset_item_ids,
                run_name=run_name,
                system_prompt=output.get("system_prompt", ""),
                model_config=output.get("model_config", {}),
                agent_config=agent_config,
                **manifest_context,
            )
            manifest_path = write_manifest_exclusive(
                manifest,
                Path(__file__).parent / "artifacts" / "manifests",
            )
            print(f"\n  Resolved manifest: {manifest_path}")
        
        trace.update(
            output=output,
            metadata={
                "run_name": run_name,
                "item_index": i,
                "system_prompt": output.get("system_prompt", ""),
                "model_config": output.get("model_config", {}),
                "agent_config": output.get("agent_config", {}),
                "compression": output.get("compression", {}),
                "provider_cache": output.get("provider_cache", {}),
                "manifest_hash": manifest.get("manifest_hash") if manifest else None,
                "manifest_path": str(manifest_path) if manifest_path else None,
            },
        )
        
        steps = output.get("steps", [])
        for step in steps:
            step_num = step.get("step_number", "?")
            if step_num == "final_answer":
                trace.span(
                    name="final_answer",
                    input={"query": step.get("query", "")},
                    output={"answer": step.get("main_output", "")},
                    metadata={"token_usage": step.get("token_usage")},
                )
            else:
                trace.span(
                    name=f"step_{step_num}",
                    input={
                        "query": step.get("query", ""),
                        "thinking": step.get("thinking", ""),
                        "deep_thinking": step.get("deep_thinking", ""),
                    },
                    output={
                        "main_output": step.get("main_output", ""),
                        "code": step.get("code", ""),
                        "tool_call": step.get("tool_call", ""),
                        "observation": step.get("observation", ""),
                    },
                    metadata={
                        "token_usage": step.get("token_usage"),
                        "compression": step.get("compression"),
                        "provider_cache": step.get("provider_cache"),
                    },
                )
        
        item_scores = {}
        for eval_fn in evaluator_fns:
            try:
                result = eval_fn(
                    input=item.input,
                    output=output,
                    expected_output=item.expected_output,
                    metadata=item.metadata,
                )
                if isinstance(result, dict):
                    name = result.get("name", "unknown")
                    value = result.get("value", 0.0)
                elif isinstance(result, list):
                    for r in result:
                        n_ = r.get("name", "unknown")
                        v_ = r.get("value", 0.0)
                        trace.score(name=n_, value=v_)
                        item_scores[n_] = v_
                    continue
                else:
                    name, value = "unknown", 0.0
                
                trace.score(name=name, value=value)
                item_scores[name] = value
                
                if name not in total_scores:
                    total_scores[name] = []
                total_scores[name].append(value)
                
            except Exception as e:
                print(f"EVAL_ERROR: {e}")

        compression = output.get("compression", {})
        agg_compression_calls += compression.get("calls", 0)
        agg_compression_input_tokens += compression.get("input_tokens", 0)
        agg_summary_cache_hits += compression.get("summary_cache_hits", 0)
        if (
            compression.get("calls", 0) > 0
            or compression.get("summary_cache_hits", 0) > 0
        ):
            trace.score(name="compression_calls", value=compression["calls"])
            trace.score(name="compression_input_tokens", value=compression.get("input_tokens", 0))
            trace.score(name="compression_output_tokens", value=compression.get("output_tokens", 0))
            trace.score(name="summary_cache_hits", value=compression.get("summary_cache_hits", 0))
            total_uncompressed = compression.get("total_uncompressed_est_tokens", 0)
            total_input = output.get("total_input_tokens", 0)
            if total_uncompressed > 0:
                trace.score(
                    name="compression_token_reduction_pct",
                    value=round((1 - total_input / total_uncompressed) * 100, 1),
                )

        provider_cache = output.get("provider_cache", {})
        if provider_cache.get("status") == "available":
            available_calls = provider_cache.get("available_calls", 0) or 0
            hit_calls = provider_cache.get("hit_calls", 0) or 0
            cached_tokens = provider_cache.get("provider_cached_tokens", 0) or 0
            provider_input_tokens = provider_cache.get("provider_input_tokens", 0) or 0
            agg_provider_cache_available_calls += available_calls
            agg_provider_cache_hit_calls += hit_calls
            agg_provider_cached_tokens += cached_tokens
            agg_provider_input_tokens += provider_input_tokens
            trace.score(name="provider_cache_hit_calls", value=hit_calls)
            trace.score(name="provider_cached_tokens", value=cached_tokens)
            trace.score(
                name="provider_cached_input_ratio",
                value=provider_cache.get("provider_cached_input_ratio", 0.0) or 0.0,
            )

        primary_score = next(iter(item_scores.values()), 0.0)
        if primary_score >= 1.0:
            passed += 1
        else:
            failed += 1
        
        score_str = ", ".join(f"{k}={v:.2f}" for k, v in item_scores.items())
        print(f"✓ {score_str}")
        
        item.link(trace, run_name)
    
    lf.flush()
    
    print(f"\n{'='*60}")
    print(f"Experiment complete: {run_name}")
    print(f"  Total:  {n}")
    print(f"  Passed: {passed}")
    print(f"  Failed: {failed}")
    for metric, values in total_scores.items():
        avg = sum(values) / len(values) if values else 0
        print(f"  Avg {metric}: {avg:.4f}")
    if agg_compression_calls > 0:
        print(f"  Compression:")
        print(f"    Total calls:        {agg_compression_calls}")
        print(f"    Total input tokens: {agg_compression_input_tokens}")
        print(f"    Summary cache hits: {agg_summary_cache_hits}")
    if agg_provider_cache_available_calls:
        print("  Provider prefix cache:")
        print(
            "    Call hit rate:       "
            f"{agg_provider_cache_hit_calls / agg_provider_cache_available_calls:.2%}"
        )
        print(f"    Cached tokens:      {agg_provider_cached_tokens}")
        print(
            "    Cached input ratio:  "
            f"{agg_provider_cached_tokens / agg_provider_input_tokens:.2%}"
            if agg_provider_input_tokens
            else "    Cached input ratio:  unavailable"
        )
    else:
        print("  Provider prefix cache: unsupported or metrics unavailable")
    print(f"\nView in Langfuse: {os.environ.get('LANGFUSE_HOST', '')}/dataset/{dataset.id}")
    print(f"{'='*60}")


def rescore_experiment(dataset_name: str, existing_run: str, evaluator_fns: list,
                       new_run_name: str):
    """Re-evaluate existing traces with new evaluators (no LLM calls)."""
    from langfuse import Langfuse
    lf = Langfuse()
    
    dataset = lf.get_dataset(dataset_name)
    items = dataset.items
    
    existing = lf.get_dataset_run(dataset_name, existing_run)
    run_items = existing.dataset_run_items
    print(f"Rescore '{new_run_name}': {len(run_items)} traces from '{existing_run}'")
    
    output_by_item_id = {}
    for ri in run_items:
        trace = lf.get_trace(ri.trace_id)
        output_by_item_id[ri.dataset_item_id] = trace.output
    
    total_scores = {}
    passed = 0
    
    for i, item in enumerate(items):
        output = output_by_item_id.get(item.id)
        if output is None:
            print(f"  [{i+1}] SKIP (no trace)")
            continue
        
        trace = lf.trace(
            name=f"re-eval-{dataset_name}",
            input=item.input, output=output,
            metadata={"re_eval_of": existing_run, "evaluators": [f.__name__ for f in evaluator_fns]},
        )
        
        item_scores = {}
        for eval_fn in evaluator_fns:
            try:
                result = eval_fn(
                    input=item.input, output=output,
                    expected_output=item.expected_output, metadata=item.metadata,
                )
                if isinstance(result, dict):
                    name = result.get("name", "unknown")
                    value = result.get("value", 0.0)
                    trace.score(name=name, value=value)
                    item_scores[name] = value
                    total_scores.setdefault(name, []).append(value)
                elif isinstance(result, list):
                    for r in result:
                        trace.score(name=r.get("name"), value=r.get("value"))
                        item_scores[r.get("name")] = r.get("value")
                        total_scores.setdefault(r.get("name"), []).append(r.get("value"))
            except Exception as e:
                print(f"  [{i+1}] EVAL_ERROR: {e}")
        
        primary = next(iter(item_scores.values()), 0.0)
        if primary >= 1.0:
            passed += 1
        
        item.link(trace, new_run_name)
        print(f"  [{i+1}] scores={item_scores}")
    
    lf.flush()
    
    avg_str = ", ".join(
        f"avg_{k}={sum(v)/len(v):.4f}" for k, v in total_scores.items()
    )
    print(f"Rescore '{new_run_name}' DONE: {passed}/{len(items)} passed, {avg_str}")


def main():
    parser = argparse.ArgumentParser(
        description="Unified benchmark runner for Nexent Agent evaluation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    # Agent config
    parser.add_argument("--agent-config", type=str, 
                        help="Path to agent YAML config file (from export_agent_config.py)")
    
    # Dataset
    parser.add_argument("--dataset", type=str, required=True,
                        help="Langfuse dataset name")
    parser.add_argument("--upload", type=str,
                        help="Path to JSONL file to upload as dataset")
    parser.add_argument("--input-key", type=str, default="question",
                        help="Key in JSONL for question (default: question)")
    parser.add_argument("--output-key", type=str, default="answer",
                        help="Key in JSONL for answer (default: answer)")
    
    # Evaluators
    parser.add_argument("--evaluators", nargs="+", default=["exact_match"],
                        help="Evaluator names (default: exact_match)")
    
    # Agent execution params (override YAML)
    parser.add_argument("--max-steps", type=int,
                        help="Max agent steps (overrides YAML)")
    parser.add_argument("--temperature", type=float,
                        help="LLM temperature (default: 0.1; exported YAML does not include it)")
    parser.add_argument(
        "--model-factory",
        type=str,
        help=(
            "Explicit provider capability identifier used for cache metrics "
            "(for example: openai); unknown providers remain unsupported"
        ),
    )
    parser.add_argument("--language", type=str, choices=["en", "zh"],
                        help="Prompt language (overrides YAML)")
    parser.add_argument("--duty-prompt", type=str,
                        help="Custom duty prompt (overrides YAML)")
    parser.add_argument("--constraint-prompt", type=str,
                        help="Custom constraint prompt (overrides YAML)")
    parser.add_argument("--few-shots-prompt", type=str,
                        help="Custom few shots prompt (overrides YAML)")
    parser.add_argument("--system-prompt-file", type=str,
                        help="Path to custom system prompt file (bypasses template)")
    parser.add_argument("--experiment-time", type=str,
                        help=argparse.SUPPRESS)
    
    # Context manager
    context_group = parser.add_mutually_exclusive_group()
    context_group.add_argument("--enable-context-manager", action="store_true",
                               help="Enable context manager (overrides YAML)")
    context_group.add_argument("--disable-context-manager", action="store_true",
                               help="Disable context manager (overrides YAML)")
    parser.add_argument("--token-threshold", type=positive_int,
                        help="Context manager token threshold (SDK default: 10000)")
    parser.add_argument("--keep-recent-steps", type=non_negative_int,
                        help="Keep N recent action steps from compression (SDK default: 4)")
    parser.add_argument("--keep-recent-pairs", type=non_negative_int,
                        help="Keep N recent conversation pairs from compression (SDK default: 2)")
    parser.add_argument("--max-observation-length", type=non_negative_int,
                        help="Truncate observations longer than N chars; 0=disabled (SDK default: 0)")
    
    # Execution
    parser.add_argument("--max-concurrency", type=positive_int, default=1,
                        help="Max parallel agent runs (default: 1)")
    parser.add_argument("--item-limit", type=positive_int,
                        help="Run only the first N dataset items (for deterministic smoke tests)")
    parser.add_argument("--run-name", type=str,
                        help="Custom run name (default: auto-generated)")
    
    # Rescore mode
    parser.add_argument("--rescore", action="store_true",
                        help="Rescore existing traces (no LLM calls)")
    parser.add_argument("--existing-run", type=str,
                        help="Existing run name to rescore (required with --rescore)")
    
    # Utilities
    parser.add_argument("--dry-run", action="store_true",
                        help="Upload dataset but don't run experiment")
    parser.add_argument("--list-evaluators", action="store_true",
                        help="List available evaluators and exit")
    
    args = parser.parse_args()
    
    # List evaluators
    if args.list_evaluators:
        from evaluators import list_evaluators
        print("Available evaluators:")
        for name in list_evaluators():
            print(f"  - {name}")
        return
    
    # Load agent config if provided
    agent_config = {}
    if args.agent_config:
        print(f"Loading agent config from: {args.agent_config}")
        agent_config = load_agent_config(args.agent_config)
        
        agent_info = agent_config.get("agent_info", {})
        print(f"  Agent: {agent_info.get('display_name', 'unknown')}")
        print(f"  Description: {agent_info.get('description', '')[:80]}...")
    
    # Merge config: CLI args override YAML
    prompts = agent_config.get("prompts", {})
    agent_cfg = agent_config.get("agent_config", {})
    
    duty_prompt = args.duty_prompt or prompts.get("duty_prompt", "")
    constraint_prompt = args.constraint_prompt or prompts.get("constraint_prompt", "")
    few_shots_prompt = args.few_shots_prompt or prompts.get("few_shots_prompt", "")
    max_steps = args.max_steps or agent_cfg.get("max_steps", 10)
    temperature = args.temperature if args.temperature is not None else agent_cfg.get("temperature", 0.1)
    language = args.language or "en"
    model_factory = args.model_factory or agent_cfg.get("model_factory")
    
    # Context manager
    enable_cm = agent_cfg.get("enable_context_manager", False)
    if args.enable_context_manager:
        enable_cm = True
    elif args.disable_context_manager:
        enable_cm = False

    if not args.rescore:
        cm_only_args = {
            "--token-threshold": args.token_threshold,
            "--keep-recent-steps": args.keep_recent_steps,
            "--keep-recent-pairs": args.keep_recent_pairs,
            "--max-observation-length": args.max_observation_length,
        }
        ignored_cm_args = [name for name, value in cm_only_args.items() if value is not None]
        if not enable_cm and ignored_cm_args:
            parser.error(
                f"{', '.join(ignored_cm_args)} require ContextManager to be enabled"
            )
    
    # Load custom system prompt if provided
    system_prompt = ""
    if args.system_prompt_file:
        with open(args.system_prompt_file, "r", encoding="utf-8") as f:
            system_prompt = f.read().strip()
        print(f"Loaded custom system prompt from: {args.system_prompt_file}")
    
    # Resolve evaluators
    from evaluators import resolve_evaluators
    evaluator_fns = resolve_evaluators(args.evaluators)
    print(f"Evaluators: {args.evaluators}")
    
    # Init Langfuse
    from langfuse import Langfuse
    lf = Langfuse()
    try:
        lf.auth_check()
        print(f"Langfuse connected: {os.environ.get('LANGFUSE_HOST')}")
    except Exception as e:
        print(f"ERROR: Langfuse connection failed: {e}")
        sys.exit(1)
    
    # Upload dataset if requested
    if args.upload:
        if not os.path.exists(args.upload):
            print(f"ERROR: File not found: {args.upload}")
            sys.exit(1)
        upload_jsonl(
            dataset_name=args.dataset,
            jsonl_path=args.upload,
            input_key=args.input_key,
            output_key=args.output_key,
        )
    
    if args.dry_run:
        print("\nDry run complete.")
        return
    
    # Rescore mode
    if args.rescore:
        if not args.existing_run:
            parser.error("--existing-run is required with --rescore")
        
        new_run_name = args.run_name or f"{args.existing_run}-rescore-{'-'.join(args.evaluators)}"
        rescore_experiment(
            dataset_name=args.dataset,
            existing_run=args.existing_run,
            evaluator_fns=evaluator_fns,
            new_run_name=new_run_name
        )
        return

    from agent_runner import build_tools_from_yaml
    tools_yaml = agent_config.get("tools", [])
    tools = build_tools_from_yaml(tools_yaml) if tools_yaml else []

    # Run new experiment
    from task_adapter import make_nexent_task

    from nexent.core.agents.agent_context import ContextManagerConfig
    cm_kwargs = {"enabled": enable_cm}
    if args.token_threshold is not None:
        cm_kwargs["token_threshold"] = args.token_threshold
    if args.keep_recent_steps is not None:
        cm_kwargs["keep_recent_steps"] = args.keep_recent_steps
    if args.keep_recent_pairs is not None:
        cm_kwargs["keep_recent_pairs"] = args.keep_recent_pairs
    if args.max_observation_length is not None:
        cm_kwargs["max_observation_length"] = args.max_observation_length
    cm_config = ContextManagerConfig(**cm_kwargs)

    task_fn = make_nexent_task(
        system_prompt=system_prompt,
        duty_prompt=duty_prompt,
        constraint_prompt=constraint_prompt,
        few_shots_prompt=few_shots_prompt,
        max_steps=max_steps,
        temperature=temperature,
        language=language,
        input_key=args.input_key,
        context_manager_config=cm_config,
        experiment_time=args.experiment_time,
        tools=tools,
        model_factory=model_factory,
        user_id="user_id",
    )

    run_name = args.run_name or f"{args.dataset}-{int(time.time())}"

    print(f"\nConfiguration:")
    print(f"  Max steps:    {max_steps}")
    print(f"  Temperature:  {temperature}")
    print(f"  Language:     {language}")
    print(f"  Model factory:{model_factory or 'unknown'}")
    print(f"  Context mgr:  {enable_cm}")
    if enable_cm:
        print(f"  CM config:    threshold={cm_config.token_threshold}, "
              f"keep_steps={cm_config.keep_recent_steps}, "
              f"keep_pairs={cm_config.keep_recent_pairs}, "
              f"max_obs_len={cm_config.max_observation_length}")
    print(f"  Tools:        {len(tools)} ({', '.join(t.name for t in tools) if tools else 'none'})")
    if duty_prompt:
        print(f"  Duty prompt:  {duty_prompt[:60]}...")
    
    run_experiment(
        dataset_name=args.dataset,
        task_fn=task_fn,
        evaluator_fns=evaluator_fns,
        run_name=run_name,
        max_concurrency=args.max_concurrency,
        item_limit=args.item_limit,
        manifest_context={
            "repo_root": Path(__file__).resolve().parents[3],
            "lifecycle_mode": "isolated-item",
            "context_manager_config": cm_config,
            "max_steps": max_steps,
            "temperature": temperature,
            "language": language,
            "max_concurrency": args.max_concurrency,
            "tools": tools,
            "evaluator_names": args.evaluators,
            "observation_policy": {
                "owner": "managed" if enable_cm else "legacy",
                "algorithm": "head_tail",
                "effective_limit_chars": (
                    cm_config.max_observation_length if enable_cm else 100000
                ),
            },
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


if __name__ == "__main__":
    main()
