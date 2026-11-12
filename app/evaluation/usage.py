"""Evaluation-only LLM usage tracking wrapper."""

import json
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from app.evaluation.models import (
    ContextCallTelemetry,
    ContextTelemetry,
    LLMUsage,
    MessageLifecycleTelemetry,
    RawToolOutputTelemetry,
)

CI_TOOL_NAMES = {
    "list_symbols",
    "find_symbol",
    "find_references",
    "find_callers",
    "find_callees",
    "analyze_impact",
}


def _content_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    return len(json.dumps(content, ensure_ascii=False, sort_keys=True, default=str))


def _tool_schema_chars(tools: list) -> int:
    payload = []
    for tool in tools:
        schema = getattr(tool, "args_schema", None)
        if schema is not None and hasattr(schema, "model_json_schema"):
            schema = schema.model_json_schema()
        payload.append(
            {
                "name": getattr(tool, "name", type(tool).__name__),
                "description": getattr(tool, "description", ""),
                "args_schema": schema,
            }
        )
    return len(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    )


@dataclass
class UsageTracker:
    llm_calls: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    task_id: str = "unknown"
    profile: str = "unknown"
    repair_round: int = 0
    tool_schema_chars: int = 0
    context_calls: list[ContextCallTelemetry] = field(default_factory=list)
    message_states: dict[int, MessageLifecycleTelemetry] = field(
        default_factory=dict
    )
    raw_test_outputs: list[RawToolOutputTelemetry] = field(default_factory=list)
    raw_test_keys: set[int] = field(default_factory=set)

    def set_tool_schema_chars(self, value: int) -> None:
        self.tool_schema_chars = value

    def set_repair_round(self, value: int) -> None:
        self.repair_round = value

    def add_usage(
        self,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
    ) -> None:
        if prompt_tokens is not None:
            self.prompt_tokens = (self.prompt_tokens or 0) + prompt_tokens
        if completion_tokens is not None:
            self.completion_tokens = (self.completion_tokens or 0) + completion_tokens
        if total_tokens is not None:
            self.total_tokens = (self.total_tokens or 0) + total_tokens
        elif prompt_tokens is not None or completion_tokens is not None:
            self.total_tokens = (self.total_tokens or 0) + (
                prompt_tokens or 0
            ) + (completion_tokens or 0)

    def snapshot(self) -> LLMUsage:
        return LLMUsage(
            llm_calls=self.llm_calls,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.total_tokens,
            estimated_cost_usd=None,
        )

    def snapshot_context(self) -> ContextTelemetry:
        return ContextTelemetry(
            calls=[
                call.model_copy(deep=True)
                for call in self.context_calls
            ],
            messages=[
                state.model_copy(deep=True)
                for state in self.message_states.values()
            ],
            raw_test_outputs=[
                output.model_copy(deep=True)
                for output in self.raw_test_outputs
            ],
        )

    def capture_raw_test_outputs(
        self,
        messages: list,
        *,
        call_index: int,
    ) -> None:
        calls_by_id = {}
        for message in messages:
            if not isinstance(message, AIMessage):
                continue
            for call in message.tool_calls:
                call_id = str(call.get("id") or "")
                if call_id:
                    calls_by_id[call_id] = str(call.get("name") or "unknown")
        for message in messages:
            if not isinstance(message, ToolMessage):
                continue
            if calls_by_id.get(str(message.tool_call_id)) != "run_tests":
                continue
            if id(message) in self.raw_test_keys:
                continue
            self.raw_test_keys.add(id(message))
            self.raw_test_outputs.append(
                RawToolOutputTelemetry(
                    call_index=call_index,
                    tool_name="run_tests",
                    content=str(message.content),
                )
            )

    def record_call(
        self,
        messages: list,
        *,
        call_index: int,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
    ) -> None:
        categories = {
            "system_message_chars": 0,
            "user_message_chars": 0,
            "assistant_message_chars": 0,
            "tool_message_chars": 0,
            "tool_call_chars": 0,
            "ci_tool_chars": 0,
            "read_file_chars": 0,
            "test_output_chars": 0,
            "patch_output_chars": 0,
            "planner_chars": 0,
            "other_chars": 0,
        }
        calls_by_id: dict[str, str] = {}
        for message in messages:
            if isinstance(message, AIMessage):
                for call in getattr(message, "tool_calls", None) or []:
                    call_id = str(call.get("id") or "")
                    if call_id:
                        calls_by_id[call_id] = str(call.get("name") or "unknown")

        for message in messages:
            content_chars = _content_chars(message.content)
            tool_call_chars = 0
            source = "other"
            role = "other"
            if isinstance(message, SystemMessage):
                categories["system_message_chars"] += content_chars
                source = "system"
                role = "system"
            elif isinstance(message, HumanMessage):
                categories["user_message_chars"] += content_chars
                source = "user_task"
                role = "user"
            elif isinstance(message, AIMessage):
                categories["assistant_message_chars"] += content_chars
                for call in getattr(message, "tool_calls", None) or []:
                    tool_call_chars += _content_chars(
                        {
                            "name": call.get("name"),
                            "args": call.get("args") or {},
                        }
                    )
                categories["tool_call_chars"] += tool_call_chars
                source = "assistant_tool_call" if tool_call_chars else "assistant"
                role = "assistant"
            elif isinstance(message, ToolMessage):
                categories["tool_message_chars"] += content_chars
                tool_name = calls_by_id.get(
                    str(message.tool_call_id),
                    "unknown",
                )
                source = tool_name
                role = "tool"
                if tool_name in CI_TOOL_NAMES:
                    categories["ci_tool_chars"] += content_chars
                elif tool_name == "read_file":
                    categories["read_file_chars"] += content_chars
                elif tool_name == "run_tests":
                    categories["test_output_chars"] += content_chars
                    if id(message) not in self.raw_test_keys:
                        self.raw_test_keys.add(id(message))
                        self.raw_test_outputs.append(
                            RawToolOutputTelemetry(
                                call_index=call_index,
                                tool_name=tool_name,
                                content=str(message.content),
                            )
                        )
                elif tool_name == "apply_patch":
                    categories["patch_output_chars"] += content_chars
                elif tool_name == "create_repair_plan":
                    categories["planner_chars"] += content_chars
                else:
                    categories["other_chars"] += content_chars
            else:
                categories["other_chars"] += content_chars

            raw_chars = content_chars + tool_call_chars
            key = id(message)
            state = self.message_states.get(key)
            if state is None:
                state = MessageLifecycleTelemetry(
                    message_key=str(key),
                    role=role,
                    source=source,
                    raw_chars=raw_chars,
                    introduced_call_index=call_index,
                )
                self.message_states[key] = state
            state.retained_call_count += 1
            state.context_exposure_chars = (
                state.raw_chars * state.retained_call_count
            )

        primary_total = sum(
            categories[key]
            for key in (
                "system_message_chars",
                "user_message_chars",
                "assistant_message_chars",
                "tool_call_chars",
                "ci_tool_chars",
                "read_file_chars",
                "test_output_chars",
                "patch_output_chars",
                "planner_chars",
                "other_chars",
            )
        )
        self.context_calls.append(
            ContextCallTelemetry(
                call_index=call_index,
                task_id=self.task_id,
                profile=self.profile,
                repair_round=self.repair_round,
                message_count=len(messages),
                provider_prompt_tokens=prompt_tokens,
                provider_completion_tokens=completion_tokens,
                provider_total_tokens=total_tokens,
                tool_schema_chars=self.tool_schema_chars,
                total_history_chars=primary_total,
                **categories,
            )
        )


class UsageTrackingChatModel(BaseChatModel):
    """Wrap a chat model without changing Agent prompts or tools."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    delegate: Any
    tracker: UsageTracker = Field(exclude=True)

    @property
    def _llm_type(self) -> str:
        return f"tracked-{self.delegate._llm_type}"

    @property
    def _identifying_params(self) -> dict:
        return self.delegate._identifying_params

    def bind_tools(self, tools, **kwargs):
        self.tracker.set_tool_schema_chars(_tool_schema_chars(list(tools)))
        return UsageTrackingChatModel(
            delegate=self.delegate.bind_tools(tools, **kwargs),
            tracker=self.tracker,
        )

    def set_context_metadata(
        self,
        *,
        repair_round: int,
        profile: str | None = None,
        raw_messages: list | None = None,
    ) -> None:
        self.tracker.set_repair_round(repair_round)
        if profile is not None:
            self.tracker.profile = profile
        if raw_messages is not None:
            self.tracker.capture_raw_test_outputs(
                raw_messages,
                call_index=self.tracker.llm_calls + 1,
            )

    def _generate(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        self.tracker.llm_calls += 1
        message = self.delegate.invoke(messages, stop=stop, **kwargs)
        usage = getattr(message, "usage_metadata", None)
        prompt_tokens = None
        completion_tokens = None
        total_tokens = None
        if usage:
            prompt_tokens = usage.get("input_tokens")
            completion_tokens = usage.get("output_tokens")
            total_tokens = usage.get("total_tokens")
        else:
            token_usage = getattr(message, "response_metadata", {}).get(
                "token_usage", {}
            )
            prompt_tokens = token_usage.get("prompt_tokens")
            completion_tokens = token_usage.get("completion_tokens")
            total_tokens = token_usage.get("total_tokens")
        self.tracker.add_usage(prompt_tokens, completion_tokens, total_tokens)
        self.tracker.record_call(
            list(messages),
            call_index=self.tracker.llm_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
        return ChatResult(generations=[ChatGeneration(message=message)])
