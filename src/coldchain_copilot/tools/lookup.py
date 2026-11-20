"""Level 1 lookup tool: one shipment as stored.

``get_shipment`` is usually the LLM's first call. It answers "what is this
shipment?" (reefer, cargo, route, temperature policy, contract) before any
sensor or obligation question, and it turns a reefer id the user mentions
into the shipment that reefer is carrying now.

The document is returned as stored, without ``_id``. Dates stay
``datetime``; ``tools/gateway.py`` serializes them for the LLM.
"""

from __future__ import annotations

from typing import Any

from pymongo import DESCENDING

from coldchain_copilot import db
from coldchain_copilot.errors import ErrorCode, ToolError

Doc = dict[str, Any]

NO_ID = {"_id": 0}
"""Projection that drops ``_id``: an ObjectId means nothing to the LLM."""


def get_shipment(shipment_id: str | None = None, reefer_id: str | None = None) -> Doc:
    """Returns a shipment by its id, or the active shipment of a reefer.

    With ``shipment_id`` the shipment is found directly. With only
    ``reefer_id`` the tool returns the shipment that reefer is carrying now:
    one with no ``deliveredAt``. If several match, which should not happen
    in normal operation, the one that departed last wins. When both ids are
    given, ``shipment_id`` is used.

    Args:
        shipment_id: Shipment id, for example ``"SHP-102"``.
        reefer_id: Reefer id, for example ``"RF-02"``.

    Returns:
        The ``shipments`` document without ``_id``.

    Raises:
        ToolError: ``SCOPE_REQUIRED`` when neither id is given;
            ``NOT_FOUND`` when no shipment matches.
    """
    shipments = db.collection(db.SHIPMENTS)

    if shipment_id:
        shipment = shipments.find_one({"shipmentId": shipment_id}, NO_ID)
        if shipment is None:
            raise ToolError(ErrorCode.NOT_FOUND, f"No shipment with id {shipment_id}.")
        return shipment

    if reefer_id:
        # {"deliveredAt": None} matches both a null and a missing field.
        shipment = shipments.find_one(
            {"reeferId": reefer_id, "deliveredAt": None},
            NO_ID,
            sort=[("departedAt", DESCENDING)],
        )
        if shipment is None:
            raise ToolError(ErrorCode.NOT_FOUND, f"Reefer {reefer_id} has no active shipment.")
        return shipment

    raise ToolError(ErrorCode.SCOPE_REQUIRED, "Provide shipment_id or reefer_id.")