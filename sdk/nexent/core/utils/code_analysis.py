"""Code analysis utilities for extracting tool usage information from agent-generated code."""

import ast
import logging
from typing import List

logger = logging.getLogger("code_analysis")

_MAX_SUMMARY_FIRST_LINE = 120


def extract_invoked_tools(code_action: str, registered_tools: dict) -> List[str]:
    """Extract registered tool names called in code_action via AST analysis.

    Walks the AST to find all ``ast.Call`` nodes whose func is an ``ast.Name``,
    then intersects with the keys of *registered_tools* (typically
    ``self.tools`` on the agent).  Returns a **sorted** list of matched tool
    names (duplicates removed).
    """
    if not code_action:
        return []
    try:
        tree = ast.parse(code_action)
    except SyntaxError:
        logger.warning("Failed to parse code_action for invoked_tools extraction")
        return []
    called_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)
    return sorted(name for name in called_names if name in registered_tools)


def _render_arg_value(node: ast.AST, max_value_len: int) -> str:
    """Render one call-argument value with size-bounded placeholders."""
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, str):
            return repr(v) if len(v) <= max_value_len else f"<str:{len(v)} chars>"
        return repr(v)
    try:
        src = ast.unparse(node)
    except Exception:
        return "<expr>"
    if len(src) <= max_value_len:
        return src
    return f"<{type(node).__name__.lower()}:{len(src)} chars>"


def _render_call_signature(node: ast.Call, max_value_len: int, max_sig_len: int) -> str:
    """Render a single ``tool(...)`` call into a compact, length-bounded signature."""
    parts: List[str] = []
    for arg in node.args:
        parts.append(_render_arg_value(arg, max_value_len))
    for kw in node.keywords:
        name = kw.arg if kw.arg is not None else "**"
        parts.append(f"{name}={_render_arg_value(kw.value, max_value_len)}")
    sig = f"{node.func.id}(" + ", ".join(parts) + ")"
    if len(sig) > max_sig_len:
        sig = sig[:max_sig_len] + f"...(+{len(parts)} args)"
    return sig


def extract_invoked_tool_signatures(
    code_action: str,
    registered_tools: dict,
    max_value_len: int = 60,
    max_sig_len: int = 200,
) -> List[str]:
    """Extract compact call signatures for registered tools invoked in code.

    Unlike :func:`extract_invoked_tools` (bare names), this preserves the call
    shape so a compacted TOOL_CALL message retains causal info. Large argument
    values collapse to size-describing placeholders. Returns [] on syntax errors.
    """
    if not code_action:
        return []
    try:
        tree = ast.parse(code_action)
    except SyntaxError:
        logger.warning("Failed to parse code_action for invoked_tool signatures")
        return []

    nested_arg_calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for sub in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(sub, ast.Call):
                    nested_arg_calls.add(id(sub))

    signatures: List[str] = []
    seen = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id not in registered_tools:
            continue
        if id(node) in nested_arg_calls:
            continue
        try:
            sig = _render_call_signature(node, max_value_len, max_sig_len)
        except Exception:
            sig = node.func.id
        if sig not in seen:
            seen.add(sig)
            signatures.append(sig)
    return signatures


def summarize_pure_python(code_action: str) -> str:
    """Produce a compact, readable summary for pure-Python code_action.

    When ``extract_invoked_tool_signatures`` finds no registered-tool calls,
    the code consists solely of built-in Python (loops, math, print, etc.).
    Showing a ``truncate_content(code, 100)`` fragment yields broken syntax
    that confuses the LLM.  Instead we return the first meaningful line
    (comment or statement) plus a line-count hint so the model understands
    *what* was executed without seeing the full source.

    Examples::

        >>> summarize_pure_python("# compute squares\\nfor i in range(10):\\n    print(i**2)")
        '# compute squares … (3 lines)'

        >>> summarize_pure_python("results = {}\\nfor i in range(10, 16):\\n    results[i] = 2 ** i")
        'results = {} … (3 lines)'
    """
    if not code_action or not code_action.strip():
        return "<empty code block>"
    lines = code_action.strip().splitlines()
    first_line = ""
    for line in lines:
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break
    if not first_line:
        return "<empty code block>"
    if len(first_line) > _MAX_SUMMARY_FIRST_LINE:
        first_line = first_line[:_MAX_SUMMARY_FIRST_LINE] + "…"
    total = len(lines)
    suffix = f" … ({total} lines)" if total > 1 else ""
    return first_line + suffix
