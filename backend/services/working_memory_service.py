import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional

import redis

from consts.const import (
    MINIO_DEFAULT_BUCKET,
    REDIS_URL,
    WORKING_MEMORY_KV_MAX_ENTRIES,
    WORKING_MEMORY_KV_VALUE_MAX_CHARS,
    WORKING_MEMORY_MAX_MESSAGES,
    WORKING_MEMORY_READ_TIMEOUT_MS,
    WORKING_MEMORY_SNAPSHOT_EVERY_N_TURNS,
    WORKING_MEMORY_TTL_SECONDS,
    WORKING_MEMORY_WRITE_TIMEOUT_MS,
)
from consts.exceptions import WorkingMemoryError
from database.client import minio_client

logger = logging.getLogger("working_memory_service")

_read_client: Optional[redis.Redis] = None
_write_client: Optional[redis.Redis] = None


def _redis_client(timeout_ms: int) -> redis.Redis:
    if not REDIS_URL:
        raise WorkingMemoryError("REDIS_URL environment variable is not set")
    timeout_s = max(timeout_ms / 1000, 0.001)
    return redis.from_url(
        REDIS_URL,
        socket_timeout=timeout_s,
        socket_connect_timeout=timeout_s,
        decode_responses=True,
    )


def _get_read_client() -> redis.Redis:
    global _read_client
    if _read_client is None:
        _read_client = _redis_client(WORKING_MEMORY_READ_TIMEOUT_MS)
    return _read_client


def _get_write_client() -> redis.Redis:
    global _write_client
    if _write_client is None:
        _write_client = _redis_client(WORKING_MEMORY_WRITE_TIMEOUT_MS)
    return _write_client


def _prefix(tenant_id: str, user_id: str, conversation_id: str) -> str:
    return f"wm:{tenant_id}:{user_id}:{conversation_id}"


def _keys(tenant_id: str, user_id: str, conversation_id: str) -> Dict[str, str]:
    prefix = _prefix(tenant_id, user_id, conversation_id)
    return {
        "kv": f"{prefix}:kv",
        "msgs": f"{prefix}:msgs",
        "meta": f"{prefix}:meta",
        "order": f"{prefix}:kv_order",
    }


def _refresh_ttl(client: redis.Redis, keys: Dict[str, str]) -> None:
    pipe = client.pipeline()
    for key in keys.values():
        pipe.expire(key, WORKING_MEMORY_TTL_SECONDS)
    pipe.execute()


def _ordered_kv(client: redis.Redis, keys: Dict[str, str]) -> Dict[str, str]:
    names = client.zrange(keys["order"], 0, -1)
    kv = client.hgetall(keys["kv"]) or {}
    if not names:
        return dict(kv)
    ordered = {name: kv[name] for name in names if name in kv}
    for name, value in kv.items():
        if name not in ordered:
            ordered[name] = value
    return ordered


def _sync_get_kv(tenant_id: str, user_id: str, conversation_id: str) -> Dict[str, str]:
    client = _get_read_client()
    keys = _keys(tenant_id, user_id, conversation_id)
    return _ordered_kv(client, keys)


async def get_kv(tenant_id: str, user_id: str, conversation_id: str) -> Dict[str, str]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_sync_get_kv, tenant_id, user_id, conversation_id),
            timeout=WORKING_MEMORY_READ_TIMEOUT_MS / 1000,
        )
    except Exception as e:
        logger.warning(f"working_memory.get_kv degraded: {e}")
        return {}


def set_kv_sync(
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    key: str,
    value: str,
    agent_id: str,
) -> Dict[str, Any]:
    if not key:
        raise WorkingMemoryError("key must not be empty")
    client = _get_write_client()
    keys = _keys(tenant_id, user_id, conversation_id)
    now = time.time()
    value = "" if value is None else str(value)
    truncated = False
    if len(value) > WORKING_MEMORY_KV_VALUE_MAX_CHARS:
        value = value[:WORKING_MEMORY_KV_VALUE_MAX_CHARS]
        truncated = True

    try:
        previous = client.hget(keys["kv"], key)
        score = client.zscore(keys["order"], key)
        pipe = client.pipeline()
        pipe.hset(keys["kv"], key, value)
        if score is None:
            pipe.zadd(keys["order"], {key: now})
        pipe.hset(keys["meta"], mapping={
            "last_writer_agent_id": agent_id,
            "updated_at": str(now),
        })
        for redis_key in keys.values():
            pipe.expire(redis_key, WORKING_MEMORY_TTL_SECONDS)
        pipe.execute()

        evicted_key = None
        count = client.zcard(keys["order"])
        if count > WORKING_MEMORY_KV_MAX_ENTRIES:
            overflow = count - WORKING_MEMORY_KV_MAX_ENTRIES
            evict_keys = client.zrange(keys["order"], 0, overflow - 1)
            if evict_keys:
                evicted_key = evict_keys[0]
                pipe = client.pipeline()
                pipe.hdel(keys["kv"], *evict_keys)
                pipe.zrem(keys["order"], *evict_keys)
                pipe.execute()

        return {
            "key": key,
            "previous": previous,
            "current": value,
            "truncated": truncated,
            "evicted_key": evicted_key,
            "kv": _ordered_kv(client, keys),
        }
    except Exception as e:
        raise WorkingMemoryError(str(e)) from e


def delete_kv_sync(
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    key: str,
    agent_id: str,
) -> Dict[str, Any]:
    if not key:
        raise WorkingMemoryError("key must not be empty")
    client = _get_write_client()
    keys = _keys(tenant_id, user_id, conversation_id)
    now = time.time()
    try:
        pipe = client.pipeline()
        pipe.hdel(keys["kv"], key)
        pipe.zrem(keys["order"], key)
        pipe.hset(keys["meta"], mapping={
            "last_writer_agent_id": agent_id,
            "updated_at": str(now),
        })
        for redis_key in keys.values():
            pipe.expire(redis_key, WORKING_MEMORY_TTL_SECONDS)
        result = pipe.execute()
        return {
            "key": key,
            "deleted": bool(result[0]),
            "kv": _ordered_kv(client, keys),
        }
    except Exception as e:
        raise WorkingMemoryError(str(e)) from e


def append_message(
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    role: str,
    content: str,
    agent_id: str,
) -> None:
    if not content:
        return
    client = _get_write_client()
    keys = _keys(tenant_id, user_id, conversation_id)
    entry = json.dumps({
        "role": role,
        "content": content,
        "ts": time.time(),
        "agent_id": agent_id,
    }, ensure_ascii=False)
    pipe = client.pipeline()
    pipe.rpush(keys["msgs"], entry)
    pipe.ltrim(keys["msgs"], -WORKING_MEMORY_MAX_MESSAGES, -1)
    pipe.hincrby(keys["meta"], "turn_count", 1 if role == "assistant" else 0)
    for redis_key in keys.values():
        pipe.expire(redis_key, WORKING_MEMORY_TTL_SECONDS)
    pipe.execute()


def maybe_snapshot(tenant_id: str, user_id: str, conversation_id: str) -> Optional[str]:
    client = _get_write_client()
    keys = _keys(tenant_id, user_id, conversation_id)
    try:
        turn_count = int(client.hget(keys["meta"], "turn_count") or 0)
        if not turn_count or turn_count % WORKING_MEMORY_SNAPSHOT_EVERY_N_TURNS != 0:
            return None
        return snapshot_to_minio(tenant_id, user_id, conversation_id)
    except Exception as e:
        logger.warning(f"working_memory.maybe_snapshot failed: {e}")
        return None


def _dump_raw(tenant_id: str, user_id: str, conversation_id: str) -> Dict[str, Any]:
    client = _get_write_client()
    keys = _keys(tenant_id, user_id, conversation_id)
    msgs = []
    for item in client.lrange(keys["msgs"], 0, -1) or []:
        try:
            msgs.append(json.loads(item))
        except Exception:
            msgs.append({"raw": item})
    return {
        "kv": _ordered_kv(client, keys),
        "msgs": msgs,
        "meta": client.hgetall(keys["meta"]) or {},
    }


def snapshot_to_minio(tenant_id: str, user_id: str, conversation_id: str) -> str:
    payload = {
        "schema_version": 1,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        **_dump_raw(tenant_id, user_id, conversation_id),
    }
    object_key = (
        f"working_memory/{tenant_id}/{user_id}/{conversation_id}/"
        f"{int(time.time())}.json"
    )
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
            tmp_path = fp.name
        success, result = minio_client.upload_file(
            tmp_path,
            object_name=object_key,
            bucket=MINIO_DEFAULT_BUCKET,
        )
        if not success:
            raise WorkingMemoryError(str(result))
        client = _get_write_client()
        client.hset(_keys(tenant_id, user_id, conversation_id)["meta"], "last_snapshot_ts", str(time.time()))
        return object_key
    except Exception as e:
        logger.warning(f"working_memory.snapshot_to_minio failed: {e}")
        return ""
    finally:
        try:
            if "tmp_path" in locals():
                os.unlink(tmp_path)
        except Exception:
            pass


def finalize(tenant_id: str, user_id: str, conversation_id: str) -> None:
    try:
        snapshot_to_minio(tenant_id, user_id, conversation_id)
    except Exception:
        pass
    client = _get_write_client()
    keys = _keys(tenant_id, user_id, conversation_id)
    client.delete(*keys.values())


def dump(tenant_id: str, user_id: str, conversation_id: str) -> Dict[str, Any]:
    try:
        return _dump_raw(tenant_id, user_id, conversation_id)
    except Exception as e:
        raise WorkingMemoryError(str(e)) from e
