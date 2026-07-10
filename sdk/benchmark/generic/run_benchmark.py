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
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

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
                   run_name: str, max_concurrency: int = 1):
    """Run experiment using Langfuse v2 SDK: trace → score → link pattern."""
    from langfuse import Langfuse
    lf = Langfuse()
    
    dataset = lf.get_dataset(dataset_name)
    items = dataset.items
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
    agg_compression_cache_hits = 0

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
        
        trace.update(
            output=output,
            metadata={
                "run_name": run_name,
                "item_index": i,
                "system_prompt": output.get("system_prompt", ""),
                "model_config": output.get("model_config", {}),
                "agent_config": output.get("agent_config", {}),
                "compression": output.get("compression", {}),
            },
        )
        
        steps = output.get("steps", [])
        for step in steps:
            step_num = step.get("step_number", "?")
            if step_num == "final_answer":
                trace.span(
                    name="final_answer",
                    output={"answer": step.get("main_output", "")},
                    metadata={"token_usage": step.get("token_usage")},
                )
            else:
                trace.span(
                    name=f"step_{step_num}",
                    input={
                        "thinking": step.get("thinking", ""),
                        "deep_thinking": step.get("deep_thinking", ""),
                    },
                    output={
                        "main_output": step.get("main_output", ""),
                        "code": step.get("code", ""),
                        "observation": step.get("observation", ""),
                    },
                    metadata={
                        "token_usage": step.get("token_usage"),
                        "compression": step.get("compression"),
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
        agg_compression_cache_hits += compression.get("cache_hits", 0)
        if compression.get("calls", 0) > 0:
            trace.score(name="compression_calls", value=compression["calls"])
            trace.score(name="compression_input_tokens", value=compression.get("input_tokens", 0))
            trace.score(name="compression_output_tokens", value=compression.get("output_tokens", 0))
            trace.score(name="compression_cache_hits", value=compression.get("cache_hits", 0))
            total_uncompressed = compression.get("total_uncompressed_est_tokens", 0)
            total_input = output.get("total_input_tokens", 0)
            if total_uncompressed > 0:
                trace.score(
                    name="compression_token_reduction_pct",
                    value=round((1 - total_input / total_uncompressed) * 100, 1),
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
        print(f"    Total cache hits:   {agg_compression_cache_hits}")
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
                        help="LLM temperature (overrides YAML)")
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
    
    # Context manager
    parser.add_argument("--enable-context-manager", action="store_true",
                        help="Enable context manager (overrides YAML)")
    parser.add_argument("--disable-context-manager", action="store_true",
                        help="Disable context manager (overrides YAML)")
    
    # Execution
    parser.add_argument("--max-concurrency", type=int, default=1,
                        help="Max parallel agent runs (default: 1)")
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
    
    # Context manager
    enable_cm = agent_cfg.get("enable_context_manager", False)
    if args.enable_context_manager:
        enable_cm = True
    elif args.disable_context_manager:
        enable_cm = False
    
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
    cm_config = ContextManagerConfig(enabled=enable_cm)

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
        tools=tools,
    )

    run_name = args.run_name or f"{args.dataset}-{int(time.time())}"

    print(f"\nConfiguration:")
    print(f"  Max steps:    {max_steps}")
    print(f"  Temperature:  {temperature}")
    print(f"  Language:     {language}")
    print(f"  Context mgr:  {enable_cm}")
    print(f"  Tools:        {len(tools)} ({', '.join(t.name for t in tools) if tools else 'none'})")
    if duty_prompt:
        print(f"  Duty prompt:  {duty_prompt[:60]}...")
    
    run_experiment(
        dataset_name=args.dataset,
        task_fn=task_fn,
        evaluator_fns=evaluator_fns,
        run_name=run_name,
        max_concurrency=args.max_concurrency,
    )


if __name__ == "__main__":
    main()
