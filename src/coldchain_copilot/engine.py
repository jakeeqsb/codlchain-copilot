"""The execution engine: evaluate every applicable clause for one shipment.

The engine reads names and connections from the ontology and runs the
functions ``registry.py`` maps them to. For one shipment it takes six steps:

1. Read the shipment object and fix its period (departure to delivery, or
   to now while in transit).
2. Select applicable clauses: its contract version plus contract-free
   recommendations for its cargo class and ancestors (``ontology.py``).
3. For every event type those clauses wait on, look up the event type's
   ``detector`` and run it over the shipment period. Also check whether the
   fields it requires were collected at all.
4. Compare each clause's conditions with the detected measures (``rules.py``).
5. For each triggered clause, follow ``rel:references`` with the link type's
   ``traverse`` function to list the clauses that build on it.
6. For each triggered clause, run its action type's ``handler`` to produce a
   briefing item.

Nothing is stored; the result is returned to the caller (a tool, the agent).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import db, detection, ontology, registry, rules
from .errors import ErrorCode, ToolError

Doc = dict[str, Any]

REFERENCES = "rel:references"


def evaluate_shipment(shipment_id: str, now: datetime | None = None) -> Doc:
    """Runs the six steps for one shipment and returns every clause's result.

    Args:
        shipment_id: For example ``"SHP-102"``.
        now: End of the period for a shipment still in transit. Defaults to
            the current time; tests pass a fixed value.

    Returns:
        ``shipmentId``, ``contractRef``, ``period`` (``start``/``end``),
        ``results`` (one per applicable clause, from ``rules.py``, each with a
        ``follows`` list of dependent clause ids when triggered), and
        ``actions`` (one briefing item per triggered clause).

    Raises:
        ToolError: ``NOT_FOUND`` if the shipment does not exist.
    """
    # 1. Object and period
    shipment = db.collection(db.SHIPMENTS).find_one({"shipmentId": shipment_id}, {"_id": 0})
    if shipment is None:
        raise ToolError(ErrorCode.NOT_FOUND, f"Shipment {shipment_id} does not exist.")
    start = shipment["departedAt"]
    end = shipment.get("deliveredAt") or now or datetime.now(timezone.utc)
    scope = {"meta.shipmentId": shipment_id}

    # 2. Applicable clauses, and the ontology entries they use
    clauses = ontology.applicable_clauses(shipment)
    event_ids = {c["trigger"]["eventType"] for c in clauses if "eventType" in c["trigger"]}
    action_ids = {f"action:{c['action']['type']}" for c in clauses}
    defs = ontology.ontology_docs(event_ids | action_ids | {REFERENCES})

    # 3. Detect events through each event type's detector
    events_by_type: dict[str, list[Doc]] = {}
    collected_by_type: dict[str, bool] = {}
    for event_id in sorted(event_ids):
        event_type = defs[event_id]
        detector = registry.DETECTORS[event_type["detector"]]
        events_by_type[event_id] = detector(scope, start, end)
        collected_by_type[event_id] = all(
            detection.field_collected(field, scope, start, end) for field in event_type["requiresFields"]
        )

    # 4. Compare conditions
    results = rules.evaluate_clauses(clauses, events_by_type, collected_by_type)

    # 5. Follow references from triggered clauses
    traverse = registry.TRAVERSERS[defs[REFERENCES]["traverse"]]
    for result in results:
        if result["status"] == "triggered":
            result["follows"] = [c["clauseId"] for c in traverse(result["clauseId"])]

    # 6. Run each triggered clause's action handler
    actions = []
    for result in results:
        if result["status"] == "triggered":
            handler = registry.ACTIONS[defs[f"action:{result['action']['type']}"]["handler"]]
            actions.append(handler(result))

    return {
        "shipmentId": shipment_id,
        "contractRef": shipment["contractRef"],
        "period": {"start": start, "end": end},
        "results": results,
        "actions": actions,
    }