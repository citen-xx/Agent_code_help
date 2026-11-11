"""Deterministic compact delivery for Java code-intelligence results."""

import json

from app.code_intel.models import (
    CallQueryResult,
    ImpactAnalysisResult,
    JavaCallEdge,
    JavaReference,
    JavaSymbol,
    ReferenceQueryResult,
    SymbolQueryResult,
)

DEFAULT_SYMBOL_LIMIT = 30
MAX_SYMBOL_LIMIT = 100
DEFAULT_REFERENCE_LIMIT = 40
MAX_REFERENCE_LIMIT = 200
DEFAULT_CALL_LIMIT = 30
MAX_CALL_LIMIT = 100
DEFAULT_IMPACT_LIMIT = 20
MAX_IMPACT_LIMIT = 100
MAX_DIAGNOSTICS = 5
MAX_SIGNATURE_CHARS = 240
MAX_CONTEXT_CHARS = 160
MAX_CANDIDATES_PER_CALL = 3

SYMBOL_RESULT_CHARS = 12_000
REFERENCE_RESULT_CHARS = 12_000
CALL_RESULT_CHARS = 12_000
IMPACT_RESULT_CHARS = 16_000


def _clip(text: str, limit: int) -> str:
    value = " ".join(text.split())
    return value if len(value) <= limit else value[:limit] + "..."


def _symbol(symbol: JavaSymbol) -> dict:
    return {
        "name": symbol.name,
        "kind": symbol.symbol_type.value,
        "file": symbol.file_path,
        "line_start": symbol.line_start,
        "line_end": symbol.line_end,
        "signature": _clip(symbol.signature, MAX_SIGNATURE_CHARS),
        "containing_class": symbol.containing_class,
    }


def _reference(reference: JavaReference) -> dict:
    if reference.is_definition:
        context_type = "definition"
    elif "src/test/java/" in reference.file_path.replace("\\", "/"):
        context_type = "test"
    else:
        context_type = "source"
    return {
        "name": reference.name,
        "file": reference.file_path,
        "line": reference.line_start,
        "context_type": context_type,
        "context": _clip(reference.context, MAX_CONTEXT_CHARS),
    }


def _call(call: JavaCallEdge) -> dict:
    return {
        "caller": call.caller_symbol,
        "callee": call.callee_name,
        "file": call.file_path,
        "line": call.line,
        "containing_class": call.containing_class,
        "containing_method": call.containing_method,
        "ambiguous": call.ambiguous,
        "candidate_count": len(call.candidates),
        "candidates": [
            _symbol(candidate)
            for candidate in call.candidates[:MAX_CANDIDATES_PER_CALL]
        ],
    }


def _diagnostics(items) -> tuple[list[dict], int]:
    compact = [
        {
            "file": item.file_path,
            "line": item.line,
            "message": _clip(item.message, 160),
        }
        for item in items[:MAX_DIAGNOSTICS]
    ]
    return compact, len(items)


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _fit_single_collection(
    *,
    key: str,
    items: list[dict],
    limit: int,
    budget: int,
    diagnostics: list[dict],
    diagnostics_total: int,
    duplicates_removed: int,
    extra: dict | None = None,
) -> dict:
    selected = items[:limit]
    truncated = len(items) > len(selected)
    payload = {
        **(extra or {}),
        key: selected,
        "returned": len(selected),
        "total": len(items),
        "result_count": len(selected),
        "truncated": truncated,
        "duplicates_removed": duplicates_removed,
        "diagnostic_count": diagnostics_total,
        "diagnostics": diagnostics,
    }
    while selected and len(_json(payload)) > budget:
        selected.pop()
        payload[key] = selected
        payload["returned"] = len(selected)
        payload["result_count"] = len(selected)
        payload["truncated"] = True
    return payload


def compact_symbol_result(
    result: SymbolQueryResult,
    *,
    limit: int,
) -> str:
    items = [_symbol(symbol) for symbol in result.symbols]
    items, duplicates = _dedupe_symbol_dicts(items)
    diagnostics, diagnostic_total = _diagnostics(result.diagnostics)
    payload = _fit_single_collection(
        key="symbols",
        items=items,
        limit=limit,
        budget=SYMBOL_RESULT_CHARS,
        diagnostics=diagnostics,
        diagnostics_total=diagnostic_total,
        duplicates_removed=duplicates,
        extra={"total_matches": result.total},
    )
    payload["total"] = result.total
    return _json(payload)


def _dedupe_symbol_dicts(items: list[dict]) -> tuple[list[dict], int]:
    seen = set()
    result = []
    for item in items:
        key = (
            item["name"],
            item["kind"],
            item["file"],
            item["line_start"],
            item["signature"],
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result, len(items) - len(result)


def compact_reference_result(
    result: ReferenceQueryResult,
    *,
    limit: int,
) -> str:
    items = [_reference(reference) for reference in result.references]
    seen = set()
    deduped = []
    for item in items:
        key = (item["file"], item["line"], item["name"], item["context_type"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    diagnostics, diagnostic_total = _diagnostics(result.diagnostics)
    payload = _fit_single_collection(
        key="references",
        items=deduped,
        limit=limit,
        budget=REFERENCE_RESULT_CHARS,
        diagnostics=diagnostics,
        diagnostics_total=diagnostic_total,
        duplicates_removed=len(items) - len(deduped),
        extra={"total_matches": result.total},
    )
    payload["total"] = result.total
    return _json(payload)


def compact_call_result(
    result: CallQueryResult,
    *,
    limit: int,
) -> str:
    items = [_call(call) for call in result.calls]
    seen = set()
    deduped = []
    for item in items:
        key = (
            item["file"],
            item["line"],
            item["callee"],
            item["containing_class"],
            item["containing_method"],
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    diagnostics, diagnostic_total = _diagnostics(result.diagnostics)
    payload = _fit_single_collection(
        key="calls",
        items=deduped,
        limit=limit,
        budget=CALL_RESULT_CHARS,
        diagnostics=diagnostics,
        diagnostics_total=diagnostic_total,
        duplicates_removed=len(items) - len(deduped),
        extra={
            "targets": [_symbol(target) for target in result.targets],
            "ambiguous": result.ambiguous,
        },
    )
    return _json(payload)


def compact_impact_result(
    result: ImpactAnalysisResult,
    *,
    limit: int,
) -> str:
    callers = [
        {
            **_call(item.call),
            "depth": item.depth,
            "in_test": item.in_test,
        }
        for item in result.impacted_callers
    ]
    callers, caller_duplicates = _dedupe_calls(callers)
    references = [_reference(reference) for reference in result.test_references]
    references, reference_duplicates = _dedupe_references(references)
    related = [_symbol(symbol) for symbol in result.related_methods]
    related, related_duplicates = _dedupe_symbol_dicts(related)
    diagnostics, diagnostic_total = _diagnostics(result.diagnostics)

    caller_items = callers[:limit]
    reference_items = references[:limit]
    related_items = related[:limit]
    payload = {
        "query": result.query,
        "mode": result.mode,
        "depth": result.depth,
        "targets": [_symbol(target) for target in result.targets],
        "direct_callers": caller_items,
        "test_references": reference_items,
        "related_methods": related_items,
        "ambiguous": result.ambiguous,
        "returned": {
            "callers": len(caller_items),
            "references": len(reference_items),
            "related": len(related_items),
        },
        "total": len(callers) + len(references),
        "result_count": (
            len(caller_items) + len(reference_items) + len(related_items)
        ),
        "truncated": (
            len(callers) > len(caller_items)
            or len(references) > len(reference_items)
            or len(related) > len(related_items)
        ),
        "duplicates_removed": (
            caller_duplicates + reference_duplicates + related_duplicates
        ),
        "diagnostic_count": diagnostic_total,
        "diagnostics": diagnostics,
        "summary": {
            "caller_count": len(callers),
            "test_count": len(references),
            "related_count": len(related),
        },
    }
    while len(_json(payload)) > IMPACT_RESULT_CHARS:
        if related_items:
            related_items.pop()
        elif reference_items:
            reference_items.pop()
        elif caller_items:
            caller_items.pop()
        else:
            payload["diagnostics"] = []
            break
        payload["related_methods"] = related_items
        payload["test_references"] = reference_items
        payload["direct_callers"] = caller_items
        payload["returned"] = {
            "callers": len(caller_items),
            "references": len(reference_items),
            "related": len(related_items),
        }
        payload["result_count"] = (
            len(caller_items) + len(reference_items) + len(related_items)
        )
        payload["truncated"] = True
    return _json(payload)


def _dedupe_calls(items: list[dict]) -> tuple[list[dict], int]:
    seen = set()
    result = []
    for item in items:
        key = (
            item["file"],
            item["line"],
            item["callee"],
            item["containing_class"],
            item["containing_method"],
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result, len(items) - len(result)


def _dedupe_references(items: list[dict]) -> tuple[list[dict], int]:
    seen = set()
    result = []
    for item in items:
        key = (item["file"], item["line"], item["name"], item["context_type"])
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result, len(items) - len(result)
