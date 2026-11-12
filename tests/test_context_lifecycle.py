"""Tests for Phase 4.5 context accounting and lifecycle exposure."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.evaluation.usage import UsageTracker


def _tool_call(call_id: str, name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": name, "args": args}],
    )


def test_context_accounting_categories_and_retained_exposure():
    tracker = UsageTracker(task_id="task-a", profile="full")
    system = SystemMessage(content="sys")
    user = HumanMessage(content="task")
    read_call = _tool_call("read-1", "read_file", {"path": "A.java"})
    read_result = ToolMessage(content="source", tool_call_id="read-1")
    test_call = _tool_call("test-1", "run_tests", {"tool": "maven"})
    test_result = ToolMessage(content="raw-test-log", tool_call_id="test-1")
    first = [system, user, read_call, read_result, test_call, test_result]
    tracker.record_call(
        first,
        call_index=1,
        prompt_tokens=100,
        completion_tokens=10,
        total_tokens=110,
    )
    tracker.record_call(
        first,
        call_index=2,
        prompt_tokens=140,
        completion_tokens=12,
        total_tokens=152,
    )

    calls = tracker.snapshot_context().calls
    assert calls[0].system_message_chars == 3
    assert calls[0].user_message_chars == 4
    assert calls[1].read_file_chars == len("source")
    assert calls[1].test_output_chars == len("raw-test-log")
    assert calls[1].tool_message_chars == len("source") + len("raw-test-log")
    assert calls[1].provider_prompt_tokens == 140
    assert calls[1].task_id == "task-a"
    assert calls[1].profile == "full"

    context = tracker.snapshot_context()
    system_state = next(
        message for message in context.messages if message.source == "system"
    )
    assert system_state.introduced_call_index == 1
    assert system_state.retained_call_count == 2
    assert system_state.context_exposure_chars == 6
    assert len(context.raw_test_outputs) == 1
    assert context.raw_test_outputs[0].content == "raw-test-log"


def test_repair_round_and_tool_schema_are_recorded():
    tracker = UsageTracker(task_id="task-b", profile="planner")
    tracker.set_repair_round(2)
    tracker.set_tool_schema_chars(321)
    tracker.record_call(
        [HumanMessage(content="task")],
        call_index=1,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
    )
    call = tracker.snapshot_context().calls[0]
    assert call.repair_round == 2
    assert call.tool_schema_chars == 321
    assert call.provider_prompt_tokens is None
