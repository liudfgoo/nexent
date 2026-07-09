"""Unit tests for OffloadStore."""

import os
import sys

import pytest

# OffloadStore is pure stdlib -- no smolagents dependency.
# Add the sdk root so that ``sdk.nexent...`` imports resolve.
_sdk_root = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "..", "sdk")
)
if _sdk_root not in sys.path:
    sys.path.insert(0, _sdk_root)

from sdk.nexent.core.agents.agent_context.offload_store import OffloadStore


class TestOffloadStoreBasic:
    def test_store_returns_handle(self):
        store = OffloadStore()
        handle = store.store("some content", "test description")
        assert handle is not None
        assert isinstance(handle, str)
        assert len(handle) == 32  # uuid hex

    def test_reload_returns_content(self):
        store = OffloadStore()
        handle = store.store("hello world", "greeting")
        assert store.reload(handle) == "hello world"

    def test_reload_miss_returns_none(self):
        store = OffloadStore()
        assert store.reload("nonexistent") is None

    def test_list_active(self):
        store = OffloadStore()
        h1 = store.store("content1", "desc1")
        h2 = store.store("content2", "desc2")
        active = store.list_active()
        handles = [h for h, _, _ in active]
        assert h1 in handles
        assert h2 in handles

    def test_len(self):
        store = OffloadStore()
        assert len(store) == 0
        store.store("content", "desc")
        assert len(store) == 1

    def test_clear(self):
        store = OffloadStore()
        store.store("content", "desc")
        store.clear()
        assert len(store) == 0


class TestOffloadStoreEviction:
    def test_max_entries_eviction(self):
        store = OffloadStore(max_entries=2)
        h1 = store.store("c1", "d1")
        h2 = store.store("c2", "d2")
        h3 = store.store("c3", "d3")
        # h1 should be evicted (oldest)
        assert store.reload(h1) is None
        assert store.reload(h2) == "c2"
        assert store.reload(h3) == "c3"

    def test_max_entry_chars_rejection(self):
        store = OffloadStore(max_entry_chars=10)
        handle = store.store("x" * 100, "too long")
        assert handle is None

    def test_reload_diagnostics(self):
        store = OffloadStore()
        h = store.store("content", "desc")
        store.reload(h)
        store.reload("missing")
        assert store.reload_hits == 1
        assert store.reload_misses == 1


class TestOffloadStoreInventory:
    def test_disabled_returns_none(self):
        store = OffloadStore()
        store.store("content", "desc")
        assert store.build_reload_inventory(enable_reload=False) is None

    def test_empty_store_returns_none(self):
        store = OffloadStore()
        assert store.build_reload_inventory(enable_reload=True) is None

    def test_inventory_contains_handle(self):
        store = OffloadStore()
        handle = store.store("content", "test desc")
        inv = store.build_reload_inventory(enable_reload=True)
        assert handle in inv
        assert "test desc" in inv

    def test_query_scoring(self):
        store = OffloadStore()
        store.store("db results", "database query results")
        store.store("file content", "file read output")
        inv = store.build_reload_inventory(enable_reload=True, query="database search")
        assert "database" in inv.lower()

    def test_cjk_tokenization(self):
        store = OffloadStore()
        store.store("content1", "数据库查询结果")
        store.store("content2", "文件读取输出")
        inv = store.build_reload_inventory(enable_reload=True, query="数据库")
        assert "数据库" in inv or "数据" in inv
