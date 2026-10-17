"""Real Feishu request objects, bounded delivery and strict acknowledgement checks."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from reportagent.services import daily_push, weekly_report
from reportagent.services.feishu_replies import deliver_response


def response(ok=True, message_id="om-delivered"):
    return SimpleNamespace(
        success=lambda: ok,
        code=0 if ok else 999,
        msg="ok" if ok else "rejected",
        data=SimpleNamespace(message_id=message_id),
    )


@pytest.fixture
def sdk(monkeypatch):
    import lark_oapi

    client = MagicMock()
    builder = MagicMock()
    builder.app_id.return_value = builder
    builder.app_secret.return_value = builder
    builder.build.return_value = client
    monkeypatch.setattr(lark_oapi.Client, "builder", lambda: builder)
    return client


@pytest.mark.parametrize("bad_id", [None, "", "  ", 42, MagicMock()])
def test_text_requires_real_nonempty_message_id(sdk, bad_id):
    sdk.im.v1.message.create.return_value = response(message_id=bad_id)
    assert daily_push.send_feishu_text("recipient", "plain", "open_id") is False
    with pytest.raises(RuntimeError):
        deliver_response(sdk, "plain", "recipient")


@pytest.mark.parametrize("service", [daily_push, weekly_report])
def test_card_failure_falls_back_to_complete_utf8_bounded_text(
    sdk, service, monkeypatch
):
    from reportagent.services import feishu_transport

    build = MagicMock(return_value=sdk)
    monkeypatch.setattr(feishu_transport, "build_client", build)
    sdk.im.v1.message.create.side_effect = lambda req: response(
        ok=req.request_body.msg_type == "text",
    )
    text = "完整🙂原文。" * 2500 + "FINAL_SOURCE https://example.test/paper"
    if service is daily_push:
        delivered = service.send_feishu_card(
            "target", text, [], "2026-10-17", "chat_id"
        )
    else:
        delivered = service.send_feishu_card(
            "target", text, [], "2026-10-05", "2026-10-11", "chat_id"
        )
    assert delivered is True
    build.assert_called_once_with()
    requests = [call.args[0] for call in sdk.im.v1.message.create.call_args_list]
    assert requests[0].request_body.msg_type == "interactive"
    parts = [json.loads(req.request_body.content)["text"] for req in requests[1:]]
    assert "".join(parts) == text
    assert all(len(part.encode("utf-8")) <= 7000 for part in parts)
    assert all(
        req.receive_id_type == "chat_id" and req.request_body.receive_id == "target"
        for req in requests
    )


@pytest.mark.parametrize("service", [daily_push, weekly_report])
def test_fallback_partial_failure_is_not_complete_delivery(sdk, service):
    sdk.im.v1.message.create.side_effect = [
        response(False),
        response(),
        response(False),
    ]
    text = "原文🙂" * 4000
    if service is daily_push:
        delivered = service.send_feishu_card("target", text, [], "2026-10-17")
    else:
        delivered = service.send_feishu_card(
            "target", text, [], "2026-10-05", "2026-10-11"
        )
    assert delivered is False
    assert sdk.im.v1.message.create.call_count == 3


def test_rich_reply_fallback_preserves_reply_route_and_text(sdk):
    sdk.im.v1.message.reply.side_effect = lambda req: response(
        ok=req.request_body.msg_type == "text",
        message_id="om-reply",
    )
    text = "**Evidence**\n" + "全部证据🙂" * 1200
    ids = deliver_response(sdk, text, "", reply_to="om-parent")
    assert ids and all(message_id == "om-reply" for message_id in ids)
    requests = [call.args[0] for call in sdk.im.v1.message.reply.call_args_list]
    assert all(req.message_id == "om-parent" for req in requests)
    parts = [
        json.loads(req.request_body.content)["text"]
        for req in requests
        if req.request_body.msg_type == "text"
    ]
    assert "".join(parts) == text
    sdk.im.v1.message.create.assert_not_called()


@pytest.mark.parametrize("service", [daily_push, weekly_report])
def test_success_without_card_id_requires_confirmed_fallback(sdk, service):
    sdk.im.v1.message.create.side_effect = [
        response(message_id=None),
        response(message_id="om-text"),
    ]
    if service is daily_push:
        assert service.send_feishu_card("target", "full evidence", [], "2026-10-17")
    else:
        assert service.send_feishu_card(
            "target", "full evidence", [], "2026-10-05", "2026-10-11"
        )
    request = sdk.im.v1.message.create.call_args.args[0]
    assert request.request_body.msg_type == "text"
    assert json.loads(request.request_body.content) == {"text": "full evidence"}


def test_rich_fallback_uuids_are_distinct_and_stable_across_retries(sdk):
    sdk.im.v1.message.create.side_effect = lambda req: response(
        ok=req.request_body.msg_type == "text"
    )
    text = "**Evidence**\n" + "完整原文🙂" * 1500
    deliver_response(sdk, text, "target", request_id="original-request")
    before = [
        call.args[0].request_body.uuid
        for call in sdk.im.v1.message.create.call_args_list
    ]
    sdk.reset_mock()
    deliver_response(sdk, text, "target", request_id="original-request")
    after = [
        call.args[0].request_body.uuid
        for call in sdk.im.v1.message.create.call_args_list
    ]
    assert before == after and before[0] == "original-request"
    assert len(before) > 2 and len(set(before)) == len(before)


def test_bot_factory_uses_same_configured_credential_names(monkeypatch):
    from reportagent.bot import client as bot_client
    from reportagent.services import feishu_transport
    import lark_oapi

    builder = MagicMock()
    builder.app_id.return_value = builder
    builder.app_secret.return_value = builder
    monkeypatch.setattr(lark_oapi.Client, "builder", lambda: builder)
    monkeypatch.setattr(
        feishu_transport,
        "get_config",
        lambda *args, **kwargs: {
            "app_id_env": "FIXTURE_FEISHU_APP",
            "app_secret_env": "FIXTURE_FEISHU_SECRET",
        },
    )
    monkeypatch.setenv("FIXTURE_FEISHU_APP", "app-fixture")
    monkeypatch.setenv("FIXTURE_FEISHU_SECRET", "secret-fixture")
    bot_client._client()
    assert builder.app_id.call_args.args == ("app-fixture",)
    assert builder.app_secret.call_args.args == ("secret-fixture",)


def test_daily_text_adapter_bounds_direct_calls_without_losing_content(sdk):
    sdk.im.v1.message.create.return_value = response()
    text = "全部🙂文字" * 3000
    assert daily_push.send_feishu_text("target", text, "chat_id")
    requests = [call.args[0] for call in sdk.im.v1.message.create.call_args_list]
    parts = [json.loads(request.request_body.content)["text"] for request in requests]
    assert len(parts) > 1 and "".join(parts) == text
    assert all(len(part.encode("utf-8")) <= 7000 for part in parts)
