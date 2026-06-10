import asyncio
import json

import pytest

from consts.exceptions import WorkingMemoryError
from services import working_memory_service as wm


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.ops = []

    def hset(self, *args, **kwargs):
        self.ops.append(("hset", args, kwargs))
        return self

    def hdel(self, *args, **kwargs):
        self.ops.append(("hdel", args, kwargs))
        return self

    def zadd(self, *args, **kwargs):
        self.ops.append(("zadd", args, kwargs))
        return self

    def zrem(self, *args, **kwargs):
        self.ops.append(("zrem", args, kwargs))
        return self

    def expire(self, *args, **kwargs):
        self.ops.append(("expire", args, kwargs))
        return self

    def rpush(self, *args, **kwargs):
        self.ops.append(("rpush", args, kwargs))
        return self

    def ltrim(self, *args, **kwargs):
        self.ops.append(("ltrim", args, kwargs))
        return self

    def hincrby(self, *args, **kwargs):
        self.ops.append(("hincrby", args, kwargs))
        return self

    def execute(self):
        results = []
        for name, args, kwargs in self.ops:
            results.append(getattr(self.client, name)(*args, **kwargs))
        return results


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.zsets = {}
        self.lists = {}
        self.expire_calls = []
        self.deleted_keys = []

    def pipeline(self):
        return FakePipeline(self)

    def hset(self, key, field=None, value=None, mapping=None):
        target = self.hashes.setdefault(key, {})
        if mapping is not None:
            target.update({str(k): str(v) for k, v in mapping.items()})
            return len(mapping)
        target[str(field)] = str(value)
        return 1

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hdel(self, key, *fields):
        target = self.hashes.setdefault(key, {})
        deleted = 0
        for field in fields:
            if field in target:
                deleted += 1
                del target[field]
        return deleted

    def hincrby(self, key, field, amount=1):
        target = self.hashes.setdefault(key, {})
        target[field] = str(int(target.get(field, 0)) + amount)
        return int(target[field])

    def zadd(self, key, mapping):
        target = self.zsets.setdefault(key, {})
        target.update(mapping)
        return len(mapping)

    def zscore(self, key, member):
        return self.zsets.get(key, {}).get(member)

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def zrange(self, key, start, end):
        ordered = [
            member
            for member, _ in sorted(
                self.zsets.get(key, {}).items(),
                key=lambda item: (item[1], item[0]),
            )
        ]
        if end == -1:
            end = len(ordered) - 1
        return ordered[start:end + 1]

    def zrem(self, key, *members):
        target = self.zsets.setdefault(key, {})
        removed = 0
        for member in members:
            if member in target:
                removed += 1
                del target[member]
        return removed

    def expire(self, key, ttl):
        self.expire_calls.append((key, ttl))
        return True

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def ltrim(self, key, start, end):
        values = self.lists.setdefault(key, [])
        if start < 0:
            start = max(len(values) + start, 0)
        if end < 0:
            end = len(values) + end
        self.lists[key] = values[start:end + 1]
        return True

    def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        if end == -1:
            end = len(values) - 1
        return list(values[start:end + 1])

    def delete(self, *keys):
        self.deleted_keys.extend(keys)
        for key in keys:
            self.hashes.pop(key, None)
            self.zsets.pop(key, None)
            self.lists.pop(key, None)
        return len(keys)


@pytest.fixture
def fake_redis(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(wm, "_read_client", client)
    monkeypatch.setattr(wm, "_write_client", client)
    monkeypatch.setattr(wm, "_get_read_client", lambda: client)
    monkeypatch.setattr(wm, "_get_write_client", lambda: client)
    monkeypatch.setattr(wm, "WORKING_MEMORY_TTL_SECONDS", 60)
    monkeypatch.setattr(wm, "WORKING_MEMORY_KV_MAX_ENTRIES", 2)
    monkeypatch.setattr(wm, "WORKING_MEMORY_KV_VALUE_MAX_CHARS", 5)
    monkeypatch.setattr(wm, "WORKING_MEMORY_MAX_MESSAGES", 3)
    monkeypatch.setattr(wm, "WORKING_MEMORY_SNAPSHOT_EVERY_N_TURNS", 2)
    return client


def test_set_get_delete_kv_and_refreshes_ttl(fake_redis):
    result = wm.set_kv_sync("t", "u", "c", "task", "report", "agent-1")

    assert result["previous"] is None
    assert result["current"] == "repor"
    assert result["truncated"] is True
    assert result["kv"] == {"task": "repor"}
    assert wm._sync_get_kv("t", "u", "c") == {"task": "repor"}
    assert fake_redis.hashes["wm:t:u:c:meta"]["last_writer_agent_id"] == "agent-1"
    assert ("wm:t:u:c:kv", 60) in fake_redis.expire_calls

    delete_result = wm.delete_kv_sync("t", "u", "c", "task", "agent-2")

    assert delete_result == {"key": "task", "deleted": True, "kv": {}}
    assert fake_redis.hashes["wm:t:u:c:meta"]["last_writer_agent_id"] == "agent-2"


def test_fifo_eviction_removes_oldest_key(fake_redis):
    wm.set_kv_sync("t", "u", "c", "first", "1", "agent")
    wm.set_kv_sync("t", "u", "c", "second", "2", "agent")
    result = wm.set_kv_sync("t", "u", "c", "third", "3", "agent")

    assert result["evicted_key"] == "first"
    assert result["kv"] == {"second": "2", "third": "3"}
    assert fake_redis.hget("wm:t:u:c:kv", "first") is None
    assert "first" not in fake_redis.zsets["wm:t:u:c:kv_order"]


def test_existing_key_update_does_not_change_fifo_position(fake_redis):
    wm.set_kv_sync("t", "u", "c", "first", "1", "agent")
    wm.set_kv_sync("t", "u", "c", "second", "2", "agent")
    wm.set_kv_sync("t", "u", "c", "first", "updated", "agent")
    result = wm.set_kv_sync("t", "u", "c", "third", "3", "agent")

    assert result["evicted_key"] == "first"
    assert result["kv"] == {"second": "2", "third": "3"}


def test_append_message_caps_window_and_counts_assistant_turns(fake_redis):
    for idx, role in enumerate(["user", "assistant", "user", "assistant"]):
        wm.append_message("t", "u", "c", role, f"m{idx}", "agent")

    raw_msgs = fake_redis.lrange("wm:t:u:c:msgs", 0, -1)
    msgs = [json.loads(item) for item in raw_msgs]

    assert [msg["content"] for msg in msgs] == ["m1", "m2", "m3"]
    assert fake_redis.hashes["wm:t:u:c:meta"]["turn_count"] == "2"
    assert ("wm:t:u:c:msgs", 60) in fake_redis.expire_calls


def test_maybe_snapshot_only_runs_on_configured_turn_interval(fake_redis, monkeypatch):
    calls = []
    monkeypatch.setattr(
        wm,
        "snapshot_to_minio",
        lambda tenant_id, user_id, conversation_id: calls.append(
            (tenant_id, user_id, conversation_id)
        ) or "object-key",
    )

    wm.append_message("t", "u", "c", "assistant", "a1", "agent")
    assert wm.maybe_snapshot("t", "u", "c") is None

    wm.append_message("t", "u", "c", "assistant", "a2", "agent")
    assert wm.maybe_snapshot("t", "u", "c") == "object-key"
    assert calls == [("t", "u", "c")]


def test_snapshot_to_minio_uploads_expected_json(fake_redis, monkeypatch):
    uploaded = {}

    def fake_upload(path, object_name, bucket):
        with open(path, encoding="utf-8") as fp:
            uploaded["payload"] = json.load(fp)
        uploaded["object_name"] = object_name
        uploaded["bucket"] = bucket
        return True, "ok"

    monkeypatch.setattr(wm.minio_client, "upload_file", fake_upload)
    monkeypatch.setattr(wm, "MINIO_DEFAULT_BUCKET", "bucket")
    wm.set_kv_sync("t", "u", "c", "task", "report", "agent")
    wm.append_message("t", "u", "c", "user", "hello", "agent")

    object_key = wm.snapshot_to_minio("t", "u", "c")

    assert object_key.startswith("working_memory/t/u/c/")
    assert uploaded["object_name"] == object_key
    assert uploaded["bucket"] == "bucket"
    assert uploaded["payload"]["schema_version"] == 1
    assert uploaded["payload"]["tenant_id"] == "t"
    assert uploaded["payload"]["user_id"] == "u"
    assert uploaded["payload"]["conversation_id"] == "c"
    assert uploaded["payload"]["kv"] == {"task": "repor"}
    assert uploaded["payload"]["msgs"][0]["content"] == "hello"
    assert "last_snapshot_ts" in fake_redis.hashes["wm:t:u:c:meta"]


def test_snapshot_to_minio_failure_is_best_effort(fake_redis, monkeypatch):
    monkeypatch.setattr(
        wm.minio_client,
        "upload_file",
        lambda *args, **kwargs: (False, "minio failed"),
    )

    assert wm.snapshot_to_minio("t", "u", "c") == ""


def test_finalize_snapshots_then_deletes_all_keys(fake_redis, monkeypatch):
    wm.set_kv_sync("t", "u", "c", "task", "report", "agent")
    calls = []
    monkeypatch.setattr(
        wm,
        "snapshot_to_minio",
        lambda tenant_id, user_id, conversation_id: calls.append(
            (tenant_id, user_id, conversation_id)
        ) or "object-key",
    )

    wm.finalize("t", "u", "c")

    assert calls == [("t", "u", "c")]
    assert set(fake_redis.deleted_keys) == {
        "wm:t:u:c:kv",
        "wm:t:u:c:msgs",
        "wm:t:u:c:meta",
        "wm:t:u:c:kv_order",
    }


@pytest.mark.asyncio
async def test_get_kv_returns_empty_on_sync_failure(monkeypatch):
    monkeypatch.setattr(
        wm,
        "_sync_get_kv",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("redis down")),
    )

    assert await wm.get_kv("t", "u", "c") == {}


def test_write_errors_raise_working_memory_error(monkeypatch):
    class BrokenRedis:
        def hget(self, *args, **kwargs):
            raise RuntimeError("redis down")

    monkeypatch.setattr(wm, "_get_write_client", lambda: BrokenRedis())

    with pytest.raises(WorkingMemoryError, match="redis down"):
        wm.set_kv_sync("t", "u", "c", "task", "report", "agent")
