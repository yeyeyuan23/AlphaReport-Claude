"""Canonical startup must retain successful transfer diagnostics."""
from pathlib import Path
import subprocess
import sys


def test_web_startup_logs_project_timings_without_http_client_urls():
    script = '''
import logging
import uvicorn
from reportagent.main import serve
def observe(*args, **kwargs):
    logging.getLogger('reportagent.sources.content_fetch').info('transfer-timing-visible')
    logging.getLogger('httpx').info('signed-url-must-not-print')
uvicorn.run = observe
serve()
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'transfer-timing-visible' in result.stderr
    assert 'signed-url-must-not-print' not in result.stderr
