"""Validate orchestration and failure handling without requiring Docker or secrets."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from tests.support.paths import PROJECT_ROOT


@pytest.fixture
def deployment(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
args = sys.argv[1:]
with open(os.environ['DOCKER_EVENTS'], 'a') as stream:
    stream.write(json.dumps({'args': args, 'port': os.environ.get('PORT'),
        'env_file': os.environ.get('ENV_FILE'), 'data_dir': os.environ.get('DATA_DIR')}) + '\\n')
if args[:2] == ['compose', 'version'] and os.environ.get('LEGACY_COMPOSE'):
    sys.exit(1)
if 'run' in args:
    if os.environ.get('REJECT_CONFIG'):
        sys.exit(1)
    print('https://research.example')
if args and args[0] == 'inspect':
    print('healthy')
"""
    )
    docker.chmod(0o755)
    # The fallback accepts compose arguments directly, as docker-compose does.
    (binary / "docker-compose").symlink_to(docker)
    state = tmp_path / "state"
    state.mkdir()
    env_file = state / ".env"
    env_file.write_text("ALPHAREPORT_BASE_URL=https://research.example\n")
    env = {
        **os.environ,
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "DATA_DIR": str(state / "data"),
        "MODEL_CACHE": str(state / "models"),
        "CONFIG_DIR": str(state / "configs"),
        "BOT_STATE_DIR": str(state / "sessions"),
        "ENV_FILE": str(env_file),
        "PORT": "8088",
        "DOCKER_EVENTS": str(tmp_path / "events"),
    }
    return env


@pytest.mark.parametrize("legacy", [False, True])
def test_deploy_builds_validates_and_recreates_shared_supervisor(deployment, legacy):
    if legacy:
        deployment["LEGACY_COMPOSE"] = "true"
    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "deploy/deploy.sh")],
        env=deployment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    events = [
        json.loads(line)
        for line in Path(deployment["DOCKER_EVENTS"]).read_text().splitlines()
    ]
    run = next(i for i, e in enumerate(events) if "run" in e["args"])
    up = next(i for i, e in enumerate(events) if "up" in e["args"])
    assert run < up
    assert "reportagent.cli.check_deployment" in events[run]["args"]
    assert "--force-recreate" in events[up]["args"]
    assert "--remove-orphans" in events[up]["args"]
    assert events[up]["port"] == "8088"
    assert events[up]["env_file"] == deployment["ENV_FILE"]
    assert "https://research.example/library/" in result.stdout
    assert (Path(deployment["CONFIG_DIR"]) / "app.yaml").is_file()
    assert Path(deployment["BOT_STATE_DIR"]).is_dir()


def test_invalid_replacement_keeps_existing_deployment_running(deployment):
    deployment["REJECT_CONFIG"] = "true"
    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "deploy/deploy.sh")],
        env=deployment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    events = [
        json.loads(line)["args"]
        for line in Path(deployment["DOCKER_EVENTS"]).read_text().splitlines()
    ]
    assert not any("up" in args or "down" in args for args in events)


def test_first_deployment_creates_template_and_stops_for_configuration(deployment):
    Path(deployment["ENV_FILE"]).unlink()
    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "deploy/deploy.sh")],
        env=deployment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "ALPHAREPORT_BASE_URL" in result.stderr
    assert Path(deployment["ENV_FILE"]).is_file()
    assert "DEEPSEEK_API_KEY=" in Path(deployment["ENV_FILE"]).read_text()
    assert not Path(deployment["ENV_FILE"]).stat().st_mode & 0o077


def test_compose_exposes_shared_web_and_bot_runtime():
    services = yaml.safe_load((PROJECT_ROOT / "deploy/docker-compose.yml").read_text())[
        "services"
    ]
    runtime = services["backend"]
    assert runtime["command"] == ["bash", "start.sh"]
    assert runtime["environment"]["ALPHAREPORT_BIND_HOST"] == "0.0.0.0"
    assert runtime["environment"]["ALPHAREPORT_DEPLOYMENT"] == "true"
    assert runtime["ports"] == ["${PORT:-80}:8000"]
    assert any(v.endswith(":/app/data") for v in runtime["volumes"])
    assert any(v.endswith(":/app/.claude-bot") for v in runtime["volumes"])
    assert runtime["init"] is True
    assert "frontend" not in services


def test_fresh_image_grants_only_named_shared_skill_commands():
    settings = PROJECT_ROOT / "deploy/claude-settings.json"
    assert settings.is_file(), (
        "Fresh containers need tracked permissions for shared skill CLIs"
    )
    allowed = json.loads(settings.read_text())["permissions"]["allow"]
    assert "Bash(.venv/bin/python -m reportagent.cli.report *)" in allowed
    assert "Bash(.venv/bin/python -m reportagent.cli.reproduction *)" in allowed
    assert "Bash" not in allowed and "Bash(*)" not in allowed
    dockerfile = (PROJECT_ROOT / "deploy/Dockerfile.backend").read_text()
    assert "COPY deploy/claude-settings.json /app/.claude/settings.json" in dockerfile
