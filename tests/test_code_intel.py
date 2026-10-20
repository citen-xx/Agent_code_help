"""Tree-sitter Java symbol index and read-only tool tests."""

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from app.agent.graph import run_agent
from app.code_intel.index import CodeIntelligenceIndex
from app.code_intel.parser import parse_java_source
from app.code_intel.registry import (
    clear_all_indexes,
    get_index,
)
from app.code_intel.tools import create_code_intel_tools
from app.tools.patch_tools import create_patch_tools
from app.tools.repo import ToolError
from app.workspace.manager import WorkspaceNotFoundError
from tests.fakes import FakeChatModel, tool_call


@pytest.fixture(autouse=True)
def clean_indexes():
    clear_all_indexes()
    yield
    clear_all_indexes()


STRUCTURE_SOURCE = b"""package com.example;
import java.util.List;

public class Outer {
    private int count;

    public Outer() {
    }

    public void run(String name) {
    }

    public void run(int count) {
    }

    class Inner {
        private String label;

        void visit() {
            run(1);
        }
    }
}

interface Service {
    void execute();
}

enum Status {
    READY;
    private int code;
}

record Point(int x, int y) {
    int sum() {
        return x + y;
    }
}
"""


def _write(repo: Path, relative: str, source: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_tree_sitter_extracts_java_symbols():
    parsed = parse_java_source("Outer.java", STRUCTURE_SOURCE)
    assert parsed.diagnostics == []
    symbols = parsed.symbols

    assert any(
        symbol.name == "com.example"
        and symbol.symbol_type == "package"
        for symbol in symbols
    )
    assert any(
        symbol.name == "java.util.List"
        and symbol.symbol_type == "import"
        for symbol in symbols
    )
    assert any(symbol.name == "Outer" and symbol.symbol_type == "class" for symbol in symbols)
    assert any(symbol.name == "Service" and symbol.symbol_type == "interface" for symbol in symbols)
    assert any(symbol.name == "Status" and symbol.symbol_type == "enum" for symbol in symbols)
    assert any(symbol.name == "Point" and symbol.symbol_type == "record" for symbol in symbols)

    overloads = [
        symbol
        for symbol in symbols
        if symbol.name == "run" and symbol.symbol_type == "method"
    ]
    assert len(overloads) == 2
    assert {symbol.signature for symbol in overloads} == {
        "public void run(String name)",
        "public void run(int count)",
    }

    constructor = next(
        symbol
        for symbol in symbols
        if symbol.symbol_type == "constructor" and symbol.containing_class == "Outer"
    )
    assert constructor.signature == "public Outer()"
    assert any(
        symbol.name == "count"
        and symbol.symbol_type == "field"
        and symbol.containing_class == "Outer"
        for symbol in symbols
    )
    assert any(
        symbol.name == "Inner"
        and symbol.symbol_type == "class"
        and symbol.containing_class == "Outer"
        for symbol in symbols
    )
    assert any(
        symbol.name == "visit" and symbol.containing_class == "Outer.Inner"
        for symbol in symbols
    )
    assert {
        symbol.name
        for symbol in symbols
        if symbol.containing_class == "Point" and symbol.symbol_type == "field"
    } == {"x", "y"}


def test_parse_error_is_diagnostic_and_file_is_skipped():
    parsed = parse_java_source("Broken.java", b"class Broken { void run( { }\n")
    assert parsed.symbols == []
    assert parsed.references == []
    assert parsed.diagnostics
    assert parsed.diagnostics[0].file_path == "Broken.java"
    assert "语法错误" in parsed.diagnostics[0].message


@pytest.fixture
def workspace(manager, origin_repo):
    info = manager.create(origin_repo.as_uri())
    repo = Path(info.repository_path)
    _write(
        repo,
        "src/main/java/com/example/OrderService.java",
        """package com.example;

public class OrderService {
    private int sequence;

    public OrderService() {
    }

    public void create() {
        sequence++;
    }
}
""",
    )
    _write(
        repo,
        "src/main/java/com/example/App.java",
        """package com.example;

public class App {
    public void start() {
        OrderService service = new OrderService();
        service.create();
    }
}
""",
    )
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


def test_list_symbols_tool_filters_file_and_class(workspace, manager):
    tools = _tools(workspace, manager)
    file_body = json.loads(
        tools["list_symbols"].invoke(
            {"file_path": "src/main/java/com/example/OrderService.java"}
        )
    )
    assert file_body["total"] >= 4
    assert {symbol["name"] for symbol in file_body["symbols"]} >= {
        "OrderService",
        "create",
        "sequence",
    }

    class_body = json.loads(
        tools["list_symbols"].invoke(
            {"class_name": "OrderService", "symbol_type": "method"}
        )
    )
    assert [symbol["name"] for symbol in class_body["symbols"]] == ["create"]
    assert class_body["symbols"][0]["file_path"].endswith("OrderService.java")
    assert class_body["symbols"][0]["line_start"] >= 1


def test_index_only_reads_java_files(workspace, manager):
    repo = Path(workspace.repository_path)
    _write(repo, "src/main/resources/Notes.txt", "class NotJava { void hidden() {} }\n")
    tools = _tools(workspace, manager)
    body = json.loads(tools["find_symbol"].invoke({"name": "NotJava"}))
    assert body["total"] == 0


def test_find_symbol_returns_duplicate_and_overloaded_definitions(manager, origin_repo):
    info = manager.create(origin_repo.as_uri())
    repo = Path(info.repository_path)
    _write(repo, "A.java", "class Duplicate { void run() {} void run(int x) {} }\n")
    _write(repo, "B.java", "class Duplicate { void other() {} }\n")
    tools = _tools(info, manager)

    body = json.loads(tools["find_symbol"].invoke({"name": "Duplicate"}))
    assert body["total"] == 2
    assert {symbol["file_path"] for symbol in body["symbols"]} == {"A.java", "B.java"}

    overloads = json.loads(
        tools["find_symbol"].invoke({"name": "run", "symbol_type": "method"})
    )
    assert overloads["total"] == 2
    assert len({symbol["signature"] for symbol in overloads["symbols"]}) == 2


def test_find_references_returns_usage_locations(workspace, manager):
    tools = _tools(workspace, manager)
    body = json.loads(tools["find_references"].invoke({"name": "OrderService"}))
    assert body["total"] >= 2
    assert any(reference["file_path"].endswith("App.java") for reference in body["references"])
    assert all(reference["line_start"] >= 1 for reference in body["references"])

    with_definitions = json.loads(
        tools["find_references"].invoke(
            {"name": "OrderService", "include_definitions": True}
        )
    )
    assert with_definitions["total"] > body["total"]
    assert any(reference["is_definition"] for reference in with_definitions["references"])


def test_patch_refreshes_only_affected_java_file(workspace, manager, monkeypatch):
    repo = Path(workspace.repository_path)
    code_tools = _tools(workspace, manager)
    code_tools["list_symbols"].invoke({})
    assert get_index(workspace.workspace_id) is not None

    from app.code_intel import index as index_module

    real_parse = index_module.parse_java_file
    parsed_paths: list[str] = []

    def tracking_parse(repository_path, relative_path):
        parsed_paths.append(relative_path)
        return real_parse(repository_path, relative_path)

    monkeypatch.setattr(index_module, "parse_java_file", tracking_parse)
    patch_tool = {
        tool.name: tool
        for tool in create_patch_tools(
            workspace.workspace_id,
            workspace.repository_path,
            manager=manager,
        )
    }["apply_patch"]
    patch_tool.invoke(
        {
            "changes": [
                {
                    "path": "src/main/java/com/example/OrderService.java",
                    "old_text": "public void create()",
                    "new_text": "public void placeOrder()",
                }
            ]
        }
    )

    assert parsed_paths == ["src/main/java/com/example/OrderService.java"]
    assert (
        json.loads(code_tools["find_symbol"].invoke({"name": "create"}))["total"]
        == 0
    )
    refreshed = json.loads(
        code_tools["find_symbol"].invoke({"name": "placeOrder"})
    )
    assert refreshed["total"] == 1
    assert "placeOrder" in (repo / "src/main/java/com/example/OrderService.java").read_text(
        encoding="utf-8"
    )


def test_syntax_error_isolated_in_index(manager, origin_repo):
    info = manager.create(origin_repo.as_uri())
    repo = Path(info.repository_path)
    _write(repo, "Valid.java", "class Valid { void ok() {} }\n")
    _write(repo, "Broken.java", "class Broken { void broken( { }\n")
    tools = _tools(info, manager)

    body = json.loads(tools["list_symbols"].invoke({}))
    assert any(symbol["name"] == "Valid" for symbol in body["symbols"])
    assert not any(symbol["file_path"] == "Broken.java" for symbol in body["symbols"])
    assert any(diagnostic["file_path"] == "Broken.java" for diagnostic in body["diagnostics"])


def test_workspace_isolation_and_safe_file_query(manager, origin_repo):
    first = manager.create(origin_repo.as_uri())
    second = manager.create(origin_repo.as_uri())
    _write(Path(first.repository_path), "First.java", "class FirstOnly {}\n")
    _write(Path(second.repository_path), "Second.java", "class SecondOnly {}\n")
    first_tools = _tools(first, manager)
    second_tools = _tools(second, manager)

    assert json.loads(first_tools["find_symbol"].invoke({"name": "FirstOnly"}))["total"] == 1
    assert json.loads(first_tools["find_symbol"].invoke({"name": "SecondOnly"}))["total"] == 0
    assert json.loads(second_tools["find_symbol"].invoke({"name": "SecondOnly"}))["total"] == 1
    with pytest.raises(ToolError):
        first_tools["list_symbols"].invoke({"file_path": "../Second.java"})


def test_delete_workspace_clears_index(workspace, manager):
    _tools(workspace, manager)["list_symbols"].invoke({})
    assert get_index(workspace.workspace_id) is not None
    manager.delete(workspace.workspace_id)
    assert get_index(workspace.workspace_id) is None


def test_code_intel_tools_require_workspace_binding(workspace, manager):
    with pytest.raises(WorkspaceNotFoundError):
        create_code_intel_tools(
            "abcdef012345",
            workspace.repository_path,
            manager=manager,
        )


async def test_agent_can_use_symbol_tools(workspace, manager):
    model = FakeChatModel(
        script=[
            tool_call("find_symbol", {"name": "OrderService"}, "find_1"),
            tool_call("find_references", {"name": "OrderService"}, "refs_1"),
            AIMessage(content="已通过符号索引定位 OrderService。"),
        ]
    )
    result = await run_agent(
        "定位 OrderService",
        workspace.repository_path,
        model=model,
        workspace_id=workspace.workspace_id,
        workspace_manager=manager,
    )
    assert result.answer == "已通过符号索引定位 OrderService。"
    tool_contents = "\n".join(
        message.content
        for message in model.history[2]
        if message.type == "tool"
    )
    assert "OrderService.java" in tool_contents
    assert '"references"' in tool_contents
