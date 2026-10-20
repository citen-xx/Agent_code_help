"""Evaluation-only LLM usage tracking wrapper."""

from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from app.evaluation.models import LLMUsage


@dataclass
class UsageTracker:
    llm_calls: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

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
        return UsageTrackingChatModel(
            delegate=self.delegate.bind_tools(tools, **kwargs),
            tracker=self.tracker,
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
        return ChatResult(generations=[ChatGeneration(message=message)])
