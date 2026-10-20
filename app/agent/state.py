"""LangGraph state definition for the Java code agent."""

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage


class AgentState(TypedDict):
    """State shared across agent graph nodes.

    Attributes:
        task: The coding/analysis task given by the user.
        messages: Chat history (LLM + tool messages, appended via reducer).
        current_step: Label of the current stage ("init" | "thinking" |
            "tools" | "finished" | "error").
        step_count: Number of LLM calls executed so far.
        final_answer: The agent's final response.
    """

    task: str
    messages: Annotated[list[BaseMessage], operator.add]
    current_step: str
    step_count: int
    final_answer: str
