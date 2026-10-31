"""Read the operational ontology and select the clauses that apply to a shipment.

The ontology lives in the ``ontology`` collection (classes, detect rules,
measures, relations, vocabulary) and rule-form clauses live in ``clauses``.
This module answers three questions the evaluator needs before it looks at
any sensor data:

* Which classes does a shipment's cargo belong to? A live-fish shipment is
  also live seafood and seafood, so seafood-wide SOP clauses apply to it.
* Which clauses apply? The shipment's own contract version, plus contract-free
  recommendations (SOP, manual) for any of its cargo classes.
* Which clauses depend on a given clause? Followed through ``references``
  with ``$graphLookup``.
"""

from __future__ import annotations

from typing import Any

from . import db

Doc = dict[str, Any]


def cargo_classes(cargo_type: str) -> list[str]:
    """Returns the cargo class followed by all of its ancestors.

    Ancestors are precomputed on each class document, so this is one indexed
    read by ``_id`` rather than a walk up the hierarchy. For
    ``"cargo:live_fish"`` the result is
    ``["cargo:live_fish", "cargo:live_seafood", "cargo:seafood"]``.

    Raises:
        LookupError: The cargo type is not a class in the ontology. Seeding
            validates this, so it only happens if the data was changed by hand.
    """
    cls = db.collection(db.ONTOLOGY).find_one(
        {"_id": cargo_type, "kind": "class"}, {"ancestors": 1}
    )
    if cls is None:
        raise LookupError(f"{cargo_type} is not an ontology class")
    return [cargo_type, *cls["ancestors"]]


def applicable_clauses_filter(contract_ref: Doc, classes: list[str]) -> Doc:
    """Builds the ``clauses`` filter for one shipment.

    A clause applies when it is approved and either:

    * belongs to the shipment's contract version (``contractId`` and
      ``version`` both match ``contractRef``), or
    * has no contract at all (``contractId: null``, i.e. an SOP or manual
      recommendation) and targets one of the shipment's cargo classes.

    The ``contractId: null`` condition matters. Without it, the class branch
    would also match other shippers' contract clauses for the same cargo, and
    BlueHarbor's obligations would show up on another live-fish shipment.

    Kept separate from :func:`applicable_clauses` so the filter can be unit
    tested without a database.
    """
    return {
        "review.status": "approved",
        "$or": [
            {"contractId": contract_ref["contractId"], "version": contract_ref["version"]},
            {"contractId": None, "appliesTo": {"$in": classes}},
        ],
    }


def applicable_clauses(shipment: Doc) -> list[Doc]:
    """Returns every approved clause that applies to the shipment, ordered by ``clauseId``.

    Uses the ``(contractId, version)`` and ``appliesTo`` indexes created by the
    seed script, one per branch of the ``$or``.
    """
    query = applicable_clauses_filter(
        shipment["contractRef"], cargo_classes(shipment["cargoType"])
    )
    return list(db.collection(db.CLAUSES).find(query, {"_id": 0}).sort("clauseId", 1))


def event_classes(event_types: set[str]) -> dict[str, Doc]:
    """Returns the event class documents, including ``detect``, keyed by ``_id``.

    The evaluator calls this once with every event type the selected clauses
    trigger on, then runs each class's detect rule. Unknown types are simply
    absent from the result.
    """
    cursor = db.collection(db.ONTOLOGY).find({"_id": {"$in": sorted(event_types)}, "kind": "class"})
    return {doc["_id"]: doc for doc in cursor}


def dependent_clauses(clause_id: str, max_depth: int = 2) -> list[Doc]:
    """Returns the clauses that reference ``clause_id``, directly or indirectly.

    ``references`` points from a dependent clause to the clause it relies on
    (BlueHarbor Art. 7 references Art. 6). This walks the edge in reverse with
    ``$graphLookup``: start from the clause id and repeatedly collect clauses
    whose ``references`` array contains an id already found.

    The graph here is shallow (one hop in the seeded data), so ``max_depth``
    bounds the walk rather than shaping the answer. Each result carries
    ``depth``: 0 for direct dependents, 1 for their dependents, and so on.
    """
    pipeline = [
        {"$match": {"clauseId": clause_id}},
        {
            "$graphLookup": {
                "from": db.CLAUSES,
                "startWith": "$clauseId",
                "connectFromField": "clauseId",
                "connectToField": "references",
                "as": "dependents",
                "maxDepth": max_depth,
                "depthField": "depth",
            }
        },
        {"$unwind": "$dependents"},
        {"$replaceRoot": {"newRoot": "$dependents"}},
        {"$project": {"_id": 0}},
        {"$sort": {"depth": 1, "clauseId": 1}},
    ]
    return list(db.collection(db.CLAUSES).aggregate(pipeline))