"""Fake LLM for tests — never touches the network or any real API."""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


def tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    """Build an AIMessage that requests a single tool call."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


class FakeChatModel(BaseChatModel):
    """Scripted chat model: pops one queued AIMessage per call and records
    every message list it receives (for asserting on tool feedback)."""

    script: list[BaseMessage] = Field(default_factory=list)
    error: Exception | None = None
    calls: int = 0
    history: list[list[BaseMessage]] = Field(default_factory=list)
    bound_tool_names: list[list[str]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.bound_tool_names.append([tool.name for tool in tools])
        return self

    @property
    def _llm_type(self) -> str:
        return "fake-chat-model"

    @property
    def _identifying_params(self) -> dict:
        return {"fake": True}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        self.history.append(list(messages))
        if self.error is not None:
            raise self.error
        if not self.script:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])
