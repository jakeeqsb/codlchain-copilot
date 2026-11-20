"""The LLM's only door to the data: tool schemas, dispatch, and serialization.

Three jobs, in one place so every tool behaves the same:

1. ``SCHEMAS`` describes each tool to the LLM in the OpenAI function-calling
   format, which ``ChatOpenAI.bind_tools`` accepts as is. The descriptions
   are the LLM's instructions for when to use a tool, so they are written
   for the model, not for developers.
2. :func:`call` maps a tool name to its function and runs it with the
   arguments the LLM sent.
3. :func:`call` also turns the result into plain JSON: a ``ToolError``
   becomes ``{"error": {...}}``, dates become ISO 8601 strings in UTC, and
   every ``start``, ``end``, and ``deadline`` gets a KST display string next
   to it. The LLM never converts time zones itself.

Typical usage from the agent:

    from coldchain_copilot.tools import gateway

    llm = ChatOpenAI(model=...).bind_tools(gateway.SCHEMAS)
    result = gateway.call("find_excursions", {"fleet_id": "FLEET-01"})

Not to be confused with ``coldchain_copilot.registry``, which maps ontology
names (detectors, handlers) to functions for the engine. Both point at the
same code: the registry is the engine's entrance, the gateway the LLM's.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from bson import ObjectId

from coldchain_copilot import engine
from coldchain_copilot.errors import ErrorCode, ToolError
from coldchain_copilot.tools import adhoc, lookup, sensor

Doc = dict[str, Any]

DISPLAY_TZ = ZoneInfo("Asia/Seoul")
"""Time zone of the display strings added next to quoted times."""

LOCAL_KEYS = frozenset({"start", "end", "deadline"})
"""Keys whose datetime value also gets a ``<key>Local`` display string."""


# ------------------------------------------------------------------ functions

FUNCTIONS: dict[str, Callable[..., Doc]] = {
    "get_shipment": lookup.get_shipment,
    "find_excursions": sensor.find_excursions,
    "find_energy_anomalies": sensor.find_energy_anomalies,
    "get_fleet_overview": sensor.get_fleet_overview,
    "get_reefer_timeline": sensor.get_reefer_timeline,
    "evaluate_obligations": engine.evaluate_shipment,
    "run_aggregation": adhoc.run_aggregation,
}
"""Tool name → function. ``evaluate_obligations`` is the engine itself."""


# -------------------------------------------------------------------- schemas

_SCOPE = {
    "fleet_id": {"type": "string", "description": "Fleet id, e.g. FLEET-01."},
    "reefer_id": {"type": "string", "description": "Reefer id, e.g. RF-02."},
    "shipment_id": {
        "type": "string",
        "description": "Shipment id, e.g. SHP-102. Covers the whole shipment period; hours/start/end are ignored.",
    },
}

_WINDOW = {
    "hours": {
        "type": "number",
        "description": "Window length ending now, 1-72. Default 24.",
    },
    "start": {
        "type": "string",
        "description": "Window start, ISO 8601 with offset, e.g. 2026-10-15T09:00:00+09:00.",
    },
    "end": {
        "type": "string",
        "description": "Window end, ISO 8601 with offset. At most 72 hours after start.",
    },
}


def _tool(name: str, description: str, properties: Doc, required: list[str] | None = None) -> Doc:
    """Builds one tool schema in the OpenAI function-calling format."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


SCHEMAS: list[Doc] = [
    _tool(
        "get_shipment",
        "Look up one shipment: reefer, cargo type, route, temperature policy (minC, maxC), "
        "contract, departure and delivery. Give shipment_id, or reefer_id for the shipment that "
        "reefer is carrying now. Call this first when you need cargo or the allowed temperature range.",
        {"shipment_id": _SCOPE["shipment_id"] | {"description": "Shipment id, e.g. SHP-102."},
         "reefer_id": _SCOPE["reefer_id"]},
    ),
    _tool(
        "find_excursions",
        "Find runs of temperature outside each shipment's policy range. Each event has start, end, "
        "direction (above/below), consecutiveMinutes, cumulativeMinutes, peakTemp, lowestTemp, and "
        "cause (compressor_stop, door_opening, or unknown). Needs at least one of fleet_id, "
        "reefer_id, shipment_id. Does not say whether action is required: use evaluate_obligations.",
        {**_SCOPE, **_WINDOW,
         "min_minutes": {"type": "integer", "description": "Shortest run to report. Default 2."}},
    ),
    _tool(
        "find_energy_anomalies",
        "Compare each shipment's average power in its last 24 hours of data with the 48 hours "
        "before (measures.powerChangePct, percent). Reports the number for every shipment in "
        "scope, large or small; whether it needs action is decided by evaluate_obligations.",
        dict(_SCOPE),
    ),
    _tool(
        "get_fleet_overview",
        "Summarize every shipment in a fleet over one window: average, min, max, and last "
        "temperature, average power, door-open minutes, and excursion count.",
        {"fleet_id": _SCOPE["fleet_id"], **_WINDOW},
        ["fleet_id"],
    ),
    _tool(
        "get_reefer_timeline",
        "One reefer's readings in time buckets: average, min, max temperature, average and min "
        "power, door open. Use it to describe how an incident developed.",
        {"reefer_id": _SCOPE["reefer_id"], **_WINDOW,
         "bucket_minutes": {"type": "integer",
                            "description": "Bucket size. Default 5; widened automatically to at most 96 buckets."}},
        ["reefer_id"],
    ),
    _tool(
        "evaluate_obligations",
        "Evaluate every contract clause, SOP, and manual section that applies to a shipment. Each "
        "result has status triggered, not_triggered, or unresolved (data missing: report it as "
        "needing confirmation, never as no obligation), the basis (measured value against the "
        "required value), deadline, and the source chunkId. actions lists what must be done, by "
        "whom, by when. Use this for any question about obligations, deadlines, or what to do.",
        {"shipment_id": _SCOPE["shipment_id"] | {"description": "Shipment id, e.g. SHP-102."}},
        ["shipment_id"],
    ),
    _tool(
        "run_aggregation",
        "Run a read-only MongoDB aggregation pipeline for a numeric question no other tool answers. "
        "collection is readings, shipments, or cargo_specs. readings fields: ts, temp, powerKw, "
        "doorOpen, humidityPct, meta.fleetId, meta.reeferId, meta.shipmentId, "
        "meta.temperaturePolicy. Write dates as {\"$date\": \"<ISO 8601>\"}. Writes, JavaScript, and "
        "other collections are rejected. At most 50 results; truncated=true means narrow the query.",
        {"collection": {"type": "string", "enum": sorted(adhoc.ALLOWED_COLLECTIONS)},
         "pipeline": {"type": "array", "items": {"type": "object"}, "description": "Aggregation stages."}},
        ["collection", "pipeline"],
    ),
]


# ------------------------------------------------------------------ dispatch


def call(name: str, arguments: Doc) -> Doc:
    """Runs a tool by name and returns a JSON-ready result.

    Never raises to the agent. A ``ToolError`` from a tool comes back as
    its error dict. Any other exception, such as a malformed time string or
    an argument the function does not take, comes back as ``QUERY_FAILED``
    with the exception's message, so the LLM can fix the call and retry.

    Args:
        name: Tool name, a key of ``FUNCTIONS``.
        arguments: Arguments as the LLM sent them.

    Returns:
        The serialized result, or ``{"error": {"code", "message"}}``.
    """
    function = FUNCTIONS.get(name)
    if function is None:
        return ToolError(ErrorCode.NOT_FOUND, f"Unknown tool {name!r}. Use one of {sorted(FUNCTIONS)}.").to_dict()

    try:
        return serialize(function(**arguments))
    except ToolError as exc:
        return exc.to_dict()
    except Exception as exc:  # noqa: BLE001 - the agent loop must never see a raise
        return ToolError(ErrorCode.QUERY_FAILED, f"{type(exc).__name__}: {exc}").to_dict()


def serialize(value: Any) -> Any:
    """Converts a tool result to plain JSON values, recursively.

    - ``datetime`` becomes ISO 8601 in UTC, e.g. ``"2026-10-15T00:12:00Z"``.
    - Under a ``start``, ``end``, or ``deadline`` key it also gets a
      ``startLocal`` / ``endLocal`` / ``deadlineLocal`` sibling such as
      ``"2026-10-15 09:12 KST"``.
    - ``ObjectId`` becomes its hex string.
    """
    if isinstance(value, dict):
        out: Doc = {}
        for key, item in value.items():
            out[key] = serialize(item)
            if key in LOCAL_KEYS and isinstance(item, datetime):
                out[f"{key}Local"] = local_time(item)
        return out
    if isinstance(value, list):
        return [serialize(item) for item in value]
    if isinstance(value, datetime):
        return utc_time(value)
    if isinstance(value, ObjectId):
        return str(value)
    return value


def utc_time(value: datetime) -> str:
    """Formats a datetime as ISO 8601 in UTC with a ``Z`` suffix."""
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def local_time(value: datetime) -> str:
    """Formats a datetime for display in ``DISPLAY_TZ``, e.g. ``"2026-10-15 09:12 KST"``."""
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    shown = aware.astimezone(DISPLAY_TZ)
    return f"{shown:%Y-%m-%d %H:%M} {shown.tzname()}"