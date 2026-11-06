"""Names used in the ontology → the functions that do the work.

The ontology never contains logic. An event type names its ``detector``, a
link type that needs traversal names its ``traverse`` function, and an action
type names its ``handler``. This module is the single place where those names
meet code, so swapping an implementation (for example, following
``rel:references`` in a graph database instead of with ``$graphLookup``)
means registering a different function under the same name.

``catalog.py`` checks before seeding that every name the ontology uses is
registered here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import detection, ontology

Doc = dict[str, Any]

DETECTORS: dict[str, Callable[[Doc, datetime, datetime], list[Doc]]] = {
    "detect_temperature_excursion": detection.detect_temperature_excursion,
    "detect_door_opening": detection.detect_door_opening,
    "detect_compressor_stop": detection.detect_compressor_stop,
    "detect_humidity_breach": detection.detect_humidity_breach,
    "detect_power_anomaly": detection.detect_power_anomaly,
}
"""Event type ``detector`` names. Signature: ``(scope, start, end) -> events``."""

TRAVERSERS: dict[str, Callable[[str], list[Doc]]] = {
    "follow_references": ontology.dependent_clauses,
}
"""Link type ``traverse`` names. Signature: ``(clause_id) -> clauses``."""


def briefing_item(result: Doc) -> Doc:
    """Turns a triggered clause result into one line of the briefing.

    The MVP does not send anything. Every action type maps to this handler,
    which records who must do what by when, and on what evidence. A real
    deployment would register, say, an email sender under ``notify``.
    """
    return {
        "action": result["action"]["type"],
        "party": result["action"]["party"],
        "deadline": result["deadline"],
        "clauseId": result["clauseId"],
        "kind": result["kind"],
        "evidence": result["chunkId"],
        "event": result["event"],
    }


ACTIONS: dict[str, Callable[[Doc], Doc]] = {
    name: briefing_item for name in ("notify", "inspect", "hold", "report", "maintain", "retrain")
}
"""Action type ``handler`` names. Signature: ``(clause result) -> briefing item``."""