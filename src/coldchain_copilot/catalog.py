"""Load and validate the rule catalog before it reaches MongoDB.

The catalog is the hand-written data under ``data/``: ontology documents,
rule-form clauses, shipments, and cargo specs. Clauses only work if every name
they use is defined in the ontology and every clause points to real source
text in ``docs/``. This module checks those links so a typo fails at seed time
instead of silently producing ``not_triggered`` at evaluation time.

Paths are passed in by the caller; this module does not guess where the
repository lives. Scripts take them from ``DATA_DIR`` and ``DOCS_DIR`` in
``.env`` (see config.Settings); tests pass their own.

Typical usage:

    settings = get_settings()
    catalog = load_catalog(settings.data_dir)
    validate_catalog(catalog, doc_chunk_ids(settings.docs_dir))
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

COMPARISON_OPS = frozenset({"eq", "gt", "gte", "lt", "lte"})
"""Operators a clause condition may use (see design/ontology.md §4)."""

_FRONTMATTER_LINE = re.compile(r"^(\w+):\s*(.+)$", re.MULTILINE)
_UNIT_HEADING = re.compile(r"^## (Article|Section) (\d+):", re.MULTILINE)

Doc = dict[str, Any]


class CatalogError(ValueError):
    """The catalog has one or more broken references.

    Attributes:
        problems: One human-readable line per problem, in discovery order.
    """

    def __init__(self, problems: list[str]) -> None:
        super().__init__("Catalog validation failed:\n  " + "\n  ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Catalog:
    """The four hand-written data files, as loaded from JSON.

    Attributes:
        ontology: Classes, measures, relations, actions, and parties.
        clauses: Rule-form clauses from contracts, the SOP, and the manual.
        shipments: Shipments before policy resolution (no dates, no policy).
        cargo_specs: Default temperature policy per cargo class.
    """

    ontology: list[Doc]
    clauses: list[Doc]
    shipments: list[Doc]
    cargo_specs: list[Doc]


def load_catalog(data_dir: Path) -> Catalog:
    """Reads the catalog JSON files.

    Args:
        data_dir: Directory holding ``ontology.json``, ``clauses.json``,
            ``shipments.json``, and ``cargo_specs.json``.

    Returns:
        The loaded catalog. Nothing is validated yet.

    Raises:
        FileNotFoundError: A data file is missing.
        json.JSONDecodeError: A data file is not valid JSON.
    """

    def read(name: str) -> list[Doc]:
        return json.loads((data_dir / name).read_text(encoding="utf-8"))

    return Catalog(
        ontology=read("ontology.json"),
        clauses=read("clauses.json"),
        shipments=read("shipments.json"),
        cargo_specs=read("cargo_specs.json"),
    )


def chunk_id(doc_id: str, version: str, unit: str, number: int | str) -> str:
    """Builds the id of one Article or Section chunk.

    The same id links a clause (``clauses.chunkId``) to its source text
    (``knowledge.chunkId``), so seeding and ingestion must agree on it.

    Args:
        doc_id: The document's ``docId`` from its frontmatter.
        version: The document's ``version`` from its frontmatter.
        unit: ``"Article"`` or ``"Section"`` (case-insensitive).
        number: The heading number.

    Returns:
        An id such as ``"blueharbor-live-fish-v1-article-6"``.
    """
    return f"{doc_id}-{version}-{unit.lower()}-{number}"


def parse_frontmatter(text: str) -> dict[str, str]:
    """Extracts ``key: value`` pairs from a Markdown file's frontmatter.

    Only flat scalar keys are read; that is all the docs use.

    Args:
        text: Full Markdown text starting with a ``---`` block.

    Returns:
        Frontmatter keys mapped to their raw string values.

    Raises:
        ValueError: The text has no ``---`` frontmatter block.
    """
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        raise ValueError("document has no --- frontmatter block")
    return dict(_FRONTMATTER_LINE.findall(parts[1]))


def doc_chunk_ids(docs_dir: Path) -> set[str]:
    """Lists every chunk id the source documents will produce.

    Args:
        docs_dir: Directory of Markdown documents.

    Returns:
        One id per ``## Article N:`` or ``## Section N:`` heading.

    Raises:
        ValueError: A document lacks frontmatter, ``docId``, or ``version``.
    """
    ids: set[str] = set()
    for path in sorted(docs_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        try:
            front = parse_frontmatter(text)
            doc_id, version = front["docId"], front["version"]
        except (ValueError, KeyError) as exc:
            raise ValueError(f"{path.name}: missing frontmatter field ({exc})") from None
        for unit, number in _UNIT_HEADING.findall(text):
            ids.add(chunk_id(doc_id, version, unit, number))
    return ids


def find_catalog_problems(catalog: Catalog, chunk_ids: set[str]) -> list[str]:
    """Checks every cross-reference in the catalog.

    Clause checks:
        * ``chunkId`` exists in the source documents.
        * ``appliesTo`` classes, ``action.type``, and ``action.party`` are
          defined in the ontology.
        * ``references`` and ``clauseTriggered`` name existing clauses.
        * The trigger's event class has a ``detect`` rule.
        * Each condition's measure exists, belongs to the trigger's event
          class, and uses an allowed operator.

    Shipment checks:
        * Its contract version has at least one clause.
        * ``cargoType`` is an ontology class and has a cargo spec.

    Args:
        catalog: The loaded catalog.
        chunk_ids: Chunk ids available in the source documents, usually
            from :func:`doc_chunk_ids`.

    Returns:
        Problem descriptions; empty when the catalog is consistent.
    """
    by_id = {d["_id"]: d for d in catalog.ontology}

    def names(kind: str) -> dict[str, Doc]:
        # "measure:stopMinutes" -> "stopMinutes"; clauses use the short name.
        return {i.split(":", 1)[1]: d for i, d in by_id.items() if d["kind"] == kind}

    classes = {i for i, d in by_id.items() if d["kind"] == "class"}
    measures = names("measure")
    actions = names("action")
    parties = names("party")
    clause_ids = {c["clauseId"] for c in catalog.clauses}
    problems: list[str] = []

    for clause in catalog.clauses:
        cid, trigger, action = clause["clauseId"], clause["trigger"], clause["action"]
        if clause["chunkId"] not in chunk_ids:
            problems.append(f"{cid}: chunkId {clause['chunkId']} not found in docs/")
        for cls in clause["appliesTo"]:
            if cls not in classes:
                problems.append(f"{cid}: unknown class {cls}")
        if action["type"] not in actions:
            problems.append(f"{cid}: unknown action {action['type']}")
        if action["party"] not in parties:
            problems.append(f"{cid}: unknown party {action['party']}")
        for ref in clause.get("references", []):
            if ref not in clause_ids:
                problems.append(f"{cid}: references unknown clause {ref}")

        if "clauseTriggered" in trigger:
            # Chained clause: fires when another clause fires; no conditions of its own.
            if trigger["clauseTriggered"] not in clause_ids:
                problems.append(f"{cid}: depends on unknown clause {trigger['clauseTriggered']}")
            continue

        event_type = trigger["eventType"]
        if "detect" not in by_id.get(event_type, {}):
            problems.append(f"{cid}: event {event_type} has no detection rule")
        for cond in trigger["allOf"]:
            measure = measures.get(cond["measure"])
            if measure is None:
                problems.append(f"{cid}: unknown measure {cond['measure']}")
            elif measure["eventType"] != event_type:
                problems.append(
                    f"{cid}: measure {cond['measure']} belongs to "
                    f"{measure['eventType']}, not {event_type}"
                )
            if cond["op"] not in COMPARISON_OPS:
                problems.append(f"{cid}: unknown op {cond['op']}")

    contracts = {(c["contractId"], c["version"]) for c in catalog.clauses if c["contractId"]}
    specs = {s["cargoType"] for s in catalog.cargo_specs}
    for shipment in catalog.shipments:
        sid, ref = shipment["shipmentId"], shipment["contractRef"]
        if (ref["contractId"], ref["version"]) not in contracts:
            problems.append(f"{sid}: contract {ref['contractId']} {ref['version']} has no clauses")
        if shipment["cargoType"] not in classes:
            problems.append(f"{sid}: cargoType {shipment['cargoType']} is not an ontology class")
        if shipment["cargoType"] not in specs:
            problems.append(f"{sid}: no cargo spec for {shipment['cargoType']}")

    return problems


def validate_catalog(catalog: Catalog, chunk_ids: set[str]) -> None:
    """Raises if the catalog has any broken reference.

    Args:
        catalog: The loaded catalog.
        chunk_ids: Chunk ids available in the source documents, usually
            from :func:`doc_chunk_ids`.

    Raises:
        CatalogError: With every problem found, not just the first.
    """
    problems = find_catalog_problems(catalog, chunk_ids)
    if problems:
        raise CatalogError(problems)