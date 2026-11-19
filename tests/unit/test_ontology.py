"""Unit tests for coldchain_copilot.ontology.

The database-backed functions are checked against seeded data in the
integration tests. Here the clause filter is checked on its own, and then
applied in plain Python to the real catalog to confirm it selects the clauses
design/ontology.md expects.
"""

from __future__ import annotations

from coldchain_copilot.catalog import Catalog
from coldchain_copilot.ontology import applicable_clauses_filter

BLUEHARBOR = {"contractId": "blueharbor-live-fish", "version": "v1"}
LIVE_FISH = ["cargo:live_fish", "cargo:live_seafood", "cargo:seafood"]


def matches(clause: dict, query: dict) -> bool:
    """Evaluates the filter shape produced by applicable_clauses_filter in Python.

    Supports only what that filter uses: equality, ``None``, ``$in`` on an
    array field, one ``$or``, and the dotted ``review.status`` path.
    """

    def field(doc: dict, path: str):
        for part in path.split("."):
            doc = doc.get(part) if isinstance(doc, dict) else None
        return doc

    def match_one(cond: dict) -> bool:
        for path, expected in cond.items():
            actual = field(clause, path)
            if isinstance(expected, dict) and "$in" in expected:
                if not set(actual or []) & set(expected["$in"]):
                    return False
            elif actual != expected:
                return False
        return True

    rest = {k: v for k, v in query.items() if k != "$or"}
    return match_one(rest) and any(match_one(branch) for branch in query["$or"])


def test_filter_shape() -> None:
    assert applicable_clauses_filter(BLUEHARBOR, LIVE_FISH) == {
        "review.status": "approved",
        "$or": [
            {"contractId": "blueharbor-live-fish", "version": "v1"},
            {"contractId": None, "appliesTo": {"$in": LIVE_FISH}},
        ],
    }


def test_blueharbor_live_fish_gets_its_contract_and_seafood_recommendations(
    catalog: Catalog,
) -> None:
    query = applicable_clauses_filter(BLUEHARBOR, LIVE_FISH)
    selected = sorted(c["clauseId"] for c in catalog.clauses if matches(c, query))

    assert selected == [
        "blueharbor-live-fish-v1-art6",
        "blueharbor-live-fish-v1-art7",
        "blueharbor-live-fish-v1-art8",
        "manual-reefer-maintenance-v1-sec2",
        "manual-reefer-maintenance-v1-sec3",
        "manual-reefer-maintenance-v1-sec4",
        "sop-excursion-response-v1-sec2",
        "sop-excursion-response-v1-sec3",
        "sop-excursion-response-v1-sec4",
    ]


def test_other_shippers_clauses_for_same_class_are_excluded(catalog: Catalog) -> None:
    # Another live-fish contract must not pick up BlueHarbor's clauses through
    # the class branch. That is what contractId: null prevents.
    other = {"contractId": "someone-else-live-fish", "version": "v1"}
    query = applicable_clauses_filter(other, LIVE_FISH)
    selected = {c["clauseId"] for c in catalog.clauses if matches(c, query)}

    assert not any(cid.startswith("blueharbor") for cid in selected)
    assert all(cid.startswith(("sop-", "manual-")) for cid in selected)


def test_contract_version_must_match(catalog: Catalog) -> None:
    query = applicable_clauses_filter({**BLUEHARBOR, "version": "v2"}, LIVE_FISH)
    selected = {c["clauseId"] for c in catalog.clauses if matches(c, query)}

    assert not any(cid.startswith("blueharbor") for cid in selected)


def test_unapproved_clause_is_excluded(catalog: Catalog) -> None:
    art6 = next(c for c in catalog.clauses if c["clauseId"] == "blueharbor-live-fish-v1-art6")
    art6["review"]["status"] = "draft"
    query = applicable_clauses_filter(BLUEHARBOR, LIVE_FISH)

    assert not matches(art6, query)


def test_every_shipment_gets_its_own_contract_clauses(catalog: Catalog) -> None:
    ancestors = {d["_id"]: d["ancestors"] for d in catalog.ontology if d["kind"] == "class"}
    for shipment in catalog.shipments:
        classes = [shipment["cargoType"], *ancestors[shipment["cargoType"]]]
        query = applicable_clauses_filter(shipment["contractRef"], classes)
        contract_ids = {c["contractId"] for c in catalog.clauses if matches(c, query)}

        assert contract_ids == {shipment["contractRef"]["contractId"], None}