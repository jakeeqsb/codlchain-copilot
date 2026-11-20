"""Unit tests for coldchain_copilot.tools.adhoc: the pipeline guard.

No database: the guard runs before anything reaches MongoDB.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from coldchain_copilot.errors import ToolError
from coldchain_copilot.tools.adhoc import check_pipeline, from_extended_json


def rejected(collection: str, pipeline) -> str:
    """Returns the rejection message, failing the test if the guard accepts."""
    with pytest.raises(ToolError) as exc_info:
        check_pipeline(collection, pipeline)
    assert exc_info.value.code == "QUERY_REJECTED"
    return exc_info.value.message


# --- accepted ------------------------------------------------------------------


def test_read_only_pipeline_is_accepted() -> None:
    check_pipeline(
        "readings",
        [
            {"$match": {"meta.reeferId": "RF-04"}},
            {"$group": {"_id": "$meta.shipmentId", "avgTemp": {"$avg": "$temp"}}},
        ],
    )


def test_join_between_allowed_collections_is_accepted() -> None:
    check_pipeline("shipments", [{"$lookup": {"from": "cargo_specs", "localField": "cargoType",
                                              "foreignField": "_id", "as": "spec"}}])
    check_pipeline("shipments", [{"$unionWith": "cargo_specs"}])
    check_pipeline("shipments", [{"$unionWith": {"coll": "cargo_specs", "pipeline": []}}])


# --- rejected ------------------------------------------------------------------


@pytest.mark.parametrize("collection", ["clauses", "ontology", "knowledge", "checkpoints"])
def test_other_collections_are_rejected(collection: str) -> None:
    assert collection in rejected(collection, [])


@pytest.mark.parametrize(
    "stage",
    [
        {"$out": "copy"},
        {"$merge": {"into": "copy"}},
        {"$match": {"$where": "this.temp > 4"}},
        {"$set": {"x": {"$function": {"body": "return 1", "args": [], "lang": "js"}}}},
    ],
)
def test_writes_and_javascript_are_rejected(stage: dict) -> None:
    rejected("readings", [stage])


@pytest.mark.parametrize(
    "stage",
    [
        {"$lookup": {"from": "clauses", "pipeline": [], "as": "c"}},
        {"$graphLookup": {"from": "ontology", "startWith": "$x", "connectFromField": "a",
                          "connectToField": "b", "as": "g"}},
        {"$unionWith": "knowledge"},
        {"$unionWith": {"coll": "clauses"}},
    ],
)
def test_joins_to_other_collections_are_rejected(stage: dict) -> None:
    rejected("shipments", [stage])


def test_forbidden_operator_hidden_in_a_sub_pipeline_is_found() -> None:
    nested = {"$facet": {"a": [{"$lookup": {"from": "readings", "pipeline": [{"$out": "x"}], "as": "r"}}]}}
    assert "$out" in rejected("shipments", [nested])


@pytest.mark.parametrize("pipeline", [{"$match": {}}, ["$match"], None])
def test_pipeline_must_be_a_list_of_stages(pipeline) -> None:
    rejected("readings", pipeline)


# --- extended JSON -------------------------------------------------------------


def test_extended_json_dates_become_datetimes() -> None:
    [stage] = from_extended_json([{"$match": {"ts": {"$gte": {"$date": "2026-10-14T00:00:00Z"}}}}])
    assert stage["$match"]["ts"]["$gte"] == datetime(2026, 10, 14, tzinfo=timezone.utc)