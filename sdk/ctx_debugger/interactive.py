"""Interactive context debugger REPL.

Type user messages one at a time. Each line runs one agent turn against an
accumulating conversation history with a shared ContextManager, so compression
triggers naturally as the history grows. After every turn a debug panel shows
how the context was built and compressed.

Run:
    cd /home/feiran/nexent/sdk/ctx_debugger
    /home/feiran/nexent/backend/.venv/bin/python interactive.py

Slash commands:
    /help              list commands
    /context           accumulated conversation history (turn by turn)
    /summary           current compression summary (full text)
    /tokens            per-turn token timeline
    /trace [N]         raw trace events from the last N turns (default 1)
    /step N            dump every event of agent step N in the last turn
    /config            show ContextManagerConfig
    /reset [threshold] clear history + compression state (optional new threshold)
    /quit  /q          exit
"""

import asyncio
import contextlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SDK_DIR = os.path.dirname(HERE)
BENCHMARK_DIR = os.path.join(SDK_DIR, "benchmark")
for _p in (SDK_DIR, BENCHMARK_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent_runner import build_agent_run_info, run_agent_with_tracking

# agent_runner rebinds sys.stdout to a UTF-8 TextIOWrapper over the same
# terminal buffer. Use that wrapper for our console. Do NOT restore the
# previous stdout: restoring would orphan the wrapper, and closing it on GC
# would close the shared underlying buffer, breaking output entirely.
_OUT = sys.stdout

from nexent.core.agents.agent_context import ContextManager, ContextManagerConfig
from nexent.core.agents.agent_model import AgentHistory

from ctx_debugger import ContextDebugger, attach_debugger

TRACE_PATH = os.environ.get("NEXENT_CONTEXT_DEBUG", "/tmp/nexent_ctx_interactive.jsonl")
console = Console(file=_OUT)


def _sum(events, key):
    return sum((e["data"].get(key) or 0) for e in events)


class Session:
    """One interactive debugging session: shared cm + debugger + history."""

    def __init__(self, token_threshold=3000, keep_recent_pairs=1,
                 keep_recent_steps=4, max_steps=5):
        self.max_steps = max_steps
        self.cm_config = ContextManagerConfig(
            enabled=True,
            token_threshold=token_threshold,
            keep_recent_pairs=keep_recent_pairs,
            keep_recent_steps=keep_recent_steps,
        )
        self.history = []           # list[AgentHistory]
        self.turn = 0
        self.turn_tokens = []       # list of dict per turn
        self.last_turn_events = []  # events of the most recent turn
        self._last_seq = 0

        self.shared_cm = ContextManager(config=self.cm_config, max_steps=max_steps)
        self.debugger = ContextDebugger(trace_path=TRACE_PATH)

        # Wrap the shared cm's compression layer once, up front.
        attach_debugger(self.shared_cm, existing=self.debugger, layers={"compression"})
        self._install_agent_patch()

    def _install_agent_patch(self):
        """Patch CoreAgent.__init__ so each turn's fresh agent wires its
        model/observer/tools/executor layers onto this session's debugger."""
        from nexent.core.agents.core_agent import CoreAgent

        dbg = self.debugger
        if getattr(CoreAgent, "_ctxdbg_orig_init", None) is None:
            CoreAgent._ctxdbg_orig_init = CoreAgent.__init__

        orig_init = CoreAgent._ctxdbg_orig_init

        def patched_init(agent_self, *args, **kwargs):
            orig_init(agent_self, *args, **kwargs)
            try:
                attach_debugger(
                    agent_self,
                    existing=dbg,
                    layers={"model", "observer", "tools", "executor"},
                )
            except Exception as exc:
                console.print(f"[yellow]layer attach failed: {exc}[/]")

        CoreAgent.__init__ = patched_init

    async def _run_turn_async(self, user_msg):
        info = build_agent_run_info(
            user_msg,
            list(self.history),
            max_steps=self.max_steps,
            context_manager_config=self.cm_config,
        )
        info.context_manager = self.shared_cm
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = await run_agent_with_tracking(info)
        return result

    def run_turn(self, user_msg):
        self.turn += 1
        result = asyncio.run(self._run_turn_async(user_msg))
        self.history.append(AgentHistory(role="user", content=user_msg))
        self.history.append(AgentHistory(role="assistant", content=result.final_answer))
        self.last_turn_events = self._drain_events()
        self._record_tokens()
        return result

    def _drain_events(self):
        events = []
        try:
            with open(TRACE_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    e = json.loads(line)
                    if e["seq"] > self._last_seq:
                        events.append(e)
        except FileNotFoundError:
            return []
        if events:
            self._last_seq = max(e["seq"] for e in events)
        return events

    def _record_tokens(self):
        evs = self.last_turn_events
        main = [e for e in evs if e["event"] == "llm_call_end"
                and e["data"].get("tag") == "main"]
        comp = [e for e in evs if e["event"] == "llm_call_end"
                and e["data"].get("tag") == "compression"]
        self.turn_tokens.append({
            "turn": self.turn,
            "main_in": _sum(main, "input_tokens"),
            "main_out": _sum(main, "output_tokens"),
            "comp_in": _sum(comp, "input_tokens"),
            "comp_out": _sum(comp, "output_tokens"),
        })


# ============================================================
#  Rendering
# ============================================================

def render_turn(session, result, events):
    answer = result.final_answer or "(no answer)"
    console.print(Panel(
        answer.strip(),
        title=f"Turn {session.turn}  ·  assistant",
        border_style="green",
        expand=False,
    ))

    main = [e for e in events if e["event"] == "llm_call_end"
            and e["data"].get("tag") == "main"]
    comp = [e for e in events if e["event"] == "llm_call_end"
            and e["data"].get("tag") == "compression"]
    steps = [e for e in events if e["event"] == "observer_event"
             and e["data"].get("process_type") == "step_count"]
    cbegins = [e for e in events if e["event"] == "compress_begin"]
    cends = [e for e in events if e["event"] == "compress_end"]
    tools = [e for e in events if e["event"] == "tool_call_end"]
    code = [e for e in events if e["event"] == "code_execute_end"]

    t = Table(box=box.SIMPLE, show_header=False)
    t.add_column("k", style="cyan", no_wrap=True)
    t.add_column("v")

    t.add_row("agent steps", str(len(steps)))
    if main:
        t.add_row(
            "main LLM",
            f"×{len(main)}   {_sum(main,'input_tokens')}→{_sum(main,'output_tokens')} tok"
            f"   {_sum(main,'duration_ms')/1000:.1f}s",
        )
    if comp:
        t.add_row(
            "compression LLM",
            f"×{len(comp)}   {_sum(comp,'input_tokens')}→{_sum(comp,'output_tokens')} tok"
            f"   {_sum(comp,'duration_ms')/1000:.1f}s",
        )

    if cbegins:
        for cb, ce in zip(cbegins, cends):
            pd = cb["data"].get("predicted_decision") or {}
            tc = ce["data"].get("token_counts") or {}
            unc, cmp_ = tc.get("last_uncompressed"), tc.get("last_compressed")
            ratio = f"  (-{(1 - cmp_/unc)*100:.0f}%)" if unc and cmp_ else ""
            sc = ce["data"].get("summary_changed") or {}
            changed = []
            if sc.get("previous_changed"):
                changed.append("previous")
            if sc.get("current_changed"):
                changed.append("current")
            t.add_row(
                "compression",
                f"[bold]TRIGGERED[/]  branch={pd.get('branch')}  "
                f"{unc}→{cmp_} tok{ratio}",
            )
            if changed:
                t.add_row("", f"summary updated: {', '.join(changed)}")
    else:
        t.add_row("compression", "[dim]not triggered[/]")

    if code:
        t.add_row("code exec", f"×{len(code)}")
    if tools:
        names = ", ".join(e["data"].get("tool", "?") for e in tools)
        t.add_row("tool calls", names)

    errors = [e for e in events if e["event"] == "debug_error"]
    if errors:
        t.add_row("debug errors", f"[red]{len(errors)}[/] (see /trace)")

    console.print(Panel(t, title="context construction", border_style="blue",
                         expand=False))


# ============================================================
#  Slash commands
# ============================================================

def _print_config(session):
    c = session.cm_config
    t = Table(box=box.SIMPLE, show_header=False)
    t.add_column("k", style="cyan")
    t.add_column("v")
    t.add_row("token_threshold", str(c.token_threshold))
    t.add_row("keep_recent_pairs", str(c.keep_recent_pairs))
    t.add_row("keep_recent_steps", str(c.keep_recent_steps))
    t.add_row("max_steps", str(session.max_steps))
    t.add_row("trace file", TRACE_PATH)
    console.print(Panel(t, title="ContextManagerConfig", border_style="dim",
                         expand=False))


def _cmd_context(session):
    if not session.history:
        console.print("[dim](no history yet)[/]")
        return
    t = Table(box=box.SIMPLE)
    t.add_column("#", justify="right")
    t.add_column("role", style="cyan")
    t.add_column("content")
    for i, h in enumerate(session.history):
        content = h.content if isinstance(h.content, str) else str(h.content)
        if len(content) > 200:
            content = content[:200] + f" …[+{len(content)-200} chars]"
        t.add_row(str(i), h.role, content.replace("\n", " "))
    console.print(Panel(t, title=f"Conversation history ({len(session.history)} msgs)",
                         border_style="blue", expand=False))


def _cmd_summary(session):
    s = session.shared_cm.export_summary()
    prev = s.get("previous_summary")
    curr = s.get("current_summary")
    if not prev and not curr:
        console.print("[dim](no compression summary yet — nothing compressed)[/]")
        return
    if prev:
        console.print(Panel(prev, title="previous_summary", border_style="yellow",
                             expand=False))
    if curr:
        console.print(Panel(curr, title="current_summary", border_style="yellow",
                             expand=False))
    boundary = s.get("compression_boundary") or {}
    console.print(f"[dim]boundary: {boundary}[/]")


def _cmd_tokens(session):
    if not session.turn_tokens:
        console.print("[dim](no turns yet)[/]")
        return
    t = Table(box=box.SIMPLE_HEAD, title="Token timeline")
    t.add_column("Turn", justify="right")
    t.add_column("Main in", justify="right")
    t.add_column("Main out", justify="right")
    t.add_column("Comp in", justify="right")
    t.add_column("Comp out", justify="right")
    for tk in session.turn_tokens:
        t.add_row(
            str(tk["turn"]),
            str(tk["main_in"]), str(tk["main_out"]),
            str(tk["comp_in"] or "-"), str(tk["comp_out"] or "-"),
        )
    console.print(t)


def _cmd_trace(session, arg):
    events = session.last_turn_events
    if not events:
        console.print("[dim](no events from last turn)[/]")
        return
    t = Table(box=box.SIMPLE, title="Last turn — raw events")
    t.add_column("seq", justify="right")
    t.add_column("step", justify="right")
    t.add_column("event", style="cyan")
    t.add_column("detail")
    for e in events:
        d = e["data"]
        ev = e["event"]
        if ev == "llm_call_end":
            detail = (f"tag={d.get('tag')} dur={d.get('duration_ms')}ms "
                      f"in={d.get('input_tokens')} out={d.get('output_tokens')}")
        elif ev == "compress_begin":
            pd = d.get("predicted_decision") or {}
            detail = f"branch={pd.get('branch')}"
        elif ev == "compression_call":
            detail = (f"type={d.get('call_type')} cache={d.get('cache_hit')} "
                      f"in={d.get('input_tokens')} out={d.get('output_tokens')}")
        elif ev == "compress_end":
            tc = d.get("token_counts") or {}
            detail = f"{tc.get('last_uncompressed')}→{tc.get('last_compressed')}"
        elif ev == "observer_event":
            detail = f"[{d.get('process_type')}]"
        elif ev == "code_execute_end":
            detail = f"dur={d.get('duration_ms')}ms final={d.get('is_final_answer')}"
        elif ev == "tool_call_end":
            detail = f"tool={d.get('tool')} dur={d.get('duration_ms')}ms"
        elif ev == "debug_error":
            detail = f"[red]{d.get('phase')}: {d.get('error')}[/]"
        else:
            detail = ""
        t.add_row(str(e["seq"]), str(e.get("agent_step") or "-"), ev, detail)
    console.print(t)


def _cmd_step(session, arg):
    try:
        step_n = int(arg)
    except (ValueError, TypeError):
        console.print("[red]usage: /step N[/]")
        return
    events = [e for e in session.last_turn_events
              if e.get("agent_step") == step_n]
    if not events:
        console.print(f"[dim](no events at step {step_n} in last turn)[/]")
        return
    for e in events:
        content = json.dumps(e["data"], ensure_ascii=False, indent=2)
        if len(content) > 3000:
            content = content[:3000] + f"\n…[+{len(content)-3000} chars]"
        console.print(Panel(content, title=f"seq={e['seq']} {e['event']}",
                             border_style="cyan", expand=False))


HELP = """[bold]Commands[/]
  /help              this help
  /context           accumulated conversation history
  /summary           current compression summary (full text)
  /tokens            per-turn token timeline
  /trace             raw trace events from the last turn
  /step N            dump every event of agent step N (last turn)
  /config            show ContextManagerConfig
  /reset [threshold] fresh session, optionally new token_threshold
  /quit  /q          exit

Anything not starting with / is sent to the agent as a user turn."""


def handle_command(session, line):
    """Return (new_session_or_None, should_quit)."""
    parts = line.split()
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else None

    if cmd in ("/quit", "/q", "/exit"):
        return None, True
    if cmd == "/help":
        console.print(Panel(HELP, border_style="magenta", expand=False))
    elif cmd == "/context":
        _cmd_context(session)
    elif cmd == "/summary":
        _cmd_summary(session)
    elif cmd == "/tokens":
        _cmd_tokens(session)
    elif cmd == "/trace":
        _cmd_trace(session, arg)
    elif cmd == "/step":
        _cmd_step(session, arg)
    elif cmd == "/config":
        _print_config(session)
    elif cmd == "/reset":
        threshold = session.cm_config.token_threshold
        if arg:
            try:
                threshold = int(arg)
            except ValueError:
                console.print("[red]threshold must be an integer[/]")
                return session, False
        new = Session(token_threshold=threshold)
        console.print(f"[green]session reset[/] (token_threshold={threshold})")
        return new, False
    else:
        console.print(f"[red]unknown command: {cmd}[/]  (/help)")
    return session, False


def main():
    console.print(Panel(
        "Nexent Context Debugger — interactive REPL\n"
        "Type a message to run one agent turn. /help for commands.",
        border_style="magenta", expand=False,
    ))
    session = Session()
    _print_config(session)

    while True:
        try:
            line = console.input("\n[bold cyan]you>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye.[/]")
            break

        if not line:
            continue

        if line.startswith("/"):
            session, should_quit = handle_command(session, line)
            if should_quit:
                console.print("[dim]bye.[/]")
                break
            continue

        with console.status("[dim]running agent turn…[/]"):
            try:
                result = session.run_turn(line)
            except Exception as exc:
                console.print(f"[red]turn failed: {exc}[/]")
                import traceback
                traceback.print_exc(file=_OUT)
                continue

        render_turn(session, result, session.last_turn_events)


if __name__ == "__main__":
    main()
