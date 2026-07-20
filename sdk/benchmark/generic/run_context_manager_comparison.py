#!/usr/bin/env python3
"""Run paired Legacy / Managed-No-Compression / Managed-Compression benchmarks."""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


GENERIC_DIR = Path(__file__).resolve().parent
REPO_ROOT = GENERIC_DIR.parents[2]
RUNNER = GENERIC_DIR / "run_benchmark.py"
ARTIFACT_ROOT = GENERIC_DIR / "artifacts"
CONTROLLED_RUNNER_ARGS = {
    "--dataset",
    "--run-name",
    "--enable-context-manager",
    "--disable-context-manager",
    "--token-threshold",
    "--item-limit",
    "--experiment-time",
}


@dataclass(frozen=True)
class GroupSpec:
    key: str
    label: str
    runner_args: tuple[str, ...]


def comparison_groups(
    no_compression_threshold: int,
    compression_threshold: int,
) -> tuple[GroupSpec, ...]:
    """Return the three standard comparison groups."""
    return (
        GroupSpec("A", "legacy", ("--disable-context-manager",)),
        GroupSpec(
            "B",
            "managed-no-compression",
            (
                "--enable-context-manager",
                "--token-threshold",
                str(no_compression_threshold),
            ),
        ),
        GroupSpec(
            "C",
            "managed-compression",
            (
                "--enable-context-manager",
                "--token-threshold",
                str(compression_threshold),
            ),
        ),
    )


def build_run_name(prefix: str, phase: str, repeat_index: int, group: GroupSpec) -> str:
    """Build a paired and collision-resistant run name."""
    return f"{prefix}-{phase}-r{repeat_index:02d}-{group.key.lower()}-{group.label}"


def build_runner_command(
    *,
    python_executable: str,
    dataset: str,
    run_name: str,
    group: GroupSpec,
    runner_args: list[str],
    item_limit: int | None,
    experiment_time: str,
) -> list[str]:
    """Build a child runner command while keeping experimental variables owned here."""
    command = [
        python_executable,
        str(RUNNER),
        "--dataset",
        dataset,
        "--run-name",
        run_name,
        "--experiment-time",
        experiment_time,
        *group.runner_args,
        *runner_args,
    ]
    if item_limit is not None:
        command.extend(["--item-limit", str(item_limit)])
    return command


def validate_runner_args(runner_args: list[str]) -> None:
    """Reject child arguments that could invalidate the standard comparison."""
    for argument in runner_args:
        option = argument.split("=", 1)[0]
        if option in CONTROLLED_RUNNER_ARGS:
            raise ValueError(f"{option} is controlled by the comparison runner")


def preflight(
    *,
    dataset_name: str,
    required_urls: list[str],
    planned_run_names: list[str],
) -> tuple[Any, list[str]]:
    """Validate Langfuse, dataset pairing, declared services, and run uniqueness."""
    from langfuse import Langfuse

    langfuse = Langfuse()
    if not langfuse.auth_check():
        raise RuntimeError("Langfuse authentication failed")
    dataset = langfuse.get_dataset(dataset_name)
    item_ids = [str(item.id) for item in dataset.items]
    if not item_ids:
        raise ValueError(f"Dataset '{dataset_name}' is empty")
    if len(item_ids) != len(set(item_ids)):
        raise ValueError(f"Dataset '{dataset_name}' contains duplicate item IDs")

    for value in required_urls:
        name, separator, url = value.partition("=")
        if not separator or not name or not url:
            raise ValueError("--required-url must use NAME=URL")
        response = requests.get(url, timeout=10)
        if response.status_code >= 500:
            raise RuntimeError(
                f"Required service '{name}' is unhealthy: HTTP {response.status_code}"
            )

    manifest_dir = ARTIFACT_ROOT / "manifests"
    from experiment_manifest import manifest_path

    collisions = [
        run_name
        for run_name in planned_run_names
        if manifest_path(manifest_dir, run_name).exists()
    ]
    if collisions:
        raise FileExistsError(
            "Refusing to overwrite existing local runs: " + ", ".join(collisions)
        )

    remote_collisions = []
    for run_name in planned_run_names:
        try:
            langfuse.get_dataset_run(dataset_name, run_name)
        except Exception as error:
            if getattr(error, "status_code", None) == 404:
                continue
            raise RuntimeError(
                f"Unable to verify whether Langfuse run '{run_name}' exists"
            ) from error
        remote_collisions.append(run_name)
    if remote_collisions:
        raise FileExistsError(
            "Refusing to reuse existing Langfuse runs: " + ", ".join(remote_collisions)
        )
    return langfuse, item_ids


def fetch_run_results(
    langfuse: Any,
    dataset_name: str,
    run_name: str,
    evaluator_name: str,
    attempts: int = 10,
) -> dict[str, bool]:
    """Fetch per-item pass/fail results, tolerating short ingestion delays."""
    run = None
    for attempt in range(attempts):
        try:
            run = langfuse.get_dataset_run(dataset_name, run_name)
            break
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(1)

    results: dict[str, bool] = {}
    for run_item in run.dataset_run_items:
        trace = langfuse.get_trace(run_item.trace_id)
        score = next(
            (
                candidate
                for candidate in (trace.scores or [])
                if candidate.name == evaluator_name
            ),
            None,
        )
        results[str(run_item.dataset_item_id)] = bool(
            score is not None and float(score.value) >= 1.0
        )
    return results


def paired_outcomes(group_results: dict[str, dict[str, bool]]) -> dict[str, Any]:
    """Build the paired A/B/C outcome matrix for one repeat."""
    common_ids = set.intersection(
        *(set(results) for results in group_results.values())
    )
    matrix: dict[str, int] = {}
    items = []
    for item_id in sorted(common_ids):
        pattern = "".join(
            "P" if group_results[key][item_id] else "F"
            for key in ("A", "B", "C")
        )
        matrix[pattern] = matrix.get(pattern, 0) + 1
        items.append({"item_id": item_id, "A": pattern[0], "B": pattern[1], "C": pattern[2]})
    return {
        "paired_item_count": len(common_ids),
        "outcome_matrix": matrix,
        "items": items,
    }


def validate_manifest_parity(run_names: dict[str, str]) -> dict[str, Any]:
    """Ensure all non-target resolved settings are identical across A/B/C."""
    from experiment_manifest import manifest_path

    manifest_dir = ARTIFACT_ROOT / "manifests"
    manifests = {
        key: json.loads(
            manifest_path(manifest_dir, run_name).read_text(encoding="utf-8")
        )
        for key, run_name in run_names.items()
    }
    invariant_fields = (
        "dataset_name",
        "dataset_version",
        "dataset_item_ids",
        "code_commit",
        "benchmark_lifecycle_mode",
        "main_model",
        "summary_model",
        "model_endpoint",
        "temperature",
        "max_steps",
        "language",
        "max_concurrency",
        "tool_count",
        "tool_schema_hash",
        "system_prompt_hash",
        "evaluator_names",
        "evaluator_version",
    )
    mismatches = {
        field: {key: manifest.get(field) for key, manifest in manifests.items()}
        for field in invariant_fields
        if len({json.dumps(manifest.get(field), sort_keys=True) for manifest in manifests.values()}) > 1
    }
    if mismatches:
        raise RuntimeError(
            "A/B/C resolved manifest parity failed: "
            + ", ".join(sorted(mismatches))
        )
    return {
        "status": "passed",
        "checked_fields": list(invariant_fields),
        "target_fields": [
            "context_runtime",
            "context_manager_enabled",
            "context_manager.token_threshold",
            "context_component_types",
            "observation_policy",
        ],
    }


def write_report_exclusive(report: dict[str, Any], prefix: str) -> tuple[Path, Path]:
    """Write immutable JSON and Markdown comparison summaries."""
    json_path, markdown_path = comparison_report_paths(prefix)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    lines = [
        f"# ContextManager comparison: {prefix}",
        "",
        "A vs B measures runtime/assembly differences; B vs C measures compression.",
        "",
        "| Phase | Repeat | Paired | PPP | PPF | PFP | PFF | FPP | FPF | FFP | FFF |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in report["results"]:
        matrix = result["paired"]["outcome_matrix"]
        lines.append(
            f"| {result['phase']} | {result['repeat_index']} "
            f"| {result['paired']['paired_item_count']} "
            f"| {matrix.get('PPP', 0)} | {matrix.get('PPF', 0)} "
            f"| {matrix.get('PFP', 0)} | {matrix.get('PFF', 0)} "
            f"| {matrix.get('FPP', 0)} | {matrix.get('FPF', 0)} "
            f"| {matrix.get('FFP', 0)} | {matrix.get('FFF', 0)} |"
        )
    with markdown_path.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return json_path, markdown_path


def comparison_report_paths(prefix: str) -> tuple[Path, Path]:
    """Return immutable report paths for a comparison prefix."""
    output_dir = ARTIFACT_ROOT / "comparisons"
    safe_prefix = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in prefix
    )
    json_path = output_dir / f"{safe_prefix}.comparison.json"
    markdown_path = output_dir / f"{safe_prefix}.comparison.md"
    return json_path, markdown_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run-prefix", required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--smoke-items", type=int, default=1)
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--formal-items", type=int)
    parser.add_argument("--no-compression-threshold", type=int, default=1_000_000)
    parser.add_argument("--compression-threshold", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--required-url", action="append", default=[])
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--runner-args",
        nargs=argparse.REMAINDER,
        default=[],
        help="Arguments forwarded unchanged to run_benchmark.py",
    )
    args = parser.parse_args()
    for name in (
        "repeat",
        "smoke_items",
        "no_compression_threshold",
        "compression_threshold",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be greater than 0")
    if args.formal_items is not None and args.formal_items <= 0:
        parser.error("--formal-items must be greater than 0")
    try:
        validate_runner_args(args.runner_args)
    except ValueError as error:
        parser.error(str(error))
    return args


def main() -> None:
    load_dotenv()
    load_dotenv(REPO_ROOT / ".env")
    args = parse_args()
    groups = comparison_groups(
        args.no_compression_threshold,
        args.compression_threshold,
    )
    phases = []
    if not args.skip_smoke:
        phases.append(("smoke", 1, args.smoke_items))
    phases.extend(("formal", index, args.formal_items) for index in range(1, args.repeat + 1))
    report_collisions = [
        path
        for path in comparison_report_paths(args.run_prefix)
        if path.exists()
    ]
    if report_collisions:
        raise FileExistsError(
            "Refusing to overwrite comparison reports: "
            + ", ".join(str(path) for path in report_collisions)
        )

    planned = [
        build_run_name(args.run_prefix, phase, repeat_index, group)
        for phase, repeat_index, _ in phases
        for group in groups
    ]
    langfuse, dataset_item_ids = preflight(
        dataset_name=args.dataset,
        required_urls=args.required_url,
        planned_run_names=planned,
    )
    print(
        f"Preflight passed: dataset={args.dataset}, items={len(dataset_item_ids)}, "
        f"planned_runs={len(planned)}"
    )

    report = {
        "comparison_schema_version": 1,
        "run_prefix": args.run_prefix,
        "dataset_name": args.dataset,
        "dataset_item_ids": dataset_item_ids,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "thresholds": {
            "managed_no_compression": args.no_compression_threshold,
            "managed_compression": args.compression_threshold,
        },
        "evaluator_name": _primary_evaluator(args.runner_args),
        "results": [],
    }
    experiment_time = report["created_at"]
    rng = random.Random(args.seed)
    for phase, repeat_index, item_limit in phases:
        ordered_groups = list(groups)
        rng.shuffle(ordered_groups)
        run_names = {}
        for group in ordered_groups:
            run_name = build_run_name(args.run_prefix, phase, repeat_index, group)
            run_names[group.key] = run_name
            command = build_runner_command(
                python_executable=args.python,
                dataset=args.dataset,
                run_name=run_name,
                group=group,
                runner_args=args.runner_args,
                item_limit=item_limit,
                experiment_time=experiment_time,
            )
            print(f"Running {group.key} ({group.label}): {run_name}")
            subprocess.run(command, cwd=REPO_ROOT, check=True)

        group_results = {
            key: fetch_run_results(
                langfuse,
                args.dataset,
                run_name,
                report["evaluator_name"],
            )
            for key, run_name in run_names.items()
        }
        report["results"].append(
            {
                "phase": phase,
                "repeat_index": repeat_index,
                "run_names": run_names,
                "execution_order": [group.key for group in ordered_groups],
                "manifest_parity": validate_manifest_parity(run_names),
                "paired": paired_outcomes(group_results),
            }
        )

    json_path, markdown_path = write_report_exclusive(report, args.run_prefix)
    print(f"Comparison complete: {json_path}")
    print(f"Paired summary: {markdown_path}")


def _primary_evaluator(runner_args: list[str]) -> str:
    if "--evaluators" not in runner_args:
        return "exact_match"
    index = runner_args.index("--evaluators") + 1
    if index >= len(runner_args) or runner_args[index].startswith("--"):
        raise ValueError("--evaluators requires at least one evaluator")
    return runner_args[index]


if __name__ == "__main__":
    main()
