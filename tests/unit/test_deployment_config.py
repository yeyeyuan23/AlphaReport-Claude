from types import SimpleNamespace

import pytest


@pytest.fixture
def deployment(monkeypatch):
    from reportagent.cli import check_deployment as module

    for name, value in {
        "FEISHU_APP_ID": "fixture-app",
        "FEISHU_APP_SECRET": "fixture-secret",
        "FEISHU_ALLOWED_OPEN_IDS": "fixture-user",
        "DEEPSEEK_API_KEY": "fixture-key",
        "ALPHAREPORT_BASE_URL": "https://research.example/",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(module, "get_llm_api_key", lambda: "fixture-model-key")
    monkeypatch.setattr(module.shutil, "which", lambda command: "/usr/local/bin/claude")
    return module


def test_external_server_url_is_used_in_bot_details(deployment):
    from reportagent.bot.feishu import _report_list

    assert deployment.check_deployment() == "https://research.example"
    report = SimpleNamespace(
        id=2, title="Fixture paper", source="arxiv", created_at=None
    )
    reply = _report_list([report], 1, "最近入库")
    assert "https://research.example/library/2" in reply
    assert "localhost" not in reply


def test_shared_web_url_normalizes_server_address_for_all_reply_paths(monkeypatch):
    from reportagent.services.public_url import get_web_base_url

    monkeypatch.setenv("ALPHAREPORT_BASE_URL", " https://research.example:8443/ ")
    assert (
        get_web_base_url() + "/library/2" == "https://research.example:8443/library/2"
    )


def test_shared_web_url_keeps_local_development_default(monkeypatch):
    from reportagent.services.public_url import get_web_base_url

    for name in ("ALPHAREPORT_BASE_URL", "ALPHAREPORT_HOST", "ALPHAREPORT_PORT"):
        monkeypatch.delenv(name, raising=False)
    assert get_web_base_url() == "http://localhost:8000"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.2.3.4",
        "http://[::1]",
        "http://0.0.0.0",
        "http://[::]",
        "ftp://research.example",
        "http://user:password@research.example",
        "https://research.example/?key=secret",
        "https://research.example/#fragment",
        "http://research.example:invalid",
        "https://research.example/subpath",
    ],
)
def test_deployment_rejects_unusable_detail_addresses(deployment, monkeypatch, url):
    monkeypatch.setenv("ALPHAREPORT_BASE_URL", url)
    with pytest.raises(ValueError, match="ALPHAREPORT_BASE_URL"):
        deployment.check_deployment()


@pytest.mark.parametrize(
    "url",
    ["http://192.168.1.20:8080", "https://research.example", "http://[fd00::2]:8080"],
)
def test_reachable_private_network_addresses_are_allowed(deployment, monkeypatch, url):
    monkeypatch.setenv("ALPHAREPORT_BASE_URL", url)
    assert deployment.check_deployment() == url


@pytest.mark.parametrize(
    "name",
    [
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_ALLOWED_OPEN_IDS",
    ],
)
def test_missing_bot_configuration_fails_before_startup(deployment, monkeypatch, name):
    monkeypatch.delenv(name)
    with pytest.raises(ValueError, match=name):
        deployment.check_deployment()


def test_missing_chat_runtime_is_reported(deployment, monkeypatch):
    monkeypatch.setattr(deployment.shutil, "which", lambda command: None)
    with pytest.raises(ValueError, match="Claude Code"):
        deployment.check_deployment()


def test_python_model_configuration_is_checked_too(deployment, monkeypatch):
    monkeypatch.setattr(deployment, "get_llm_api_key", lambda: "")
    with pytest.raises(ValueError, match="llm"):
        deployment.check_deployment()


def test_deployment_uses_runtime_credential_names_without_requiring_deepseek(
    deployment, monkeypatch
):
    from reportagent.services import feishu_transport

    monkeypatch.setattr(
        feishu_transport,
        "get_config",
        lambda *args, **kwargs: {
            "app_id_env": "CUSTOM_FEISHU_ID",
            "app_secret_env": "CUSTOM_FEISHU_SECRET",
        },
    )
    for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("CUSTOM_FEISHU_ID", "configured-app")
    monkeypatch.setenv("CUSTOM_FEISHU_SECRET", "configured-secret")
    assert deployment.check_deployment() == "https://research.example"


def test_deployment_rejects_blank_custom_credentials(deployment, monkeypatch):
    from reportagent.services import feishu_transport

    monkeypatch.setattr(
        feishu_transport,
        "get_config",
        lambda *args, **kwargs: {
            "app_id_env": "CUSTOM_FEISHU_ID",
            "app_secret_env": "CUSTOM_FEISHU_SECRET",
        },
    )
    monkeypatch.setenv("CUSTOM_FEISHU_ID", "   ")
    monkeypatch.setenv("CUSTOM_FEISHU_SECRET", "configured-secret")
    with pytest.raises(ValueError, match="CUSTOM_FEISHU_ID"):
        deployment.check_deployment()
