#!/usr/bin/env python
"""Production entrypoint — avoids `python -m uvicorn` subprocess issues."""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "reportagent.main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
