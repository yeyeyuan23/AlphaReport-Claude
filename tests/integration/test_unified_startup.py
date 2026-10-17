"""Exercise the real supervisor with offline stand-ins for its two processes."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from tests.support.paths import PROJECT_ROOT


def fixture_runtime(tmp_path):
    shutil.copy(PROJECT_ROOT / "start.sh", tmp_path / "start.sh")
    dist = tmp_path / "frontend/dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>Fixture</html>")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "lsof").write_text("#!/bin/sh\nexit 1\n")
    (tools / "lsof").chmod(0o755)
    runtime = tools / "python"
    runtime.write_text(
        f"#!{sys.executable}\n"
        + """
import os, signal, sys, time
if sys.argv[1] == '-c':
    sys.exit(0)
role = 'web' if 'uvicorn' in sys.argv else 'bot'
def record(event):
    with open(os.environ['EVENTS'], 'a') as stream:
        stream.write(event + ':' + role + '\\n')
def stop(*args):
    record('stop')
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
record('start')
if os.environ.get('FAIL_ROLE') == role:
    time.sleep(0.3)
    sys.exit(7)
while True:
    time.sleep(0.02)
"""
    )
    runtime.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "ALPHAREPORT_PYTHON": str(runtime),
        "ALPHAREPORT_FRONTEND_PREBUILT": "true",
        "ALPHAREPORT_DEPLOYMENT": "false",
        "ALPHAREPORT_PUBLIC_TUNNEL": "false",
        "FEISHU_APP_ID": "fixture",
        "FEISHU_APP_SECRET": "fixture",
        "EVENTS": str(tmp_path / "events"),
    }
    return env


def test_prebuilt_container_starts_web_and_bot_and_stops_both(tmp_path):
    env = fixture_runtime(tmp_path)
    process = subprocess.Popen(
        ["bash", str(tmp_path / "start.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            events = (
                Path(env["EVENTS"]).read_text() if Path(env["EVENTS"]).exists() else ""
            )
            if "start:web" in events and "start:bot" in events:
                break
            if process.poll() is not None:
                pytest.fail(process.communicate()[0])
            time.sleep(0.02)
        else:
            pytest.fail("Supervisor did not start both services")
        process.terminate()
        process.communicate(timeout=6)
        events = Path(env["EVENTS"]).read_text()
        assert "stop:web" in events and "stop:bot" in events
        assert not (tmp_path / "data/alphareport.pid").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_child_failure_stops_the_other_service(tmp_path):
    env = fixture_runtime(tmp_path)
    env["FAIL_ROLE"] = "bot"
    result = subprocess.run(
        ["bash", str(tmp_path / "start.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode != 0
    assert "stop:web" in Path(env["EVENTS"]).read_text()
    assert not (tmp_path / "data/alphareport.pid").exists()


def test_missing_prebuilt_web_files_prevents_partial_startup(tmp_path):
    env = fixture_runtime(tmp_path)
    (tmp_path / "frontend/dist/index.html").unlink()
    result = subprocess.run(
        ["bash", str(tmp_path / "start.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "index.html" in result.stdout + result.stderr
    assert not Path(env["EVENTS"]).exists()
