"""Compact progressive-disclosure Code Intelligence contract tests."""

import json

from app.code_intel.compact import (
    IMPACT_RESULT_CHARS,
    SYMBOL_RESULT_CHARS,
    compact_impact_result,
    compact_reference_result,
    compact_symbol_result,
)
from app.code_intel.models import (
    ImpactAnalysisResult,
    ImpactedCaller,
    JavaCallEdge,
    JavaReference,
    JavaSymbol,
    JavaSymbolType,
    ReferenceQueryResult,
    SymbolQueryResult,
)


def _symbol(index: int, *, name: str = "run") -> JavaSymbol:
    return JavaSymbol(
        name=name,
        symbol_type=JavaSymbolType.METHOD,
        file_path=f"src/main/java/p{index // 5}/C{index}.java",
        line_start=index + 1,
        line_end=index + 10,
        containing_class=f"C{index}",
        signature=f"public void run(int value) " + "x" * 500,
    )


def test_compact_symbols_have_locations_and_no_body():
    items = [_symbol(i) for i in range(50)]
    body = json.loads(
        compact_symbol_result(
            SymbolQueryResult(
                symbols=items,
                diagnostics=[],
                total=50,
                truncated=True,
            ),
            limit=5,
        )
    )
    assert body["returned"] == 5
    assert body["total"] == 50
    assert body["truncated"] is True
    assert body["result_count"] == 5
    first = body["symbols"][0]
    assert set(first) >= {
        "name",
        "kind",
        "file",
        "line_start",
        "line_end",
        "signature",
    }
    assert "{" not in first["signature"]
    assert len(first["signature"]) <= 243


def test_compact_symbol_budget_is_deterministic_and_reported():
    items = [_symbol(i) for i in range(30)]
    first = compact_symbol_result(
        SymbolQueryResult(
            symbols=items,
            diagnostics=[],
            total=30,
            truncated=True,
        ),
        limit=30,
    )
    second = compact_symbol_result(
        SymbolQueryResult(
            symbols=items,
            diagnostics=[],
            total=30,
            truncated=True,
        ),
        limit=30,
    )
    assert first == second
    assert len(first) <= SYMBOL_RESULT_CHARS
    body = json.loads(first)
    assert body["truncated"] is True
    assert body["returned"] < 30


def test_duplicate_compact_symbols_are_removed():
    item = _symbol(1)
    body = json.loads(
        compact_symbol_result(
            SymbolQueryResult(
                symbols=[item, item, item],
                diagnostics=[],
                total=3,
                truncated=False,
            ),
            limit=30,
        )
    )
    assert body["returned"] == 1
    assert body["duplicates_removed"] == 2


def test_compact_reference_has_structured_location_and_short_context():
    reference = JavaReference(
        name="missing",
        file_path="src/test/java/ExampleTest.java",
        line_start=12,
        line_end=12,
        column_start=1,
        column_end=8,
        context="    assertTrue(missing); " + "y" * 500,
        is_definition=False,
    )
    body = json.loads(
        compact_reference_result(
            ReferenceQueryResult(
                references=[reference, reference],
                diagnostics=[],
                total=2,
                truncated=False,
            ),
            limit=30,
        )
    )
    item = body["references"][0]
    assert item["file"] == "src/test/java/ExampleTest.java"
    assert item["line"] == 12
    assert item["context_type"] == "test"
    assert len(item["context"]) <= 163
    assert body["duplicates_removed"] == 1


def test_impact_compact_sections_are_budgeted_with_summary():
    target = JavaSymbol(
        name="checkout",
        symbol_type=JavaSymbolType.METHOD,
        file_path="src/main/java/CheckoutService.java",
        line_start=10,
        line_end=30,
        containing_class="CheckoutService",
        signature="public int checkout(int qty)",
    )
    call = JavaCallEdge(
        caller_symbol="Controller.checkout",
        callee_name="checkout",
        file_path="src/main/java/Controller.java",
        line=5,
        column=2,
        containing_class="Controller",
        containing_method="checkout",
        caller_signature="public int checkout(int qty)",
        caller_line_start=5,
        caller_line_end=8,
    )
    impacted = [
        ImpactedCaller(
            depth=1,
            in_test=False,
            call=call.model_copy(update={"line": index + 1}),
        )
        for index in range(100)
    ]
    body = json.loads(
        compact_impact_result(
            ImpactAnalysisResult(
                query="checkout",
                mode="method",
                depth=1,
                targets=[target],
                ambiguous=False,
                impacted_callers=impacted,
                test_references=[],
                related_methods=[],
                diagnostics=[],
                total=100,
                truncated=True,
            ),
            limit=20,
        )
    )
    assert len(json.dumps(body, ensure_ascii=False)) <= IMPACT_RESULT_CHARS
    assert body["summary"]["caller_count"] == 100
    assert body["truncated"] is True
    assert len(body["direct_callers"]) <= 20
