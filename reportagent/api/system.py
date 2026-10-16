from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["system"])

_ws_connections: dict[str, list[WebSocket]] = {}


@router.get("/api/health")
async def health():
    return {"status": "ok"}


@router.websocket("/ws/collection/{task_id}")
async def collection_ws(websocket: WebSocket, task_id: str):
    await websocket.accept()
    if task_id not in _ws_connections:
        _ws_connections[task_id] = []
    _ws_connections[task_id].append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        _ws_connections[task_id].remove(websocket)
        if not _ws_connections[task_id]:
            del _ws_connections[task_id]


async def broadcast_to_task(task_id: str, message: dict):
    connections = _ws_connections.get(task_id, [])
    for ws in connections:
        try:
            await ws.send_json(message)
        except Exception:
            pass
