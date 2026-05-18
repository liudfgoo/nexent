"""Export a ctx_debugger JSONL trace into Langfuse for visual analysis.

This is the "option 1" adapter: instead of building a custom web UI, map the
trace onto a self-hosted Langfuse instance and get nested traces, drill-down,
token/cost views and session grouping for free.

Mapping:
    each agent turn (an `agent_init` event)  -> one Langfuse trace
    llm_call_begin/end                       -> a generation
    compress_begin/end                       -> a span wrapping its
                                                 compression generations
    tool_call_begin/end                      -> a tool observation
    code_execute_begin/end                   -> a span
    the whole file                           -> one Langfuse session

Usage (from sdk/):
    python -m ctx_debugger.langfuse_export <trace.jsonl> [options]

Options:
    --session-id ID   Langfuse session id (default: <file stem>-<timestamp>)
    --dry-run         Print the mapped trace tree; do not contact Langfuse
    --host URL        Langfuse host (else $LANGFUSE_HOST)

Langfuse credentials are read from the environment, the standard way:
    LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY

Known limitation: observations are created at export time, so each one's
duration is faithful but absolute placement on the Langfuse timeline is the
export moment, not the original wall-clock time.
"""

import argparse
import contextlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

# Begin event -> its matching end event. Everything else is standalone.
BEGIN_TO_END = {
    "compress_begin": "compress_end",
    "llm_call_begin": "llm_call_end",
    "code_execute_begin": "code_execute_end",
    "tool_call_begin": "tool_call_end",
}
END_EVENTS = set(BEGIN_TO_END.values())


class Obs:
    """One Langfuse observation built from a begin/end event pair."""

    __slots__ = ("as_type", "name", "input", "output", "metadata",
                 "usage", "duration_ms", "children")

    def __init__(self, as_type: str, name: str):
        self.as_type = as_type
        self.name = name
        self.input: Any = None
        self.output: Any = None
        self.metadata: Dict[str, Any] = {}
        self.usage: Optional[Dict[str, int]] = None
        self.duration_ms: Optional[float] = None
        self.children: List["Obs"] = []


# ============================================================
#  Trace file -> per-turn segments
# ============================================================

def _load(path: str) -> List[dict]:
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _split_turns(events: List[dict]) -> List[dict]:
    """Split a flat event list into per-turn segments, one per agent_init."""
    turns: List[dict] = []
    current: Optional[dict] = None
    orphan: List[dict] = []
    for e in events:
        ev = e["event"]
        if ev == "run_begin":
            continue
        if ev == "agent_init":
            if current is not None:
                turns.append(current)
            current = {"init": e, "events": []}
        elif current is None:
            orphan.append(e)
        else:
            current["events"].append(e)
    if current is not None:
        turns.append(current)
    if orphan:
        if turns:
            turns[0]["events"] = orphan + turns[0]["events"]
        else:
            turns.append({"init": None, "events": orphan})
    return turns


# ============================================================
#  Events -> intermediate observation tree
# ============================================================

def _chat(input_messages: Any) -> List[dict]:
    """Render captured input_messages as a chat list for Langfuse."""
    out = []
    for m in input_messages or []:
        out.append({
            "role": m.get("role"),
            "content": m.get("text") or m.get("preview") or "",
        })
    return out


def _begin_obs(begin_ev: str, data: dict) -> Obs:
    if begin_ev == "llm_call_begin":
        tag = data.get("tag", "?")
        o = Obs("generation", f"{tag} LLM call")
        o.input = _chat(data.get("input_messages"))
        o.metadata = {"tag": tag, "stop_sequences": data.get("stop_sequences")}
        return o
    if begin_ev == "compress_begin":
        o = Obs("span", "compression")
        o.input = {
            "predicted_decision": data.get("predicted_decision"),
            "estimated_tokens": data.get("estimated_tokens"),
        }
        o.metadata = {
            "compression_step": data.get("compression_step"),
            "config": data.get("config"),
            "summary_before": data.get("summary_before"),
        }
        return o
    if begin_ev == "code_execute_begin":
        o = Obs("span", "code execution")
        o.input = data.get("code_preview")
        o.metadata = {"code_chars": data.get("code_chars")}
        return o
    if begin_ev == "tool_call_begin":
        o = Obs("tool", f"tool: {data.get('tool', '?')}")
        o.input = {"args": data.get("args"), "kwargs": data.get("kwargs")}
        return o
    return Obs("span", begin_ev)


def _finish_obs(obs: Obs, begin_ev: str, begin_e: dict, end_e: dict) -> None:
    d = end_e["data"]
    obs.duration_ms = round((end_e["ts"] - begin_e["ts"]) * 1000, 1)
    if begin_ev == "llm_call_begin":
        obs.output = d.get("output_full") or d.get("output_preview")
        it, ot = d.get("input_tokens"), d.get("output_tokens")
        if it is not None or ot is not None:
            obs.usage = {"input": it or 0, "output": ot or 0}
        if d.get("error"):
            obs.metadata["error"] = d["error"]
    elif begin_ev == "compress_begin":
        obs.output = {
            "token_counts": d.get("token_counts"),
            "summary_changed": d.get("summary_changed"),
            "summary_after": d.get("summary_after"),
        }
        obs.metadata["success"] = d.get("success")
        obs.metadata["step_stats"] = d.get("step_stats")
    elif begin_ev == "code_execute_begin":
        obs.output = {
            "output": d.get("output_preview"),
            "logs": d.get("logs_preview"),
        }
        obs.metadata["is_final_answer"] = d.get("is_final_answer")
    elif begin_ev == "tool_call_begin":
        obs.output = d.get("return_preview")
        obs.metadata["return_type"] = d.get("return_type")


def _build_tree(events: List[dict]) -> List[Obs]:
    """Pair begin/end events into a nested observation tree."""
    roots: List[Obs] = []
    stack: List[tuple] = []  # (obs, begin_event, begin_ev_name)
    for e in events:
        ev = e["event"]
        if ev in BEGIN_TO_END:
            obs = _begin_obs(ev, e["data"])
            (stack[-1][0].children if stack else roots).append(obs)
            stack.append((obs, e, ev))
        elif ev in END_EVENTS:
            for i in range(len(stack) - 1, -1, -1):
                obs, begin_e, begin_ev = stack[i]
                if BEGIN_TO_END[begin_ev] == ev:
                    _finish_obs(obs, begin_ev, begin_e, e)
                    del stack[i:]  # close it (and any left wrongly open)
                    break
        elif ev == "compression_call":
            for obs, _be, begin_ev in reversed(stack):
                if begin_ev == "compress_begin":
                    obs.metadata.setdefault("compression_calls", []).append(e["data"])
                    break
        elif ev == "debug_error":
            target = stack[-1][0].metadata if stack else None
            if target is not None:
                target.setdefault("debug_errors", []).append(e["data"])
        # observer_event and others are intentionally skipped (noise).
    return roots


def _init_payload(init: Optional[dict]):
    if not init:
        return None, {}
    d = init["data"]
    inp = {
        "agent": d.get("agent_name"),
        "agent_class": d.get("agent_class"),
        "tools": [t.get("name") for t in d.get("tools", [])],
    }
    meta = {
        "system_prompt": d.get("system_prompt"),
        "system_prompt_chars": d.get("system_prompt_chars"),
        "max_steps": d.get("max_steps"),
        "context_manager_config": d.get("context_manager_config"),
    }
    return inp, meta


# ============================================================
#  Dry-run printer
# ============================================================

def _print_turns(turns: List[dict]) -> None:
    for i, turn in enumerate(turns, 1):
        roots = _build_tree(turn["events"])
        init = turn["init"]
        agent = (init["data"].get("agent_name") if init else None) or "agent"
        print(f"\n● trace: turn {i} · {agent}")
        for o in roots:
            _print_obs(o, 1)


def _print_obs(o: Obs, depth: int) -> None:
    pad = "  " * depth
    dur = f"{o.duration_ms / 1000:.1f}s" if o.duration_ms else "-"
    extra = ""
    if o.usage:
        extra = f"   in={o.usage['input']} out={o.usage['output']} tok"
    print(f"{pad}{o.name}  [{o.as_type}]  {dur}{extra}")
    for c in o.children:
        _print_obs(c, depth + 1)


# ============================================================
#  Langfuse push
# ============================================================

def _clean(d: Optional[dict]) -> dict:
    return {k: v for k, v in (d or {}).items() if v is not None}


def _emit(parent, o: Obs) -> None:
    """Recursively create a Langfuse observation and its children."""
    start_ns = time.time_ns()
    kwargs: Dict[str, Any] = {"name": o.name, "as_type": o.as_type}
    if o.input is not None:
        kwargs["input"] = o.input
    md = _clean(o.metadata)
    if md:
        kwargs["metadata"] = md
    if o.usage and o.as_type == "generation":
        kwargs["usage_details"] = o.usage
    child = parent.start_observation(**kwargs)
    for c in o.children:
        _emit(child, c)
    if o.output is not None:
        child.update(output=o.output)
    # Explicit end_time so the displayed duration matches the recorded one.
    child.end(end_time=start_ns + int((o.duration_ms or 0) * 1e6))


def _export(turns: List[dict], session_id: str) -> None:
    from langfuse import Langfuse
    try:
        from langfuse import propagate_attributes
    except Exception:  # pragma: no cover - older/newer SDK layout
        propagate_attributes = None

    client = Langfuse()
    for i, turn in enumerate(turns, 1):
        roots = _build_tree(turn["events"])
        init = turn["init"]
        agent = (init["data"].get("agent_name") if init else None) or "agent"
        inp, meta = _init_payload(init)

        all_ev = turn["events"] + ([init] if init else [])
        t0 = min((e["ts"] for e in all_ev), default=time.time())
        t1 = max((e["ts"] for e in all_ev), default=t0)

        ctx = (propagate_attributes(session_id=session_id, trace_name=f"turn-{i}")
               if propagate_attributes else contextlib.nullcontext())
        with ctx:
            start_ns = time.time_ns()
            root = client.start_observation(
                name=f"turn {i}: {agent}", as_type="span",
                input=inp, metadata=_clean(meta),
            )
            for o in roots:
                _emit(root, o)
            root.end(end_time=start_ns + int((t1 - t0) * 1e9))
    client.flush()


# ============================================================
#  CLI
# ============================================================

def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ctx-langfuse-export",
        description="Export a ctx_debugger JSONL trace into Langfuse.",
    )
    ap.add_argument("trace", help="Path to a ctx_debugger JSONL trace file.")
    ap.add_argument("--session-id", help="Langfuse session id to group turns.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the mapped trace tree; do not contact Langfuse.")
    ap.add_argument("--host", help="Langfuse host (else $LANGFUSE_HOST).")
    args = ap.parse_args()

    events = _load(args.trace)
    turns = _split_turns(events)
    if not turns:
        sys.exit("No turns (agent_init events) found in this trace.")

    stem = os.path.splitext(os.path.basename(args.trace))[0]
    session_id = args.session_id or f"{stem}-{time.strftime('%m%d-%H%M%S')}"

    if args.dry_run:
        print(f"DRY RUN — {len(turns)} turn(s), session_id={session_id}")
        _print_turns(turns)
        return

    if args.host:
        os.environ["LANGFUSE_HOST"] = args.host
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY")
            and os.environ.get("LANGFUSE_SECRET_KEY")):
        sys.exit("ERROR: set LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY "
                 "(and LANGFUSE_HOST), or use --dry-run.")

    _export(turns, session_id)
    print(f"Exported {len(turns)} turn(s) to Langfuse — session_id={session_id}")


if __name__ == "__main__":
    main()
