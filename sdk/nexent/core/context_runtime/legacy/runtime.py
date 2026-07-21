"""Legacy context path: Jinja prompt plus the original AgentMemory assembly."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

from ..contracts import ContextEvidence, FinalContext


LEGACY_MAX_OBSERVATION_LENGTH = 100_000


def _normalize(
    value: Any,
    _active_ids: set[int] | None = None,
    _depth: int = 0,
) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    class_name = f"{value.__class__.__module__}.{value.__class__.__qualname__}"
    if _depth >= 32:
        return {"__max_depth__": class_name}

    active_ids = _active_ids if _active_ids is not None else set()
    value_id = id(value)
    if value_id in active_ids:
        return {"__cycle__": class_name}
    active_ids.add(value_id)
    try:
        if isinstance(value, dict):
            return {
                str(key): _normalize(item, active_ids, _depth + 1)
                for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            }
        if isinstance(value, (list, tuple)):
            return [
                _normalize(item, active_ids, _depth + 1)
                for item in value
            ]
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return _normalize(model_dump(), active_ids, _depth + 1)
        enum_value = getattr(value, "value", None)
        if isinstance(enum_value, (str, int, float, bool)):
            return enum_value
        if hasattr(value, "__dict__"):
            return _normalize({
                key: item
                for key, item in vars(value).items()
                if not key.startswith("_")
            }, active_ids, _depth + 1)
        return {"__class__": class_name}
    finally:
        active_ids.remove(value_id)


def _fingerprint(value: Any) -> str:
    try:
        normalized = _normalize(value)
    except Exception as error:
        normalized = {
            "__normalization_error__": type(error).__name__,
            "__class__": f"{value.__class__.__module__}.{value.__class__.__qualname__}",
        }
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _message_role(message: Any) -> str:
    role = message.get("role") if isinstance(message, dict) else getattr(message, "role", "")
    return str(getattr(role, "value", role))


def _message_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def _evidence(messages: list[Any], tools: list[Any], purpose: str) -> ContextEvidence:
    message_tokens = int(sum(len(_message_text(message)) for message in messages) / 1.5)
    system_messages = [message for message in messages if _message_role(message) == "system"]
    history_messages = [message for message in messages if _message_role(message) != "system"]
    return ContextEvidence(
        purpose=purpose,
        dynamic_message_count=len(messages),
        messages_fingerprint=_fingerprint(messages),
        tools_fingerprint=_fingerprint(tools),
        system_messages_fingerprint=_fingerprint(system_messages),
        history_messages_fingerprint=_fingerprint(history_messages),
        final_answer_prompt_fingerprint=(
            _fingerprint([messages[0], messages[-1]]) if purpose == "final_answer" and messages else None
        ),
        message_roles=tuple(_message_role(message) for message in messages),
        history_message_roles=tuple(_message_role(message) for message in history_messages),
        pre_compression_tokens=message_tokens,
        post_compression_tokens=message_tokens,
        soft_budget_tokens=0,
        hard_budget_tokens=0,
        history_budget_tokens=0,
        soft_budget_exceeded=False,
        hard_budget_exceeded=False,
        compression_attempted=False,
        fallback_compaction_used=False,
        observation_truncated=any("Output truncated to " in _message_text(message) for message in messages),
    )


class LegacyContextRuntime:
    """Fallback path deliberately independent from ContextManager and W3."""

    context_manager = None

    def prepare_run(self, *, memory: Any, fallback_system_prompt: str) -> None:
        from smolagents.memory import SystemPromptStep

        memory.system_prompt = SystemPromptStep(system_prompt=fallback_system_prompt)

    def prepare_step(
        self,
        *,
        model: Any,
        memory: Any,
        current_run_start_idx: int,
        tools: Sequence[Any] | None = None,
    ) -> FinalContext:
        del model, current_run_start_idx
        messages = self._messages_from_memory(memory)
        canonical_tools = list(tools or ())
        return FinalContext(
            messages=messages,
            tools=canonical_tools,
            evidence=_evidence(messages, canonical_tools, "step"),
        )

    def prepare_final_answer(
        self,
        *,
        model: Any,
        memory: Any,
        current_run_start_idx: int,
        task: str,
        final_answer_templates: dict,
        tools: Sequence[Any] | None = None,
    ) -> FinalContext:
        del model, current_run_start_idx
        from jinja2 import StrictUndefined, Template
        from smolagents.models import ChatMessage, MessageRole

        memory_messages = self._messages_from_memory(memory)
        final_answer = final_answer_templates["final_answer"]
        messages = [
            ChatMessage(
                role=MessageRole.SYSTEM,
                content=[{"type": "text", "text": final_answer["pre_messages"]}],
            )
        ]
        messages += memory_messages[1:]
        messages.append(
            ChatMessage(
                role=MessageRole.USER,
                content=[{
                    "type": "text",
                    "text": Template(
                        final_answer["post_messages"],
                        undefined=StrictUndefined,
                    ).render(task=task),
                }],
            )
        )
        canonical_tools = list(tools or ())
        return FinalContext(
            messages=messages,
            tools=canonical_tools,
            evidence=_evidence(messages, canonical_tools, "final_answer"),
        )

    def truncate_observation(self, memory_step: Any) -> None:
        observation = getattr(memory_step, "observations", None)
        if not observation or len(observation) <= LEGACY_MAX_OBSERVATION_LENGTH:
            return
        half = LEGACY_MAX_OBSERVATION_LENGTH // 2
        marker = (
            f"\n...[Output truncated to {LEGACY_MAX_OBSERVATION_LENGTH} characters by legacy context runtime. "
            "Enable ContextManager for budget-aware compression.]\n"
        )
        memory_step.observations = observation[:half] + marker + observation[-half:]

    @staticmethod
    def _messages_from_memory(memory: Any) -> list[Any]:
        messages: list[Any] = []
        if memory.system_prompt:
            messages.extend(memory.system_prompt.to_messages())
        for step in memory.steps:
            messages.extend(step.to_messages())
        return messages

    def render_summary_messages(self, *, memory: Any) -> list[Any]:
        """Return display-only memory messages without compression side effects."""
        return self._messages_from_memory(memory)

    def compression_stats(self) -> dict:
        return {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_hits": 0,
            "cache_types": [],
        }

    @property
    def chars_per_token(self) -> float:
        return 1.5

    @property
    def token_threshold(self) -> int | None:
        return None
