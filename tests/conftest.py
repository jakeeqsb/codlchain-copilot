"""Shared pytest fixtures.

Paths come from pytest's ``rootpath`` (the directory holding pyproject.toml),
so tests never read ``.env`` for file locations.

Unit tests use the catalog fixtures and need no database. Integration tests
request ``mongo_db``; it skips the test when MongoDB is not configured or not
reachable.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from coldchain_copilot.catalog import Catalog, doc_chunk_ids, load_catalog


@pytest.fixture(scope="session")
def project_root(request: pytest.FixtureRequest) -> Path:
    """Returns the repository root as determined by pytest."""
    return Path(request.config.rootpath)


@pytest.fixture(scope="session")
def data_dir(project_root: Path) -> Path:
    """Returns the directory of the catalog JSON files."""
    return project_root / "data"


@pytest.fixture(scope="session")
def docs_dir(project_root: Path) -> Path:
    """Returns the directory of the source Markdown documents."""
    return project_root / "docs"


@pytest.fixture(scope="session")
def chunk_ids(docs_dir: Path) -> set[str]:
    """Returns every chunk id the source documents produce."""
    return doc_chunk_ids(docs_dir)


@pytest.fixture(scope="session")
def _catalog_once(data_dir: Path) -> Catalog:
    """Loads the catalog once per session. Use ``catalog`` in tests."""
    return load_catalog(data_dir)


@pytest.fixture
def catalog(_catalog_once: Catalog) -> Catalog:
    """Returns a deep copy of the real catalog that a test may modify freely."""
    return copy.deepcopy(_catalog_once)


@pytest.fixture(scope="session")
def mongo_db():
    """Returns the project database, or skips the test if it is unavailable.

    Reads MONGODB_URI and DB_NAME from the environment (and ``.env``).
    """
    from pymongo.errors import PyMongoError

    from coldchain_copilot.config import ConfigError
    from coldchain_copilot.db import get_client, get_db

    try:
        get_client().admin.command("ping")
    except ConfigError as exc:
        pytest.skip(f"MongoDB not configured: {exc}")
    except PyMongoError as exc:
        pytest.skip(f"MongoDB not reachable: {exc}")
    return get_db()
