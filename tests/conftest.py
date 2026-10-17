"""Keep automated checks offline even when a test forgets an external mock."""

import socket
import subprocess
from pathlib import Path

import pytest

from tests.support.paths import PROJECT_ROOT

pytest_plugins = ["tests.support.database"]


@pytest.fixture(autouse=True)
def offline_boundaries(request, monkeypatch):
    relative = request.path.relative_to(PROJECT_ROOT / "tests")
    if relative.parts[0] not in {"unit", "integration"}:
        return

    def no_network(*args, **kwargs):
        raise AssertionError(
            "Offline checks must mock external API, download and message connections"
        )

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)

    from reportagent.llm.client import LLMClient

    def no_model(*args, **kwargs):
        raise AssertionError("Offline checks must inject a model double")

    monkeypatch.setattr(LLMClient, "_build_openai_client", no_model)
    monkeypatch.setattr(LLMClient, "_build_anthropic_client", no_model)

    popen = subprocess.Popen

    def offline_process(args, *positional, **kwargs):
        executable = args[0] if isinstance(args, (list, tuple)) else args.split()[0]
        if Path(str(executable)).name == "claude":
            raise AssertionError("Offline checks must mock the model CLI")
        return popen(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", offline_process)


@pytest.fixture(autouse=True)
def isolated_bot_state(monkeypatch, tmp_path):
    """No offline bot check may read a real session store or inherit active turns."""
    from reportagent.bot import feishu, sessions

    monkeypatch.setattr(sessions, "SESSION_DB", tmp_path / "bot-sessions.db")
    monkeypatch.setattr(feishu, "_dispatcher", feishu.MessageDispatcher())
