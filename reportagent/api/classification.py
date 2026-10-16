from __future__ import annotations

from fastapi import APIRouter

from reportagent.classifiers.taxonomy import load_taxonomy

router = APIRouter(prefix="/api/v1/classification", tags=["classification"])


@router.get("/taxonomy")
async def get_taxonomy():
    taxonomy = load_taxonomy()
    return {"success": True, "data": {"taxonomy": taxonomy}}
