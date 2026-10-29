"""MongoDB client and collection handles.

The process keeps one MongoClient: it owns the connection pool and is
thread-safe, so every module shares it through :func:`get_client`.

Typical usage:

    from coldchain_copilot import db

    shipment = db.collection(db.SHIPMENTS).find_one({"shipmentId": "SHP-102"})
"""

from __future__ import annotations

from functools import lru_cache

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from .config import get_settings

SHIPMENTS = "shipments"
"""Shipments with resolved policy, period, and contract reference."""
READINGS = "readings"
"""Time Series collection of per-minute sensor readings."""
KNOWLEDGE = "knowledge"
"""Document chunks with embeddings, searched with ``$vectorSearch``."""
CLAUSES = "clauses"
"""Rule-form clauses evaluated by ``evaluate_obligations``."""
ONTOLOGY = "ontology"
"""Classes, detect rules, measures, relations, and vocabulary."""
CARGO_SPECS = "cargo_specs"
"""Default temperature policy per cargo class."""

ALL_COLLECTIONS = (SHIPMENTS, READINGS, KNOWLEDGE, CLAUSES, ONTOLOGY, CARGO_SPECS)


@lru_cache(maxsize=1)
def get_client() -> MongoClient:
    """Returns the shared MongoClient, creating it on first call.

    Datetimes come back timezone-aware in UTC, which detection and deadline
    arithmetic rely on. Server selection fails after 5 seconds so a missing
    database surfaces quickly.

    Raises:
        ConfigError: ``MONGODB_URI`` is not set.
    """
    settings = get_settings()
    return MongoClient(
        settings.mongodb_uri,
        appname="coldchain-copilot",
        tz_aware=True,
        serverSelectionTimeoutMS=5000,
    )


def get_db() -> Database:
    """Returns the project database named by ``DB_NAME``."""
    return get_client()[get_settings().db_name]


def collection(name: str) -> Collection:
    """Returns a handle to one project collection.

    Args:
        name: One of the names in :data:`ALL_COLLECTIONS`.

    Returns:
        The collection handle. No request is sent until it is used.

    Raises:
        ValueError: ``name`` is not a project collection.
    """
    if name not in ALL_COLLECTIONS:
        raise ValueError(f"Unknown collection {name!r}; expected one of {ALL_COLLECTIONS}.")
    return get_db()[name]