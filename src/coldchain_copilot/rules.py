"""Decide each clause from detected events: triggered, not_triggered, or unresolved.

This is the step that RAG alone cannot make reliable. A clause in ``clauses``
already states its condition as data (``consecutiveMinutes gte 15``), and
detection already produced the measured value (``consecutiveMinutes: 24``),
so deciding is a comparison, not a reading of contract prose. The same inputs
always give the same answer.

Everything here is plain Python over dictionaries. No database access: the
caller (``tools/obligations.py``) selects clauses, runs detection, and passes
the results in. That keeps every rule in design/ontology.md §5 testable on its
own.

Statuses:

``triggered``
    At least one event of the clause's type satisfies every condition.
``not_triggered``
    Events exist but none satisfies all conditions (the closest one is
    reported), or there is no event and the required data was collected.
``unresolved``
    The required data was never collected, or an event lacks a measure the
    clause needs. Never reported as "no obligation".

A chained clause (``trigger.clauseTriggered``) takes the status of the
clause it depends on.
"""

from __future__ import annotations

import operator
from datetime import datetime, timedelta
from typing import Any

Doc = dict[str, Any]

OPS = {
    "eq": operator.eq,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}
"""Comparison operators a clause condition may use (same set catalog.py allows)."""


def check_conditions(conditions: list[Doc], measures: Doc) -> list[Doc]:
    """Compares one event's measures with a clause's ``allOf`` conditions.

    Returns one basis entry per condition, in the clause's order::

        {"measure": "consecutiveMinutes", "op": "gte", "required": 15,
         "actual": 24, "met": True}

    ``actual`` is ``None`` and ``met`` is ``None`` when the event has no value
    for that measure; the caller treats that as ``unresolved`` rather than as
    a failed comparison.
    """
    basis = []
    for cond in conditions:
        actual = measures.get(cond["measure"])
        met = None if actual is None else OPS[cond["op"]](actual, cond["value"])
        basis.append(
            {
                "measure": cond["measure"],
                "op": cond["op"],
                "required": cond["value"],
                "actual": actual,
                "met": met,
            }
        )
    return basis


def deadline(clause: Doc, event: Doc | None) -> datetime | None:
    """Computes the clause deadline from the event that triggered it.

    ``deadline.anchor`` is ``eventStart`` or ``eventEnd``, and
    ``offsetMinutes`` is added to that event time. BlueHarbor Art. 6 ("notify
    within 1 hour of the start of the excursion") is ``eventStart + 60``.

    Returns ``None`` when the clause has no deadline or there is no event.
    """
    spec = clause.get("deadline")
    if not spec or event is None:
        return None
    anchor = event["start"] if spec["anchor"] == "eventStart" else event["end"]
    return anchor + timedelta(minutes=spec["offsetMinutes"])


def evaluate_trigger(clause: Doc, events: list[Doc], data_collected: bool) -> Doc:
    """Decides one event-based clause.

    Args:
        clause: A clause whose trigger has ``eventType`` and ``allOf``.
        events: Detected events of that type, in time order.
        data_collected: Whether the fields the event type requires were
            collected at all in the evaluated period (``requiresFields``).
            Without them, "no event" cannot be trusted.

    Returns:
        A result with ``status``, ``basis`` (the comparisons for the matched
        or closest event), ``event`` (that event, or ``None``), and
        ``deadline``. The earliest event that satisfies every condition is
        the one reported, because deadlines run from the first occurrence.
    """
    conditions = clause["trigger"]["allOf"]

    if not data_collected:
        return _result(
            clause,
            "unresolved",
            basis=[{"missingData": clause["trigger"]["eventType"]}],
        )
    if not events:
        return _result(clause, "not_triggered", basis=[{"noEvent": clause["trigger"]["eventType"]}])

    checked = [(event, check_conditions(conditions, event["measures"])) for event in events]

    for event, basis in checked:
        if all(b["met"] is True for b in basis):
            return _result(clause, "triggered", basis=basis, event=event)

    for event, basis in checked:
        if any(b["met"] is None for b in basis):
            missing = [b["measure"] for b in basis if b["met"] is None]
            return _result(clause, "unresolved", basis=[{"missingMeasure": m} for m in missing], event=event)

    # Closest: the event meeting the most conditions; ties go to the earliest.
    event, basis = max(checked, key=lambda pair: sum(b["met"] for b in pair[1]))
    return _result(clause, "not_triggered", basis=basis, event=event)


def evaluate_clauses(
    clauses: list[Doc],
    events_by_type: dict[str, list[Doc]],
    collected_by_type: dict[str, bool],
) -> list[Doc]:
    """Decides every selected clause, including chained ones.

    Event-based clauses are decided first with :func:`evaluate_trigger`.
    A chained clause (``trigger.clauseTriggered``) then copies the status of
    the clause it depends on, and its basis records the dependency. Chains
    are resolved repeatedly so a clause that depends on another chained
    clause also gets a status. A dependency outside the selected clauses is
    ``unresolved``.

    Args:
        clauses: Clauses selected for one shipment.
        events_by_type: Detected events keyed by event class id.
        collected_by_type: ``requiresFields`` check per event class id.

    Returns:
        One result per clause, in the same order as ``clauses``.
    """
    results: dict[str, Doc] = {}
    for clause in clauses:
        event_type = clause["trigger"].get("eventType")
        if event_type:
            results[clause["clauseId"]] = evaluate_trigger(
                clause,
                events_by_type.get(event_type, []),
                collected_by_type.get(event_type, False),
            )

    # Chained clauses: decide any whose parent is decided, and repeat. Each
    # pass decides at least one clause unless the rest form a cycle or depend
    # on a clause that was not selected; those end up unresolved.
    pending = [c for c in clauses if "clauseTriggered" in c["trigger"]]
    for _ in range(len(pending)):
        still_pending = []
        for clause in pending:
            parent = results.get(clause["trigger"]["clauseTriggered"])
            if parent is None:
                still_pending.append(clause)
                continue
            results[clause["clauseId"]] = _result(
                clause,
                parent["status"],
                basis=[{"dependsOn": clause["trigger"]["clauseTriggered"]}],
                event=parent["event"],
            )
        pending = still_pending
    for clause in pending:
        results[clause["clauseId"]] = _result(
            clause, "unresolved", basis=[{"dependsOn": clause["trigger"]["clauseTriggered"]}]
        )

    return [results[c["clauseId"]] for c in clauses]


def _result(clause: Doc, status: str, basis: list[Doc], event: Doc | None = None) -> Doc:
    """Builds one result in the shape design/interfaces.md shows for evaluate_obligations."""
    return {
        "clauseId": clause["clauseId"],
        "kind": clause["kind"],
        "status": status,
        "action": clause["action"],
        "deadline": deadline(clause, event) if status == "triggered" else None,
        "basis": basis,
        "event": None
        if event is None
        else {k: event[k] for k in ("eventType", "reeferId", "start", "end") if k in event},
        "chunkId": clause["chunkId"],
    }