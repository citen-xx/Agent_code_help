"""Tests for Phase 4.5 context accounting and lifecycle exposure."""

import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.context_lifecycle import (
    MAX_RETAINED_EXPLORATION_RESULTS,
    compact_model_messages,
)
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


def test_old_exploration_results_are_compacted_and_recent_source_is_kept():
    messages = []
    for index in range(MAX_RETAINED_EXPLORATION_RESULTS + 2):
        call_id = f"read-{index}"
        messages.extend(
            [
                _tool_call(call_id, "read_file", {"path": f"{index}.java"}),
                ToolMessage(content=f"source-{index}", tool_call_id=call_id),
            ]
        )
    compacted = compact_model_messages(messages)
    tool_messages = [
        message for message in compacted if isinstance(message, ToolMessage)
    ]
    assert json.loads(tool_messages[0].content)["compacted"] is True
    assert json.loads(tool_messages[1].content)["compacted"] is True
    assert tool_messages[2].content == "source-2"
    assert tool_messages[-1].content == f"source-{MAX_RETAINED_EXPLORATION_RESULTS + 1}"


def test_only_latest_test_result_remains_verbatim():
    first = json.dumps(
        {
            "tool": "maven",
            "command": ["mvn", "test"],
            "exit_code": 1,
            "stdout": "[ERROR] Tests run: 1, Failures: 1\n" + "info-" * 2000,
            "stderr": "",
            "success": False,
            "timed_out": False,
            "truncated": False,
            "error": None,
            "duration_seconds": 1.0,
        }
    )
    second = first.replace("Tests run: 1", "Tests run: 2")
    messages = [
        _tool_call("test-1", "run_tests", {"tool": "maven"}),
        ToolMessage(content=first, tool_call_id="test-1"),
        _tool_call("test-2", "run_tests", {"tool": "maven"}),
        ToolMessage(content=second, tool_call_id="test-2"),
    ]
    compacted = compact_model_messages(messages)
    tool_messages = [
        message for message in compacted if isinstance(message, ToolMessage)
    ]
    old = json.loads(tool_messages[0].content)
    assert old["compacted"] is True
    assert old["failure_summary"].startswith("[ERROR] Tests run:")
    assert tool_messages[1].content == second


def test_superseded_plan_and_tool_call_args_are_compacted():
    first_args = {
        "problem_summary": "old plan",
        "target_symbols": ["Old.run"],
    }
    second_args = {
        "problem_summary": "active plan",
        "target_symbols": ["Active.run"],
    }
    messages = [
        _tool_call("plan-1", "create_repair_plan", first_args),
        ToolMessage(
            content=json.dumps(first_args),
            tool_call_id="plan-1",
        ),
        _tool_call("plan-2", "create_repair_plan", second_args),
        ToolMessage(
            content=json.dumps(second_args),
            tool_call_id="plan-2",
        ),
    ]
    compacted = compact_model_messages(messages)
    assert compacted[0].tool_calls[0]["args"]["compacted"] is True
    assert json.loads(compacted[1].content)["compacted"] is True
    assert compacted[2].tool_calls[0]["args"] == second_args
    assert compacted[3].content == json.dumps(second_args)


def test_raw_test_capture_uses_original_messages_before_compaction():
    tracker = UsageTracker(task_id="task-c", profile="full")
    raw = ToolMessage(content="raw-log", tool_call_id="test-raw")
    tracker.capture_raw_test_outputs(
        [_tool_call("test-raw", "run_tests", {"tool": "maven"}), raw],
        call_index=3,
    )
    context = tracker.snapshot_context()
    assert context.raw_test_outputs[0].content == "raw-log"
