"""Static Java call graph and impact analysis tests."""

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages import ToolMessage

from app.agent.graph import run_agent
from app.code_intel.parser import parse_java_source
from app.code_intel.registry import clear_all_indexes
from app.code_intel.tools import create_code_intel_tools
from app.tools.patch_tools import create_patch_tools
from tests.fakes import FakeChatModel, tool_call


@pytest.fixture(autouse=True)
def clean_indexes():
    clear_all_indexes()
    yield
    clear_all_indexes()


def _write(repo: Path, relative: str, source: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


CALCULATOR = """package com.shop;

public class Calculator {
    public int add(int left, int right) {
        return left + right;
    }

    public int multiply(int left, int right) {
        return left * right;
    }
}
"""

PRICE_SERVICE = """package com.shop;

public class PriceService {
    private Calculator calculator = new Calculator();

    public int calculate(int price) {
        return calculator.multiply(price, 2);
    }
}
"""

ORDER_SERVICE = """package com.shop;

public class OrderService {
    private PriceService priceService = new PriceService();

    public int create(int price) {
        return priceService.calculate(price);
    }
}
"""

CALCULATOR_TEST = """package com.shop;

public class CalculatorTest {
    public void testMultiply() {
        Calculator calculator = new Calculator();
        calculator.multiply(2, 3);
    }
}
"""

CALL_FORMS = """package com.shop;

class CallForms {
    static void target() {
    }

    static void other() {
    }

    void caller() {
        target();
        this.target();
        other();
    }
}

class StaticCaller {
    void run() {
        CallForms.target();
    }
}

class ObjectCaller {
    void run() {
        CallForms forms = new CallForms();
        forms.target();
    }
}
"""

OVERLOAD = """package com.shop;

class Overloaded {
    void work() {
    }

    void work(int value) {
    }

    void caller() {
        work();
        this.work(1);
    }
}

class WorkA {
    void work() {
    }
}

class WorkB {
    void work() {
    }
}

class AmbiguousCaller {
    void run(WorkA worker) {
        worker.work();
    }
}

class Outer {
    void target() {
    }

    class Inner {
        void innerCall() {
            target();
        }
    }
}
"""


@pytest.fixture
def workspace(manager, origin_repo):
    info = manager.create(origin_repo.as_uri())
    repo = Path(info.repository_path)
    base = "src/main/java/com/shop/"
    _write(repo, base + "Calculator.java", CALCULATOR)
    _write(repo, base + "PriceService.java", PRICE_SERVICE)
    _write(repo, base + "OrderService.java", ORDER_SERVICE)
    _write(repo, base + "CallForms.java", CALL_FORMS)
    _write(repo, base + "Overloaded.java", OVERLOAD)
    _write(repo, "src/test/java/com/shop/CalculatorTest.java", CALCULATOR_TEST)
    return info


def _tools(info, manager):
    return {
        tool.name: tool
        for tool in create_code_intel_tools(
            info.workspace_id,
            info.repository_path,
            manager=manager,
        )
    }


def test_parser_extracts_call_forms():
    parsed = parse_java_source("CallForms.java", CALL_FORMS.encode("utf-8"))
    target_calls = [call for call in parsed.calls if call.callee_name == "target"]
    assert {call.callee_qualifier for call in target_calls} == {
        None,
        "this",
        "CallForms",
        "forms",
    }
    assert {call.containing_class for call in target_calls} == {
        "CallForms",
        "StaticCaller",
        "ObjectCaller",
    }
    assert all(call.line >= 1 and call.column >= 1 for call in target_calls)
    assert all(call.caller_symbol for call in target_calls)


def test_find_callers_and_callees(workspace, manager):
    tools = _tools(workspace, manager)
    callers = json.loads(
        tools["find_callers"].invoke(
            {"method_name": "multiply", "class_name": "Calculator"}
        )
    )
    assert callers["ambiguous"] is False
    assert callers["total"] >= 2
    assert {
        (call["containing_class"], call["containing_method"])
        for call in callers["calls"]
    } >= {("PriceService", "calculate"), ("CalculatorTest", "testMultiply")}

    callees = json.loads(
        tools["find_callees"].invoke(
            {"method_name": "calculate", "class_name": "PriceService"}
        )
    )
    assert callees["total"] == 1
    assert callees["calls"][0]["callee_name"] == "multiply"
    assert callees["calls"][0]["line"] >= 1
    assert callees["calls"][0]["candidates"][0]["name"] == "multiply"


def test_one_method_multiple_callees(workspace, manager):
    tools = _tools(workspace, manager)
    body = json.loads(
        tools["find_callees"].invoke(
            {"method_name": "caller", "class_name": "CallForms"}
        )
    )
    assert body["total"] == 3
    assert {call["callee_name"] for call in body["calls"]} == {"target", "other"}


def test_overloaded_and_ambiguous_calls_are_marked(workspace, manager):
    tools = _tools(workspace, manager)
    overloaded = json.loads(
        tools["find_callers"].invoke(
            {"method_name": "work", "class_name": "Overloaded"}
        )
    )
    assert overloaded["ambiguous"] is True
    assert len(overloaded["targets"]) == 2
    assert overloaded["total"] >= 2

    ambiguous = json.loads(
        tools["find_callers"].invoke(
            {"method_name": "work", "class_name": "WorkA"}
        )
    )
    assert ambiguous["ambiguous"] is True
    ambiguous_calls = [
        call
        for call in ambiguous["calls"]
        if call["containing_class"] == "AmbiguousCaller"
    ]
    assert ambiguous_calls
    assert any(call["ambiguous"] for call in ambiguous_calls)
    assert {candidate["containing_class"] for candidate in ambiguous_calls[0]["candidates"]} >= {
        "WorkA",
        "WorkB",
    }


def test_nested_class_call_is_attributed_to_inner_class(workspace, manager):
    tools = _tools(workspace, manager)
    body = json.loads(
        tools["find_callees"].invoke(
            {"method_name": "innerCall", "class_name": "Outer.Inner"}
        )
    )
    assert body["total"] == 1
    call = body["calls"][0]
    assert call["containing_class"] == "Outer.Inner"
    assert call["containing_method"] == "innerCall"
    assert call["callee_name"] == "target"


def test_analyze_impact_depth_and_test_references(workspace, manager):
    tools = _tools(workspace, manager)
    body = json.loads(
        tools["analyze_impact"].invoke({"name": "multiply", "depth": 2})
    )
    assert body["mode"] == "method"
    assert body["depth"] == 2
    assert body["test_references"]
    assert all(
        "src/test/java" in reference["file_path"]
        for reference in body["test_references"]
    )
    impacted = {
        (
            item["depth"],
            item["call"]["containing_class"],
            item["call"]["containing_method"],
        )
        for item in body["impacted_callers"]
    }
    assert (1, "PriceService", "calculate") in impacted
    assert (2, "OrderService", "create") in impacted
    assert any(item["in_test"] for item in body["impacted_callers"])
    assert any(method["name"] == "add" for method in body["related_methods"])


def test_analyze_impact_accepts_class_name(workspace, manager):
    tools = _tools(workspace, manager)
    body = json.loads(
        tools["analyze_impact"].invoke({"name": "Calculator", "depth": 1})
    )
    assert body["mode"] == "class"
    assert body["targets"][0]["symbol_type"] == "class"
    assert any(
        item["call"]["containing_class"] == "PriceService"
        for item in body["impacted_callers"]
    )
    assert {method["name"] for method in body["related_methods"]} >= {
        "add",
        "multiply",
    }


def test_patch_refreshes_call_graph(workspace, manager):
    tools = _tools(workspace, manager)
    tools["list_symbols"].invoke({})
    before = json.loads(
        tools["find_callees"].invoke(
            {"method_name": "calculate", "class_name": "PriceService"}
        )
    )
    assert before["calls"][0]["callee_name"] == "multiply"

    patch = {
        tool.name: tool
        for tool in create_patch_tools(
            workspace.workspace_id,
            workspace.repository_path,
            manager=manager,
        )
    }["apply_patch"]
    patch.invoke(
        {
            "changes": [
                {
                    "path": "src/main/java/com/shop/PriceService.java",
                    "old_text": "calculator.multiply(price, 2)",
                    "new_text": "calculator.add(price, 2)",
                }
            ]
        }
    )

    after = json.loads(
        tools["find_callees"].invoke(
            {"method_name": "calculate", "class_name": "PriceService"}
        )
    )
    assert after["calls"][0]["callee_name"] == "add"
    callers = json.loads(
        tools["find_callers"].invoke(
            {"method_name": "multiply", "class_name": "Calculator"}
        )
    )
    assert all(
        call["containing_class"] != "PriceService" for call in callers["calls"]
    )


def test_syntax_error_removes_only_file_call_graph(manager, origin_repo):
    info = manager.create(origin_repo.as_uri())
    repo = Path(info.repository_path)
    _write(repo, "Valid.java", "class Valid { void target(){} void caller(){ target(); } }\n")
    _write(repo, "Broken.java", "class Broken { void caller( { target(); }\n")
    tools = _tools(info, manager)
    body = json.loads(tools["find_callers"].invoke({"method_name": "target"}))
    assert any(call["file_path"] == "Valid.java" for call in body["calls"])
    assert all(call["file_path"] != "Broken.java" for call in body["calls"])
    listing = json.loads(tools["list_symbols"].invoke({}))
    assert any(
        diagnostic["file_path"] == "Broken.java"
        for diagnostic in listing["diagnostics"]
    )


def test_call_graph_is_workspace_isolated(manager, origin_repo):
    first = manager.create(origin_repo.as_uri())
    second = manager.create(origin_repo.as_uri())
    _write(
        Path(first.repository_path),
        "First.java",
        "class First { void target(){} void caller(){ target(); } }\n",
    )
    _write(
        Path(second.repository_path),
        "Second.java",
        "class Second { void target(){} void caller(){ target(); } }\n",
    )
    first_tools = _tools(first, manager)
    second_tools = _tools(second, manager)
    first_calls = json.loads(
        first_tools["find_callers"].invoke({"method_name": "target"})
    )
    second_calls = json.loads(
        second_tools["find_callers"].invoke({"method_name": "target"})
    )
    assert {call["file_path"] for call in first_calls["calls"]} == {"First.java"}
    assert {call["file_path"] for call in second_calls["calls"]} == {"Second.java"}


async def test_agent_prompt_tools_include_impact(workspace, manager):
    model = FakeChatModel(
        script=[
            tool_call(
                "analyze_impact",
                {"name": "multiply", "depth": 2},
                "impact_1",
            ),
            AIMessage(content="影响分析完成。"),
        ]
    )
    result = await run_agent(
        "分析 multiply 的影响范围",
        workspace.repository_path,
        model=model,
        workspace_id=workspace.workspace_id,
        workspace_manager=manager,
    )
    assert result.answer == "影响分析完成。"
    tool_messages = [
        message
        for message in model.history[1]
        if isinstance(message, ToolMessage)
    ]
    assert "impacted_callers" in tool_messages[0].content
    assert "PriceService" in tool_messages[0].content
