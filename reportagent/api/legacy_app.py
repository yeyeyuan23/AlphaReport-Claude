"""Optional historical LangGraph API; not the default deployment entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from reportagent.db.engine import init_db
from reportagent.api import (
    collection,
    reports,
    classification,
    system,
    chat,
    reproduction,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import logging

    _log = logging.getLogger(__name__)
    init_db()
    try:
        from reportagent.services.scheduler import start_scheduler

        start_scheduler()
        print("[MAIN] Scheduler started OK", flush=True)
    except Exception as e:
        print(f"[MAIN] Scheduler startup failed: {e}", flush=True)
        _log.warning("Scheduler failed to start: %s", e)
    yield
    try:
        from reportagent.services.scheduler import stop_scheduler

        stop_scheduler()
    except Exception:
        pass


app = FastAPI(
    title="Research Report Agent",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(collection.router)
app.include_router(reports.router)
app.include_router(reproduction.router)
app.include_router(classification.router)
app.include_router(system.router)
app.include_router(chat.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "reportagent.api.legacy_app:app", host="0.0.0.0", port=8000, reload=True
    )
