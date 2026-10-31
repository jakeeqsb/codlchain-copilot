"""Turn the ontology's ``detect`` rules into events computed from ``readings``.

Events are not stored anywhere. Every call runs an aggregation over the Time
Series collection and returns what it finds, so a corrected reading or a
changed rule shows up on the next call without any backfill.

Each event class in the ontology carries a ``detect`` document, and the code
knows only two generic patterns:

``run``
    Consecutive readings that match a condition form one event, for example
    "temperature outside the policy range" or "door open". ``match`` is a
    plain MQL filter and goes straight into ``$match``. Two matching readings
    belong to the same run when they are at most ``RUN_GAP_MINUTES`` apart,
    so one missed reading does not split an excursion in two.

``trend``
    The average of a field over a recent window compared with the average
    over the baseline window just before it, for example power drawn in the
    last 24 hours against the 48 hours before that.

Adding an event type means adding an ontology document with one of these two
shapes. No code changes.

Every event has the same shape::

    {
        "eventType": "event:temperature_excursion",
        "shipmentId": "SHP-102",
        "reeferId": "RF-02",
        "start": datetime,   # first matching reading (run) / recent window start (trend)
        "end": datetime,     # last matching reading (run) / latest reading (trend)
        "measures": {"consecutiveMinutes": 24, "cumulativeMinutes": 24, ...},
    }

The keys in ``measures`` are the measure names clauses compare against, which
is how detection and rule evaluation connect.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import db, ontology

Doc = dict[str, Any]

RUN_GAP_MINUTES = 2
"""Largest gap between two matching readings that still counts as one run."""


def detect(event_type: str, scope: Doc, start: datetime, end: datetime) -> list[Doc]:
    """Detects every event of one type within a scope and time window.

    ``scope`` is a filter on reading metadata, built by the caller, for
    example ``{"meta.shipmentId": "SHP-102"}`` or ``{"meta.fleetId": "FLEET-01"}``.
    The window is ``[start, end)``; shipment-scoped callers pass the shipment
    period so cumulative measures cover the whole shipment.

    For a run rule with ``causeFrom``, the listed event types are detected in
    the same scope and window, and each event gets a ``cause`` measure naming
    the first one whose run overlaps it (``cause:unknown`` if none does).

    Raises:
        LookupError: The event type has no ``detect`` rule in the ontology.
    """
    event_class = ontology.event_classes({event_type}).get(event_type)
    if event_class is None or "detect" not in event_class:
        raise LookupError(f"{event_type} has no detect rule in the ontology")
    rule = event_class["detect"]

    if rule["pattern"] == "run":
        pipeline = run_pipeline(rule, scope, start, end)
    elif rule["pattern"] == "trend":
        pipeline = trend_pipeline(rule, scope, start, end)
    else:
        raise LookupError(f"{event_type}: unknown detect pattern {rule['pattern']!r}")

    events = [
        {"eventType": event_type, **doc}
        for doc in db.collection(db.READINGS).aggregate(pipeline)
    ]

    if rule.get("causeFrom") and events:
        candidates = {t: detect(t, scope, start, end) for t in rule["causeFrom"]}
        attach_causes(events, candidates)
    return events


def run_pipeline(rule: Doc, scope: Doc, start: datetime, end: datetime) -> list[Doc]:
    """Builds the aggregation for a ``run`` rule.

    The stages, in order:

    1. Keep readings in the scope and window that satisfy ``rule["match"]``.
       The scope and time bounds come first so the ``{meta.*, ts}`` indexes
       can be used; the rule's condition is applied in a separate ``$match``
       because it may be an ``$expr`` that compares two fields.
    2. Per shipment, in time order, look at the previous matching reading
       with ``$shift``. A reading starts a new run when there is no previous
       one or the gap is larger than ``RUN_GAP_MINUTES``.
    3. A running ``$sum`` of those "new run" flags numbers the runs, so every
       reading knows which run it belongs to.
    4. ``$group`` each run into one event: first and last timestamp plus any
       ``summary`` accumulators the rule declares (``$max``, ``$first``, ...).
       The ``$sort`` before it makes ``$first`` mean "earliest reading".
    5. Duration is ``last - first + 1`` minutes, because readings are one per
       minute and a single matching reading is a one-minute run. It is stored
       under the rule's ``durationAs`` name.
    6. If the rule has ``cumulativeAs``, every event of the shipment also gets
       the sum of all its runs' durations within the window.
    """
    duration = rule["durationAs"]
    summary = rule.get("summary", {})

    pipeline: list[Doc] = [
        {"$match": {**scope, "ts": {"$gte": start, "$lt": end}}},
        {"$match": rule["match"]},
        {
            "$setWindowFields": {
                "partitionBy": "$meta.shipmentId",
                "sortBy": {"ts": 1},
                "output": {"prevTs": {"$shift": {"output": "$ts", "by": -1}}},
            }
        },
        {
            "$set": {
                "newRun": {
                    "$cond": [
                        {
                            "$or": [
                                {"$eq": ["$prevTs", None]},
                                {
                                    "$gt": [
                                        {"$dateDiff": {"startDate": "$prevTs", "endDate": "$ts", "unit": "minute"}},
                                        RUN_GAP_MINUTES,
                                    ]
                                },
                            ]
                        },
                        1,
                        0,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "partitionBy": "$meta.shipmentId",
                "sortBy": {"ts": 1},
                "output": {
                    "runNo": {"$sum": "$newRun", "window": {"documents": ["unbounded", "current"]}}
                },
            }
        },
        {"$sort": {"ts": 1}},
        {
            "$group": {
                "_id": {
                    "shipmentId": "$meta.shipmentId",
                    "reeferId": "$meta.reeferId",
                    "runNo": "$runNo",
                },
                "start": {"$min": "$ts"},
                "end": {"$max": "$ts"},
                **summary,
            }
        },
        {
            "$set": {
                duration: {
                    "$add": [{"$dateDiff": {"startDate": "$start", "endDate": "$end", "unit": "minute"}}, 1]
                }
            }
        },
    ]

    if "cumulativeAs" in rule:
        pipeline.append(
            {
                "$setWindowFields": {
                    "partitionBy": "$_id.shipmentId",
                    "output": {
                        rule["cumulativeAs"]: {
                            "$sum": f"${duration}",
                            "window": {"documents": ["unbounded", "unbounded"]},
                        }
                    },
                }
            }
        )

    measure_names = [duration, *summary, *([rule["cumulativeAs"]] if "cumulativeAs" in rule else [])]
    pipeline += [
        {
            "$project": {
                "_id": 0,
                "shipmentId": "$_id.shipmentId",
                "reeferId": "$_id.reeferId",
                "start": 1,
                "end": 1,
                "measures": {name: f"${name}" for name in measure_names},
            }
        },
        {"$sort": {"start": 1, "reeferId": 1}},
    ]
    return pipeline


def trend_pipeline(rule: Doc, scope: Doc, start: datetime, end: datetime) -> list[Doc]:
    """Builds the aggregation for a ``trend`` rule.

    The windows are anchored on each shipment's latest reading inside
    ``[start, end)``, not on ``end`` itself. "Recent" means the last
    ``recentHours`` of data the reefer actually sent, and the baseline is the
    ``baselineHours`` before that. Anchoring on the clock instead would mix in
    hours with no data whenever readings stop before ``end``: a sensor that
    went quiet, or seeded demo data queried the next day. Both windows would
    then cover different parts of the curve and the change would be wrong.

    The stages, in order:

    1. Keep readings in the scope and window.
    2. ``$setWindowFields`` gives every reading its shipment's latest
       timestamp, and ``$dateSubtract`` turns that into the two window starts.
    3. A single ``$group`` per shipment averages the field twice: ``$cond``
       passes the value only for readings inside each window and ``null``
       otherwise, which ``$avg`` ignores.
    4. The change is the recent average over the baseline average, as a
       percentage rounded to one decimal, under the rule's ``changeAs`` name.

    A shipment without readings in one of the windows produces no event.
    Otherwise a trend always produces one event per shipment, even for a
    0.1 % change, so a clause can report the actual value when its threshold
    is not reached.
    """
    field = f"${rule['field']}"

    def avg_between(lo: str, hi: str) -> Doc:
        # Window (lo, hi]: excludes its start, includes its end (the latest reading).
        in_window = {"$and": [{"$gt": ["$ts", lo]}, {"$lte": ["$ts", hi]}]}
        return {"$avg": {"$cond": [in_window, field, None]}}

    return [
        {"$match": {**scope, "ts": {"$gte": start, "$lt": end}}},
        {
            "$setWindowFields": {
                "partitionBy": "$meta.shipmentId",
                "output": {"lastTs": {"$max": "$ts", "window": {"documents": ["unbounded", "unbounded"]}}},
            }
        },
        {
            "$set": {
                "recentStart": {"$dateSubtract": {"startDate": "$lastTs", "unit": "hour", "amount": rule["recentHours"]}},
            }
        },
        {
            "$set": {
                "baselineStart": {"$dateSubtract": {"startDate": "$recentStart", "unit": "hour", "amount": rule["baselineHours"]}},
            }
        },
        {
            "$group": {
                "_id": {"shipmentId": "$meta.shipmentId", "reeferId": "$meta.reeferId"},
                "start": {"$max": "$recentStart"},
                "end": {"$max": "$lastTs"},
                "recentAvg": avg_between("$recentStart", "$lastTs"),
                "baselineAvg": avg_between("$baselineStart", "$recentStart"),
            }
        },
        {"$match": {"recentAvg": {"$ne": None}, "baselineAvg": {"$gt": 0}}},
        {
            "$project": {
                "_id": 0,
                "shipmentId": "$_id.shipmentId",
                "reeferId": "$_id.reeferId",
                "start": 1,
                "end": 1,
                "measures": {
                    rule["changeAs"]: {
                        "$round": [
                            {"$subtract": [{"$multiply": [{"$divide": ["$recentAvg", "$baselineAvg"]}, 100]}, 100]},
                            1,
                        ]
                    },
                    "recentAvg": {"$round": ["$recentAvg", 3]},
                    "baselineAvg": {"$round": ["$baselineAvg", 3]},
                },
            }
        },
        {"$sort": {"reeferId": 1}},
    ]


def attach_causes(events: list[Doc], candidates: dict[str, list[Doc]]) -> None:
    """Sets ``measures.cause`` on each event from overlapping candidate events.

    A candidate is a cause when it is on the same shipment and its time span
    overlaps the event's span. Cause types are tried in the order the rule
    lists them in ``causeFrom``, so that order is the priority when two
    candidates overlap. The value uses the ontology's cause vocabulary:
    ``event:compressor_stop`` becomes ``cause:compressor_stop``; no overlap
    gives ``cause:unknown``.

    Modifies ``events`` in place.
    """
    for event in events:
        event["measures"]["cause"] = "cause:unknown"
        for cause_type, found in candidates.items():
            if any(
                c["shipmentId"] == event["shipmentId"]
                and c["start"] <= event["end"]
                and c["end"] >= event["start"]
                for c in found
            ):
                event["measures"]["cause"] = "cause:" + cause_type.split(":", 1)[1]
                break


def field_collected(field: str, scope: Doc, start: datetime, end: datetime) -> bool:
    """Tells whether any reading in the scope and window has ``field``.

    The evaluator uses this with an event class's ``requiresFields``. If the
    field was never collected (no humidity sensor on RF-05), finding no event
    means "unknown", not "nothing happened", and the clause is ``unresolved``.
    """
    query = {**scope, "ts": {"$gte": start, "$lt": end}, field: {"$exists": True}}
    return db.collection(db.READINGS).find_one(query, {"_id": 1}) is not None