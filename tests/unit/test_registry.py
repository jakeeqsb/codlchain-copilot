"""Unit tests for coldchain_copilot.registry: ontology names ↔ functions."""

from __future__ import annotations

import inspect

from coldchain_copilot import registry
from coldchain_copilot.catalog import Catalog


def of_kind(catalog: Catalog, kind: str) -> list[dict]:
    return [d for d in catalog.ontology if d["kind"] == kind]


def test_every_ontology_name_is_registered(catalog: Catalog) -> None:
    assert {e["detector"] for e in of_kind(catalog, "eventType")} <= set(registry.DETECTORS)
    assert {a["handler"] for a in of_kind(catalog, "actionType")} <= set(registry.ACTIONS)
    assert {
        link["traverse"] for link in of_kind(catalog, "linkType") if "traverse" in link
    } <= set(registry.TRAVERSERS)


def test_no_unused_detectors(catalog: Catalog) -> None:
    # A registered detector nobody names is dead code or a typo in the ontology.
    assert set(registry.DETECTORS) == {e["detector"] for e in of_kind(catalog, "eventType")}


def test_detectors_share_one_signature() -> None:
    for name, fn in registry.DETECTORS.items():
        assert list(inspect.signature(fn).parameters) == ["scope", "start", "end"], name


def test_briefing_item_shape() -> None:
    result = {
        "clauseId": "blueharbor-live-fish-v1-art6",
        "kind": "obligation",
        "status": "triggered",
        "action": {"type": "notify", "party": "shipper.qa"},
        "deadline": "2026-10-30T02:00:00Z",
        "chunkId": "blueharbor-live-fish-v1-article-6",
        "event": {"eventType": "event:temperature_excursion"},
    }
    assert registry.ACTIONS["notify"](result) == {
        "action": "notify",
        "party": "shipper.qa",
        "deadline": "2026-10-30T02:00:00Z",
        "clauseId": "blueharbor-live-fish-v1-art6",
        "kind": "obligation",
        "evidence": "blueharbor-live-fish-v1-article-6",
        "event": {"eventType": "event:temperature_excursion"},
    }