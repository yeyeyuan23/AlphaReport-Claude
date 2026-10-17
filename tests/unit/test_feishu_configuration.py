"""Shared Feishu clients honor the configured environment-variable names."""

import sys
from types import SimpleNamespace

import pytest

from reportagent.services import feishu_service


@pytest.mark.parametrize(
    "app_id,app_secret,expected",
    [("", "", False), ("id", "", False), ("", "secret", False), ("id", "secret", True)],
)
def test_configuration_requires_both_default_credentials(
    monkeypatch, app_id, app_secret, expected
):
    monkeypatch.setattr(feishu_service, "get_config", lambda key, default: {})
    monkeypatch.setenv("FEISHU_APP_ID", app_id)
    monkeypatch.setenv("FEISHU_APP_SECRET", app_secret)

    assert feishu_service.is_configured() is expected


def test_client_uses_configured_credential_names_at_build_time(monkeypatch):
    monkeypatch.setattr(
        feishu_service,
        "get_config",
        lambda key, default: {
            "app_id_env": "CUSTOM_FEISHU_ID",
            "app_secret_env": "CUSTOM_FEISHU_SECRET",
        },
    )
    monkeypatch.setenv("FEISHU_APP_ID", "unused default id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "unused default secret")
    monkeypatch.delenv("CUSTOM_FEISHU_ID", raising=False)
    monkeypatch.delenv("CUSTOM_FEISHU_SECRET", raising=False)
    assert not feishu_service.is_configured()

    class Builder:
        def app_id(self, value):
            self.id = value
            return self

        def app_secret(self, value):
            self.secret = value
            return self

        def build(self):
            return SimpleNamespace(app_id=self.id, app_secret=self.secret)

    monkeypatch.setitem(
        sys.modules,
        "lark_oapi",
        SimpleNamespace(Client=SimpleNamespace(builder=Builder)),
    )
    monkeypatch.setenv("CUSTOM_FEISHU_ID", "configured id")
    monkeypatch.setenv("CUSTOM_FEISHU_SECRET", "configured secret")
    assert feishu_service.is_configured()
    first = feishu_service._build_client()
    assert (first.app_id, first.app_secret) == ("configured id", "configured secret")

    monkeypatch.setenv("CUSTOM_FEISHU_SECRET", "rotated secret")
    second = feishu_service._build_client()
    assert (second.app_id, second.app_secret) == ("configured id", "rotated secret")
