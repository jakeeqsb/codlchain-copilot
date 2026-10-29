"""Unit tests for coldchain_copilot.catalog.

Each broken-catalog case changes one thing in a copy of the real catalog and
expects exactly the problem that change should cause.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from coldchain_copilot.catalog import (
    Catalog,
    CatalogError,
    chunk_id,
    doc_chunk_ids,
    find_catalog_problems,
    load_catalog,
    parse_frontmatter,
    validate_catalog,
)

ART6 = "blueharbor-live-fish-v1-art6"  # excursion >= 15 consecutive min, notify
ART7 = "blueharbor-live-fish-v1-art7"  # chained to art6 via clauseTriggered
TUNA_ART7 = "oceanpeak-tuna-v1-art7"  # door_opening > 10 min, report


def clause(catalog: Catalog, clause_id: str) -> dict:
    """Returns the clause with ``clause_id`` from ``catalog``."""
    return next(c for c in catalog.clauses if c["clauseId"] == clause_id)


# --------------------------------------------------------------------- real data


def test_real_catalog_has_no_problems(catalog: Catalog, chunk_ids: set[str]) -> None:
    assert find_catalog_problems(catalog, chunk_ids) == []


def test_real_catalog_sizes(catalog: Catalog, chunk_ids: set[str]) -> None:
    # Guards against a data file being emptied or truncated by mistake.
    assert len(catalog.ontology) == 42
    assert len(catalog.clauses) == 15
    assert len(catalog.shipments) == 5
    assert len(catalog.cargo_specs) == 4
    assert len(chunk_ids) == 23


def test_every_clause_chunk_exists_in_docs(catalog: Catalog, chunk_ids: set[str]) -> None:
    assert {c["chunkId"] for c in catalog.clauses} <= chunk_ids


# ----------------------------------------------------------------- broken catalog


def _unknown_measure(c: Catalog) -> None:
    clause(c, ART6)["trigger"]["allOf"][1]["measure"] = "consecutiveMinutez"


def _missing_chunk(c: Catalog) -> None:
    clause(c, ART6)["chunkId"] = "blueharbor-live-fish-v1-article-99"


def _measure_of_other_event(c: Catalog) -> None:
    clause(c, TUNA_ART7)["trigger"]["allOf"][0]["measure"] = "stopMinutes"


def _unknown_class(c: Catalog) -> None:
    clause(c, ART6)["appliesTo"] = ["cargo:salmon"]


def _unknown_action(c: Catalog) -> None:
    clause(c, ART6)["action"]["type"] = "escalate"


def _unknown_party(c: Catalog) -> None:
    clause(c, ART6)["action"]["party"] = "shipper.ceo"


def _unknown_reference(c: Catalog) -> None:
    clause(c, ART7)["references"] = ["blueharbor-live-fish-v1-art99"]


def _unknown_chained_clause(c: Catalog) -> None:
    clause(c, ART7)["trigger"]["clauseTriggered"] = "blueharbor-live-fish-v1-art99"


def _event_without_detect(c: Catalog) -> None:
    # event:incident is an abstract parent class with no detect rule.
    clause(c, TUNA_ART7)["trigger"]["eventType"] = "event:incident"


def _unknown_op(c: Catalog) -> None:
    clause(c, ART6)["trigger"]["allOf"][1]["op"] = "between"


def _contract_without_clauses(c: Catalog) -> None:
    c.shipments[0]["contractRef"]["version"] = "v2"


def _cargo_without_prefix(c: Catalog) -> None:
    c.shipments[0]["cargoType"] = "live_fish"


BROKEN_CASES: list[tuple[str, Callable[[Catalog], None], list[str]]] = [
    ("unknown measure", _unknown_measure,
     [f"{ART6}: unknown measure consecutiveMinutez"]),
    ("missing chunk", _missing_chunk,
     [f"{ART6}: chunkId blueharbor-live-fish-v1-article-99 not found in docs/"]),
    ("measure of another event", _measure_of_other_event,
     [f"{TUNA_ART7}: measure stopMinutes belongs to event:compressor_stop, "
      "not event:door_opening"]),
    ("unknown class", _unknown_class,
     [f"{ART6}: unknown class cargo:salmon"]),
    ("unknown action", _unknown_action,
     [f"{ART6}: unknown action escalate"]),
    ("unknown party", _unknown_party,
     [f"{ART6}: unknown party shipper.ceo"]),
    ("unknown reference", _unknown_reference,
     [f"{ART7}: references unknown clause blueharbor-live-fish-v1-art99"]),
    ("unknown chained clause", _unknown_chained_clause,
     [f"{ART7}: depends on unknown clause blueharbor-live-fish-v1-art99"]),
    ("event without detect", _event_without_detect,
     # The measure check also fires: doorOpenMinutes no longer matches the event.
     [f"{TUNA_ART7}: event event:incident has no detection rule",
      f"{TUNA_ART7}: measure doorOpenMinutes belongs to event:door_opening, "
      "not event:incident"]),
    ("unknown op", _unknown_op,
     [f"{ART6}: unknown op between"]),
    ("contract without clauses", _contract_without_clauses,
     ["SHP-101: contract blueharbor-live-fish v2 has no clauses"]),
    ("cargoType without prefix", _cargo_without_prefix,
     ["SHP-101: cargoType live_fish is not an ontology class",
      "SHP-101: no cargo spec for live_fish"]),
]


@pytest.mark.parametrize(
    ("break_catalog", "expected"),
    [pytest.param(fn, exp, id=name) for name, fn, exp in BROKEN_CASES],
)
def test_broken_catalog_reports_exact_problems(
    catalog: Catalog,
    chunk_ids: set[str],
    break_catalog: Callable[[Catalog], None],
    expected: list[str],
) -> None:
    break_catalog(catalog)
    assert find_catalog_problems(catalog, chunk_ids) == expected


def test_validate_raises_with_every_problem(catalog: Catalog, chunk_ids: set[str]) -> None:
    _unknown_measure(catalog)
    _cargo_without_prefix(catalog)

    with pytest.raises(CatalogError) as exc_info:
        validate_catalog(catalog, chunk_ids)

    assert len(exc_info.value.problems) == 3
    assert "unknown measure consecutiveMinutez" in str(exc_info.value)


def test_validate_passes_on_real_catalog(catalog: Catalog, chunk_ids: set[str]) -> None:
    validate_catalog(catalog, chunk_ids)  # does not raise


# ------------------------------------------------------------------- chunk ids


def test_chunk_id_format() -> None:
    assert chunk_id("blueharbor-live-fish", "v1", "Article", 6) == (
        "blueharbor-live-fish-v1-article-6"
    )
    assert chunk_id("sop-excursion-response", "v1", "Section", "2") == (
        "sop-excursion-response-v1-section-2"
    )


def test_parse_frontmatter() -> None:
    text = "---\ndocId: demo\nversion: v1\nappliesTo: cargo:seafood\n---\n# Title\n"
    assert parse_frontmatter(text) == {
        "docId": "demo",
        "version": "v1",
        "appliesTo": "cargo:seafood",
    }


def test_parse_frontmatter_requires_block() -> None:
    with pytest.raises(ValueError):
        parse_frontmatter("# No frontmatter\n")


def test_doc_chunk_ids_reads_article_and_section_headings(tmp_path: Path) -> None:
    (tmp_path / "contract.md").write_text(
        "---\ndocId: acme\nversion: v2\n---\n"
        "## Article 1: Scope\ntext\n"
        "## Article 12: Notice\ntext\n"
        "### Article 3: not a unit heading\n",
        encoding="utf-8",
    )
    (tmp_path / "sop.md").write_text(
        "---\ndocId: sop\nversion: v1\n---\n## Section 4: Report\ntext\n",
        encoding="utf-8",
    )
    assert doc_chunk_ids(tmp_path) == {
        "acme-v2-article-1",
        "acme-v2-article-12",
        "sop-v1-section-4",
    }


def test_doc_chunk_ids_rejects_missing_doc_id(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_text("---\nversion: v1\n---\n## Article 1: X\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bad.md"):
        doc_chunk_ids(tmp_path)


# --------------------------------------------------------------------- loading


def test_load_catalog_reads_given_directory(tmp_path: Path) -> None:
    for name in ("ontology", "clauses", "shipments", "cargo_specs"):
        (tmp_path / f"{name}.json").write_text(f'[{{"file": "{name}"}}]', encoding="utf-8")

    loaded = load_catalog(tmp_path)

    assert loaded.ontology == [{"file": "ontology"}]
    assert loaded.cargo_specs == [{"file": "cargo_specs"}]


def test_load_catalog_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_catalog(tmp_path)
