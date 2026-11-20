"""Unit tests for coldchain_copilot.tools.gateway: schemas, dispatch, serialization.

No database: dispatch is tested with tools that fail before any query
(missing scope, rejected pipeline) or with a stand-in function.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest
from bson import ObjectId

from coldchain_copilot.errors import ErrorCode, ToolError
from coldchain_copilot.tools import gateway


def schema(name: str) -> dict:
    return next(s["function"] for s in gateway.SCHEMAS if s["function"]["name"] == name)


# --- schemas -------------------------------------------------------------------


def test_every_function_has_one_schema() -> None:
    names = [s["function"]["name"] for s in gateway.SCHEMAS]
    assert sorted(names) == sorted(gateway.FUNCTIONS)
    assert len(names) == len(set(names))


@pytest.mark.parametrize("name", sorted(gateway.FUNCTIONS))
def test_schema_matches_function_signature(name: str) -> None:
    params = inspect.signature(gateway.FUNCTIONS[name]).parameters
    properties = schema(name)["parameters"]["properties"]
    required = set(schema(name)["parameters"]["required"])

    # Every schema field is a real parameter, the LLM can't be told to send "now".
    assert set(properties) <= set(params), name
    assert "now" not in properties
    # Parameters without a default must be required in the schema.
    no_default = {p for p, v in params.items() if v.default is inspect.Parameter.empty}
    assert no_default <= required, name
    assert required <= set(properties), name


def test_descriptions_are_written_for_the_model() -> None:
    for tool in gateway.SCHEMAS:
        assert len(tool["function"]["description"]) > 60, tool["function"]["name"]


# --- dispatch ------------------------------------------------------------------


def test_unknown_tool_returns_error() -> None:
    assert gateway.call("delete_everything", {})["error"]["code"] == "NOT_FOUND"


def test_tool_error_comes_back_as_dict() -> None:
    assert gateway.call("find_excursions", {}) == {
        "error": {"code": "SCOPE_REQUIRED", "message": "Provide fleet_id, reefer_id, or shipment_id."}
    }
    assert gateway.call("run_aggregation", {"collection": "clauses", "pipeline": []})["error"]["code"] == (
        "QUERY_REJECTED"
    )


def test_unexpected_exception_becomes_query_failed(monkeypatch) -> None:
    def broken(**_: object) -> dict:
        raise ValueError("Invalid isoformat string: 'yesterday'")

    monkeypatch.setitem(gateway.FUNCTIONS, "find_excursions", broken)
    error = gateway.call("find_excursions", {"fleet_id": "FLEET-01", "start": "yesterday"})["error"]
    assert error["code"] == "QUERY_FAILED"
    assert "yesterday" in error["message"]


def test_unknown_argument_becomes_query_failed() -> None:
    assert gateway.call("get_shipment", {"ship": "SHP-102"})["error"]["code"] == "QUERY_FAILED"


def test_success_is_serialized(monkeypatch) -> None:
    when = datetime(2026, 10, 15, 0, 12, tzinfo=timezone.utc)
    monkeypatch.setitem(gateway.FUNCTIONS, "get_shipment", lambda **_: {"departedAt": when})
    assert gateway.call("get_shipment", {"shipment_id": "SHP-102"}) == {"departedAt": "2026-10-15T00:12:00Z"}


# --- serialize -----------------------------------------------------------------


def test_times_are_utc_and_quoted_times_get_kst() -> None:
    start = datetime(2026, 10, 15, 0, 12, tzinfo=timezone.utc)
    event = {"start": start, "measures": {"peakTemp": 8.2}}

    assert gateway.serialize(event) == {
        "start": "2026-10-15T00:12:00Z",
        "startLocal": "2026-10-15 09:12 KST",
        "measures": {"peakTemp": 8.2},
    }


def test_kst_crosses_midnight() -> None:
    late = datetime(2026, 10, 15, 16, 30, tzinfo=timezone.utc)
    assert gateway.serialize({"deadline": late})["deadlineLocal"] == "2026-10-16 01:30 KST"


def test_other_dates_get_no_display_string() -> None:
    out = gateway.serialize({"departedAt": datetime(2026, 10, 15, tzinfo=timezone.utc)})
    assert set(out) == {"departedAt"}


def test_nested_lists_and_object_ids() -> None:
    oid = ObjectId("65f000000000000000000000")
    start = datetime(2026, 10, 15, tzinfo=timezone.utc)
    out = gateway.serialize({"results": [{"_id": oid, "event": {"start": start}}]})

    assert out["results"][0]["_id"] == "65f000000000000000000000"
    assert out["results"][0]["event"]["startLocal"] == "2026-10-15 09:00 KST"