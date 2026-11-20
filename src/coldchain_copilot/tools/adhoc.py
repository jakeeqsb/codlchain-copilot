"""Ad hoc tool: an aggregation pipeline the LLM writes itself, behind a guard.

The fixed tools cover the questions the demo is built around. For anything
else numeric ("average temperature per hour on RF-04 yesterday",
"how many shipments carry tuna"), the LLM may write its own pipeline. Free
queries are the riskiest path in the agent, so every pipeline is checked
before it runs:

- It runs on ``readings``, ``shipments``, or ``cargo_specs`` only.
- No stage or operator writes data or runs JavaScript, at any depth.
- Stages that read another collection (``$lookup``, ``$graphLookup``,
  ``$unionWith``) may name only the same allowed collections, so a pipeline
  cannot reach ``clauses``, ``ontology``, ``knowledge``, or the agent's
  checkpoints through a join.
- It stops after ``MAX_TIME_MS`` and returns at most ``RESULT_LIMIT``
  documents.

Typical tool call from the LLM:

    run_aggregation(
        collection="readings",
        pipeline=[
            {"$match": {"meta.reeferId": "RF-04",
                        "ts": {"$gte": {"$date": "2026-10-14T00:00:00Z"}}}},
            {"$group": {"_id": None, "avgTemp": {"$avg": "$temp"}}},
        ],
    )
"""

from __future__ import annotations

import json
from datetime import timezone
from typing import Any

from bson import json_util
from pymongo.errors import PyMongoError

from coldchain_copilot import db
from coldchain_copilot.errors import ErrorCode, ToolError

Doc = dict[str, Any]

ALLOWED_COLLECTIONS = frozenset({db.READINGS, db.SHIPMENTS, db.CARGO_SPECS})
"""Collections a pipeline may run on or join with."""

FORBIDDEN_OPERATORS = frozenset({"$out", "$merge", "$function", "$accumulator", "$where"})
"""Write stages and JavaScript execution, rejected wherever they appear."""

JOIN_STAGES = frozenset({"$lookup", "$graphLookup", "$unionWith"})
"""Stages that read another collection."""

MAX_TIME_MS = 5000
RESULT_LIMIT = 50

AWARE_UTC = json_util.RELAXED_JSON_OPTIONS.with_options(tz_aware=True, tzinfo=timezone.utc)
"""Extended JSON parsing that yields aware UTC datetimes."""


def run_aggregation(collection: str, pipeline: list[Doc]) -> Doc:
    """Checks and runs an LLM-written aggregation pipeline.

    Dates in the pipeline are written as ``{"$date": "<ISO 8601>"}``, the
    MongoDB Extended JSON form, because a tool call can only carry JSON.
    They are converted to real dates before the pipeline runs.

    Args:
        collection: Collection to run on; one of ``ALLOWED_COLLECTIONS``.
        pipeline: Aggregation stages as JSON objects.

    Returns:
        ``{"collection", "results", "truncated"}``. ``truncated`` is true when
        the result hit ``RESULT_LIMIT``, so the LLM knows to narrow the query
        rather than treat the list as complete.

    Raises:
        ToolError: ``QUERY_REJECTED`` when the guard refuses the pipeline;
            ``QUERY_FAILED`` when MongoDB returns an error.
    """
    check_pipeline(collection, pipeline)
    stages = [*from_extended_json(pipeline), {"$limit": RESULT_LIMIT}]

    try:
        results = list(db.collection(collection).aggregate(stages, maxTimeMS=MAX_TIME_MS))
    except PyMongoError as exc:
        raise ToolError(ErrorCode.QUERY_FAILED, f"MongoDB rejected the pipeline: {exc}") from exc

    return {"collection": collection, "results": results, "truncated": len(results) == RESULT_LIMIT}


def check_pipeline(collection: str, pipeline: Any) -> None:
    """Rejects a pipeline the agent must not run.

    Walks every nested object, so a forbidden operator inside a ``$lookup``
    sub-pipeline or a ``$facet`` branch is found too.

    Args:
        collection: The collection the pipeline will run on.
        pipeline: The pipeline as the LLM sent it.

    Raises:
        ToolError: ``QUERY_REJECTED``, with a message naming the problem.
    """
    if collection not in ALLOWED_COLLECTIONS:
        _reject(f"Collection {collection!r} is not allowed. Use one of {sorted(ALLOWED_COLLECTIONS)}.")
    if not isinstance(pipeline, list) or not all(isinstance(stage, dict) for stage in pipeline):
        _reject("pipeline must be a list of stage objects.")
    _check_node(pipeline)


def from_extended_json(pipeline: list[Doc]) -> list[Doc]:
    """Converts Extended JSON values such as ``{"$date": ...}`` to BSON types.

    Dates come back timezone-aware in UTC, like everything ``db.py`` reads.
    """
    return json_util.loads(json.dumps(pipeline), json_options=AWARE_UTC)


def _check_node(node: Any) -> None:
    """Recursively checks one value of the pipeline."""
    if isinstance(node, list):
        for item in node:
            _check_node(item)
        return
    if not isinstance(node, dict):
        return

    for key, value in node.items():
        if key in FORBIDDEN_OPERATORS:
            _reject(f"{key} is not allowed: the agent is read-only and runs no JavaScript.")
        if key in JOIN_STAGES:
            # $lookup and $graphLookup name it in "from"; $unionWith takes a bare
            # name or {"coll": name}.
            if isinstance(value, str):
                source = value
            elif isinstance(value, dict):
                source = value.get("from") or value.get("coll")
            else:
                source = None
            if source not in ALLOWED_COLLECTIONS:
                _reject(f"{key} may only read {sorted(ALLOWED_COLLECTIONS)}, not {source!r}.")
        _check_node(value)


def _reject(message: str) -> None:
    raise ToolError(ErrorCode.QUERY_REJECTED, message)