from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from reportagent.api.deps import get_db
from reportagent.models.schemas import UserCriteria

router = APIRouter(prefix="/api/v1/collection", tags=["collection"])

_tasks: dict[str, dict] = {}


def _run_pipeline_in_thread(task_id: str, criteria: UserCriteria):
    """Run the async pipeline in a dedicated thread with its own event loop."""
    _tasks[task_id]["status"] = "running"
    _tasks[task_id]["progress_message"] = "Thread started, running pipeline..."
    _tasks[task_id]["updated_at"] = datetime.utcnow().isoformat()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run_collection_pipeline(task_id, criteria))
    except Exception as e:
        _tasks[task_id]["status"] = "failed"
        _tasks[task_id]["progress_message"] = f"Pipeline error: {e}"
    finally:
        loop.close()


@router.post("/start")
async def start_collection(
    criteria: UserCriteria,
    db: Session = Depends(get_db),
):
    task_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    _tasks[task_id] = {
        "task_id": task_id,
        "status": "pending",
        "phase": "collecting",
        "progress_message": "Task queued",
        "results_count": 0,
        "storage_result": None,
        "created_at": now,
        "updated_at": now,
    }

    thread = threading.Thread(
        target=_run_pipeline_in_thread,
        args=(task_id, criteria),
        daemon=True,
    )
    thread.start()
    return {"success": True, "data": {"task_id": task_id}}


def _sync_progress(task_id: str, state: dict):
    task = _tasks.get(task_id)
    if not task:
        return
    phase = state.get("current_phase", task["phase"])
    task["phase"] = phase
    msgs = state.get("messages", [])
    if msgs:
        task["progress_message"] = msgs[-1]
    task["results_count"] = len(state.get("raw_results", []))
    task["updated_at"] = datetime.utcnow().isoformat()


def _make_progress_cb(task_id: str):
    def cb(phase: str, message: str):
        task = _tasks.get(task_id)
        if task:
            task["phase"] = phase
            task["progress_message"] = message
            task["updated_at"] = datetime.utcnow().isoformat()
    return cb


async def _run_collection_pipeline(task_id: str, criteria: UserCriteria):
    from reportagent.agents.graph import build_collection_graph
    from reportagent.agents.state import AgentState

    _tasks[task_id]["status"] = "running"
    _tasks[task_id]["phase"] = "collecting"
    _tasks[task_id]["progress_message"] = "Starting collection pipeline..."
    _tasks[task_id]["updated_at"] = datetime.utcnow().isoformat()

    progress_cb = _make_progress_cb(task_id)

    def _is_cancelled() -> bool:
        return _tasks.get(task_id, {}).get("status") == "cancelled"

    try:
        graph = build_collection_graph(
            progress_cb=progress_cb,
            cancel_check=_is_cancelled,
        )
        initial_state: AgentState = {
            "criteria": criteria,
            "task_id": task_id,
            "raw_results": [],
            "collection_status": "pending",
            "collection_errors": [],
            "classified_reports": [],
            "classification_status": "pending",
            "storage_result": None,
            "storage_status": "pending",
            "current_phase": "collecting",
            "messages": [],
        }

        result = initial_state
        async for event in graph.astream(initial_state, stream_mode="values"):
            result = event
            _sync_progress(task_id, result)
            if _tasks[task_id]["status"] == "cancelled":
                return

        _tasks[task_id]["status"] = "completed"
        _tasks[task_id]["phase"] = "complete"
        _tasks[task_id]["results_count"] = len(result.get("classified_reports", []))
        sr = result.get("storage_result")
        _tasks[task_id]["storage_result"] = (
            {
                "total_processed": sr.total_processed,
                "newly_added": sr.newly_added,
                "updated": sr.updated,
                "duplicate_skipped": sr.duplicate_skipped,
            }
            if sr
            else None
        )
        _tasks[task_id]["progress_message"] = "Collection complete"
    except Exception as e:
        _tasks[task_id]["status"] = "failed"
        _tasks[task_id]["progress_message"] = f"Error: {e}"

    _tasks[task_id]["updated_at"] = datetime.utcnow().isoformat()


@router.get("/{task_id}")
async def get_task_status(task_id: str):
    task = _tasks.get(task_id)
    if not task:
        return {"success": False, "error": "Task not found"}
    return {"success": True, "data": task}


@router.delete("/{task_id}")
async def cancel_task(task_id: str):
    task = _tasks.get(task_id)
    if not task:
        return {"success": False, "error": "Task not found"}
    task["status"] = "cancelled"
    task["progress_message"] = "Task cancelled by user"
    task["updated_at"] = datetime.utcnow().isoformat()
    return {"success": True, "message": "Task cancelled"}


@router.get("/tasks/list")
async def list_tasks():
    sorted_tasks = sorted(_tasks.values(), key=lambda t: t["created_at"], reverse=True)
    return {"success": True, "data": {"tasks": sorted_tasks[:50]}}
