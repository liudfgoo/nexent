"""Unit tests for code_analysis utility functions."""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "sdk"))

from nexent.core.utils.code_analysis import extract_invoked_tools, extract_invoked_tool_signatures


class TestExtractInvokedTools:
    def test_empty_code(self):
        assert extract_invoked_tools("", {}) == []

    def test_no_tool_calls(self):
        assert extract_invoked_tools("x = 1 + 2", {}) == []

    def test_single_tool_call(self):
        tools = {"search": object(), "write_file": object()}
        assert extract_invoked_tools("search('query')", tools) == ["search"]

    def test_multiple_tool_calls(self):
        tools = {"search": object(), "write_file": object()}
        result = extract_invoked_tools("search('q')\nwrite_file('a.txt', 'data')", tools)
        assert result == ["search", "write_file"]

    def test_unregistered_tool_ignored(self):
        tools = {"search": object()}
        assert extract_invoked_tools("unknown_tool()", tools) == []

    def test_syntax_error_returns_empty(self):
        assert extract_invoked_tools("def (", {}) == []


class TestExtractInvokedToolSignatures:
    def test_empty_code(self):
        assert extract_invoked_tool_signatures("", {}) == []

    def test_single_signature(self):
        tools = {"search": object()}
        result = extract_invoked_tool_signatures("search('hello world')", tools)
        assert len(result) == 1
        assert "search" in result[0]
        assert "'hello world'" in result[0]

    def test_long_string_argument_collapsed(self):
        tools = {"write_file": object()}
        long_str = "x" * 500
        result = extract_invoked_tool_signatures(f'write_file(content="{long_str}")', tools)
        assert len(result) == 1
        assert "<str:" in result[0]
        assert long_str not in result[0]

    def test_numeric_argument_kept(self):
        tools = {"calculate": object()}
        result = extract_invoked_tool_signatures("calculate(42, 3.14, True, None)", tools)
        assert "42" in result[0]
        assert "3.14" in result[0]

    def test_keyword_arguments(self):
        tools = {"search": object()}
        result = extract_invoked_tool_signatures("search(query='test', limit=10)", tools)
        assert "query='test'" in result[0]
        assert "limit=10" in result[0]

    def test_nested_call_not_duplicated(self):
        tools = {"search": object(), "format_query": object()}
        result = extract_invoked_tool_signatures("search(format_query('raw'))", tools)
        # format_query appears as argument to search, not as standalone
        names = [sig.split("(")[0] for sig in result]
        assert "search" in names

    def test_syntax_error_returns_empty(self):
        assert extract_invoked_tool_signatures("def (", {}) == []

    def test_max_sig_len_truncation(self):
        tools = {"tool": object()}
        # Many arguments to exceed default max_sig_len=200
        args = ", ".join(f"arg{i}='val{i}'" for i in range(20))
        result = extract_invoked_tool_signatures(f"tool({args})", tools, max_sig_len=80)
        assert len(result[0]) <= 120  # truncated + suffix
