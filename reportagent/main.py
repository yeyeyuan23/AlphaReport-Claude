"""Canonical web entrypoint for local and Docker deployments."""
from contextlib import asynccontextmanager, suppress
import asyncio
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from reportagent.db.engine import init_db
from reportagent.api import library, feishu, frontend, reproduction
from reportagent.skills.registry import load_all_skills

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app):
    init_db()
    try:
        from reportagent.services.scheduler import start_scheduler
        start_scheduler()
    except Exception:
        logger.exception("Scheduler could not start")
    from reportagent.services.local_pdf_import import import_pdf_library
    pdf_import_task = asyncio.create_task(import_pdf_library())
    try:
        yield
    finally:
        pdf_import_task.cancel()
        with suppress(asyncio.CancelledError):
            await pdf_import_task
    try:
        from reportagent.services.scheduler import stop_scheduler
        stop_scheduler()
    except Exception:
        logger.exception("Scheduler could not stop")

def create_app():
    app = FastAPI(title="AlphaReport", version="2.2.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    app.include_router(library.router)
    app.include_router(reproduction.router)
    app.include_router(feishu.router)

    @app.get("/api/v1/health")
    def health():
        import subprocess
        try:
            running = subprocess.run(["pgrep", "-f", "reportagent.bot.feishu"], capture_output=True).returncode == 0
        except OSError:
            running = False
        return {"success": True, "data": {"server": "ok", "feishu_ws": running}}

    @app.get("/api/v1/skills")
    def skills():
        return {"success": True, "data": [
            {"name": skill["name"], "description": skill.get("description", "")}
            for skill in load_all_skills()
        ]}

    app.include_router(frontend.router)
    return app

app = create_app()

def serve():
    import uvicorn
    uvicorn.run("reportagent.main:app", host=os.environ.get("ALPHAREPORT_BIND_HOST", "127.0.0.1"),
                port=int(os.environ.get("ALPHAREPORT_PORT", "8000")))

if __name__ == "__main__":
    serve()
