"""Static frontend delivery with confined paths and a SPA fallback."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from reportagent.utils.config import PROJECT_ROOT

router = APIRouter()
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


@router.get("/{full_path:path}", include_in_schema=False)
async def frontend(full_path: str):
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(404, "API endpoint not found")
    directory = FRONTEND_DIST.resolve()
    target = (directory / full_path).resolve()
    if not target.is_relative_to(directory):
        raise HTTPException(404, "File not found")
    if target.is_file():
        return FileResponse(target)
    index = (directory / "index.html").resolve()
    if not index.is_relative_to(directory):
        raise HTTPException(404, "File not found")
    if index.is_file():
        return FileResponse(index)
    return JSONResponse(
        {
            "message": "Frontend not built. Run npm ci and npm run build in frontend/",
            "api_docs": "/docs",
        }
    )
