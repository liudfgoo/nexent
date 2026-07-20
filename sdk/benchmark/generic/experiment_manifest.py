"""Resolved experiment manifest helpers for Generic Benchmark runs."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


MANIFEST_SCHEMA_VERSION = 1
SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "headers",
    "password",
    "secret",
    "token",
}
TOOL_SCHEMA_FIELDS = (
    "class_name",
    "name",
    "description",
    "inputs",
    "output_type",
    "params",
    "source",
    "usage",
    "labels",
)
MAX_SERIALIZATION_DEPTH = 50


def canonical_json(value: Any) -> str:
    """Serialize a value deterministically for hashing and persistence."""
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_value(value: Any) -> str:
    """Return a stable SHA-256 digest for a JSON-compatible value."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def resolve_code_commit(repo_root: Path) -> str:
    """Resolve the exact Git commit used by the benchmark."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def build_manifest(
    *,
    dataset_name: str,
    dataset_version: str | None,
    dataset_item_ids: list[str],
    run_name: str,
    repo_root: Path,
    lifecycle_mode: str,
    context_manager_config: Any,
    max_steps: int,
    temperature: float,
    language: str,
    max_concurrency: int,
    model_config: dict[str, Any],
    tools: list[Any],
    system_prompt: str,
    agent_config: dict[str, Any],
    evaluator_names: list[str],
    observation_policy: dict[str, Any],
    started_at: str | None = None,
) -> dict[str, Any]:
    """Build a manifest from final effective values, not raw CLI inputs."""
    cm_config = _jsonable(context_manager_config)
    enabled = bool(cm_config.get("enabled", False))
    runtime = "managed" if enabled else "legacy"
    tool_payload = _tool_schema_payload(tools)
    model_endpoint = _sanitize_endpoint(
        model_config.get("url") or model_config.get("base_url") or ""
    )

    manifest = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "dataset_name": dataset_name,
        "dataset_version": dataset_version,
        "dataset_item_ids": dataset_item_ids,
        "run_name": run_name,
        "code_commit": resolve_code_commit(repo_root),
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
        "environment": {
            "hostname": platform.node(),
            "python_version": platform.python_version(),
        },
        "benchmark_lifecycle_mode": lifecycle_mode,
        "context_runtime": runtime,
        "context_manager_enabled": enabled,
        "context_manager": cm_config,
        "main_model": model_config.get("model_name", ""),
        "summary_model": model_config.get("model_name", ""),
        "summary_uses_main_model": True,
        "model_endpoint": model_endpoint,
        "model_provider": _provider_from_endpoint(model_endpoint),
        "temperature": temperature,
        "max_steps": max_steps,
        "language": language,
        "max_concurrency": max_concurrency,
        "tool_count": len(tool_payload),
        "tool_schema_hash": sha256_value(tool_payload),
        "system_prompt_hash": sha256_value(system_prompt),
        "agent_config_hash": sha256_value(agent_config),
        "evaluator_names": evaluator_names,
        "evaluator_version": "code_commit",
        "context_component_types": agent_config.get("context_component_types", []),
        "observation_policy": observation_policy,
    }
    manifest["manifest_hash"] = sha256_value(manifest)
    return manifest


def write_manifest_exclusive(manifest: dict[str, Any], output_dir: Path) -> Path:
    """Persist a manifest without ever overwriting an existing run artifact."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_path(output_dir, manifest["run_name"])
    with path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def manifest_path(output_dir: Path, run_name: str) -> Path:
    """Return the canonical local manifest path for a run name."""
    safe_run_name = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in run_name
    )
    return output_dir / f"{safe_run_name}.manifest.json"


def _tool_schema_payload(tools: list[Any]) -> list[dict[str, Any]]:
    """Project runtime ToolConfig objects to stable, credential-free schemas."""
    payload = []
    for tool in tools:
        if isinstance(tool, dict):
            schema = {
                field: tool[field]
                for field in TOOL_SCHEMA_FIELDS
                if field in tool
            }
        else:
            schema = {
                field: getattr(tool, field)
                for field in TOOL_SCHEMA_FIELDS
                if hasattr(tool, field)
            }
        payload.append(_jsonable(schema))
    return payload


def _jsonable(
    value: Any,
    *,
    _seen: set[int] | None = None,
    _depth: int = 0,
) -> Any:
    """Convert values safely, stopping cycles and excluding runtime internals."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if _depth >= MAX_SERIALIZATION_DEPTH:
        return f"[MAX_DEPTH:{type(value).__name__}]"

    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        return f"[CYCLE:{type(value).__name__}]"
    seen.add(identity)
    next_depth = _depth + 1

    if is_dataclass(value):
        result = {
            field.name: _jsonable(
                getattr(value, field.name),
                _seen=seen,
                _depth=next_depth,
            )
            for field in fields(value)
        }
        seen.remove(identity)
        return result
    if isinstance(value, dict):
        result = {
            str(key): (
                "[REDACTED]"
                if str(key).lower() in SENSITIVE_KEYS
                else _jsonable(item, _seen=seen, _depth=next_depth)
            )
            for key, item in value.items()
        }
        seen.remove(identity)
        return result
    if isinstance(value, (list, tuple, set)):
        result = [
            _jsonable(item, _seen=seen, _depth=next_depth)
            for item in value
        ]
        seen.remove(identity)
        return result
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump(exclude={"metadata"})
        except (RecursionError, TypeError, ValueError):
            dumped = None
        if dumped is not None:
            result = _jsonable(dumped, _seen=seen, _depth=next_depth)
            seen.remove(identity)
            return result
    if hasattr(value, "__dict__"):
        public = {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_")
            and key.lower() not in SENSITIVE_KEYS
            and key != "metadata"
        }
        result = _jsonable(public, _seen=seen, _depth=next_depth)
        seen.remove(identity)
        return result
    seen.remove(identity)
    return str(value)


def _sanitize_endpoint(endpoint: str) -> str:
    if not endpoint:
        return ""
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname or ""
    if parsed.port:
        hostname = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))


def _provider_from_endpoint(endpoint: str) -> str:
    lowered = endpoint.lower()
    for provider in ("openai", "anthropic", "azure", "volcengine", "aliyun"):
        if provider in lowered:
            return provider
    return "unknown"
