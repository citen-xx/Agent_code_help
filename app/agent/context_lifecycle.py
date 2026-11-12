"""Deterministic model-context compaction without mutating Agent state."""

import json
import re

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

MAX_RETAINED_EXPLORATION_RESULTS = 6
MAX_RETAINED_TEST_RESULTS = 1
MAX_RETAINED_PLAN_RESULTS = 1

EXPLORATION_TOOLS = {
    "list_symbols",
    "find_symbol",
    "find_references",
    "find_callers",
    "find_callees",
    "analyze_impact",
    "read_file",
    "search_code",
    "list_files",
    "git_show",
    "git_log",
    "git_status",
    "git_diff",
}


def _call_names(messages: list[BaseMessage]) -> dict[str, str]:
    names = {}
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            call_id = str(call.get("id") or "")
            if call_id:
                names[call_id] = str(call.get("name") or "unknown")
    return names


def _is_successful_result(message: ToolMessage) -> bool:
    content = str(message.content)
    return not content.startswith(
        ("[tool error]", "[planner required]", "[auto test]")
    )


def _placeholder(tool_name: str, raw_chars: int) -> str:
    return json.dumps(
        {
            "compacted": True,
            "tool": tool_name,
            "original_chars": raw_chars,
            "message": (
                "Earlier successful result compacted. Call the tool again if "
                "exact detail is needed."
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _compact_test_content(content: str) -> str:
    try:
        body = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        body = None
    if not isinstance(body, dict):
        return json.dumps(
            {
                "compacted": True,
                "tool": "run_tests",
                "original_chars": len(content),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    stdout = str(body.get("stdout") or "")
    stderr = str(body.get("stderr") or "")
    relevant = []
    markers = (
        "[ERROR]",
        "BUILD FAILURE",
        "Tests run:",
        "AssertionError",
        "Exception",
        "\tat ",
    )
    for line in f"{stderr}\n{stdout}".splitlines():
        stripped = line.strip()
        if stripped and any(marker in line for marker in markers):
            relevant.append(stripped)
    summary = "\n".join(relevant)[:3000]
    failed_tests = sorted(
        set(
            re.findall(
                r"(?:Tests run:.*?-\s*in\s+|<<< FAILURE!\s*-\s*in\s+)"
                r"([A-Za-z0-9_.$#-]+)",
                stdout + "\n" + stderr,
            )
        )
    )
    return json.dumps(
        {
            "compacted": True,
            "tool": "run_tests",
            "success": body.get("success"),
            "exit_code": body.get("exit_code"),
            "timed_out": body.get("timed_out"),
            "error": body.get("error"),
            "failed_tests": failed_tests,
            "failure_summary": summary,
            "duration_seconds": body.get("duration_seconds"),
            "original_chars": len(content),
        },
        ensure_ascii=False,
        indent=2,
    )


def compact_model_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Return the model-visible history after deterministic lifecycle compaction."""
    call_names = _call_names(messages)
    exploration_indexes = []
    test_indexes = []
    plan_indexes = []
    for index, message in enumerate(messages):
        if not isinstance(message, ToolMessage):
            continue
        tool_name = call_names.get(str(message.tool_call_id), "")
        if not _is_successful_result(message):
            continue
        if tool_name in EXPLORATION_TOOLS:
            exploration_indexes.append(index)
        elif tool_name == "run_tests":
            test_indexes.append(index)
        elif tool_name == "create_repair_plan":
            plan_indexes.append(index)

    compact_indexes = set(
        exploration_indexes[:-MAX_RETAINED_EXPLORATION_RESULTS]
        + test_indexes[:-MAX_RETAINED_TEST_RESULTS]
        + plan_indexes[:-MAX_RETAINED_PLAN_RESULTS]
    )

    plan_call_ids = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            if call.get("name") == "create_repair_plan":
                plan_call_ids.append(str(call.get("id") or ""))
    active_plan_call_ids = set(plan_call_ids[-MAX_RETAINED_PLAN_RESULTS:])

    result: list[BaseMessage] = []
    for index, message in enumerate(messages):
        if isinstance(message, ToolMessage):
            tool_name = call_names.get(str(message.tool_call_id), "unknown")
            if index in compact_indexes:
                content = (
                    _compact_test_content(str(message.content))
                    if tool_name == "run_tests"
                    else _placeholder(tool_name, len(str(message.content)))
                )
                result.append(message.model_copy(update={"content": content}))
                continue
        elif isinstance(message, AIMessage):
            compacted_calls = []
            changed = False
            for call in message.tool_calls:
                call_id = str(call.get("id") or "")
                if (
                    call.get("name") == "create_repair_plan"
                    and call_id
                    and call_id not in active_plan_call_ids
                ):
                    compacted_calls.append(
                        {
                            **call,
                            "args": {
                                "compacted": True,
                                "revision_reason": (
                                    (call.get("args") or {}).get(
                                        "revision_reason"
                                    )
                                    or "superseded plan"
                                ),
                            },
                        }
                    )
                    changed = True
                else:
                    compacted_calls.append(call)
            if changed:
                result.append(
                    message.model_copy(
                        update={"tool_calls": compacted_calls}
                    )
                )
                continue
        result.append(message)
    return result
