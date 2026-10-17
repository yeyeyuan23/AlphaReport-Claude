"""Shared read/prepare endpoints for reproduction cards."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from reportagent.db.engine import get_db
from reportagent.db.repository import ReportRepository
from reportagent.services.reproduction import prepare_report, saved_preparation

router = APIRouter(prefix="/api/v1/reports", tags=["reproduction"])


@router.get("/{report_id}/reproduction-card")
def read_card(report_id: int, db: Session = Depends(get_db)):
    report = ReportRepository(db).get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"success": True, "data": saved_preparation(report)}


@router.post("/{report_id}/reproduction-card")
async def prepare_card(
    report_id: int, force: bool = Query(False), db: Session = Depends(get_db)
):
    try:
        return {
            "success": True,
            "data": await prepare_report(report_id, db, force=force),
        }
    except LookupError:
        raise HTTPException(status_code=404, detail="Report not found")
