import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from reportagent.bot import feishu as bot, claude


def test_help_lists_each_command_separately_and_chatid_is_copyable():
    text = bot.handle_cmd('/help', 'oc_fixture', 'p2p')
    for cmd in ('stats', 'search', 'recent', 'chatid', 'clear', 'help'):
        assert '\n/' + cmd in text
    assert '`' not in bot.handle_cmd('/chatid', 'oc_fixture', 'p2p')


def test_search_and_recent_separate_entries_and_link_to_reports():
    from reportagent.db import engine, repository
    rows = [SimpleNamespace(id=i, title='A long title ' * 15, source='arxiv', created_at=None)
            for i in (1, 2)]
    repo = MagicMock(); repo.list_reports.return_value = (rows, 9)
    with patch.object(engine, 'get_session_factory', return_value=MagicMock()), patch.object(repository, 'ReportRepository', return_value=repo):
        for cmd in ('/search risk', '/recent'):
            text = bot.handle_cmd(cmd, 'oc_fixture', 'p2p')
            assert '9' in text and '\n\n2.' in text
            assert '…' in text and '/library/1' in text and '/library/2' in text


def test_model_answer_is_not_cut_off_at_4000_characters():
    answer = '长回答：' + '完整原文段落。' * 1500 + 'FINAL_MARKER'
    with patch.object(claude.shutil, 'which', return_value='claude'), patch.object(claude, 'get_session_snapshot', return_value=(None, '', 0)), patch.object(claude, 'save_session'), patch.object(claude.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, answer, '')):
        assert claude.ask_claude('question', 'oc_fixture') == answer


def test_markdown_reply_has_renderable_sections_and_links():
    from reportagent.services.feishu_replies import response_payloads
    parts = response_payloads('## 方法\n\n**结论**\n\n[原文](https://example.org/paper)')
    assert len(parts) == 1 and parts[0][0] == 'interactive'
    card = json.loads(parts[0][1])
    assert '**方法**' in str(card) and '[原文](https://example.org/paper)' in str(card)


def test_long_plain_reply_is_complete_and_byte_bounded():
    from reportagent.services.feishu_replies import response_payloads
    text = '长文字🙂' * 3000 + 'FINAL_MARKER'
    parts = response_payloads(text)
    assert len(parts) > 1
    assert ''.join(json.loads(content)['text'] for kind, content in parts) == text
    assert all(len(content.encode()) < 20000 for kind, content in parts)


def test_table_becomes_readable_fields_without_losing_values():
    from reportagent.services.feishu_replies import response_payloads
    parts = response_payloads('| 模型 | 损失 |\n| --- | --- |\n| A | 0.25 |\n| B | 0.21 |')
    text = str(parts)
    assert '**模型**：A' in text and '**损失**：0.25' in text
    assert '**模型**：B' in text and '**损失**：0.21' in text
    assert '| --- |' not in text


def test_every_chunk_is_sent_and_partial_failure_is_reported():
    from reportagent.services.feishu_replies import deliver_response
    client = MagicMock()
    client.im.v1.message.create.side_effect = [
        SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id='first')),
        SimpleNamespace(success=lambda: False, code=999),
    ]
    import pytest
    with pytest.raises(RuntimeError):
        deliver_response(client, '长文字' * 4000, 'oc_fixture', request_id='stable-request')
    assert client.im.v1.message.create.call_count == 2


def test_long_fenced_code_keeps_all_lines_inside_fences():
    from reportagent.services.feishu_replies import response_payloads
    code = '\n'.join(f'print({n})' for n in range(1500))
    parts = response_payloads('```python\n' + code + '\n```')
    markdown = [e['content'] for _, content in parts for e in json.loads(content)['elements'] if e['tag'] == 'markdown']
    assert all(s.count('```') % 2 == 0 for s in markdown)
    joined = '\n'.join(markdown)
    assert all(f'print({n})' in joined for n in range(1500))


def test_private_group_and_thread_deliveries_use_correct_sdk_routes():
    from reportagent.services.feishu_replies import deliver_response
    for kind in ('open_id', 'chat_id', 'reply'):
        client = MagicMock()
        response = SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id='sent'))
        client.im.v1.message.create.return_value = response
        client.im.v1.message.reply.return_value = response
        assert deliver_response(client, '**完整回答**', 'target', receive_type=kind,
                                reply_to='parent' if kind == 'reply' else None) == ['sent']
        if kind == 'reply':
            client.im.v1.message.create.assert_not_called()
            request = client.im.v1.message.reply.call_args.args[0]
            assert request.message_id == 'parent'
        else:
            client.im.v1.message.reply.assert_not_called()
            request = client.im.v1.message.create.call_args.args[0]
            assert request.receive_id_type == kind
            assert request.request_body.receive_id == 'target'
        assert request.request_body.msg_type == 'interactive'


def test_split_report_retries_reuse_each_parts_uuid():
    from reportagent.services.feishu_replies import deliver_response
    client = MagicMock()
    client.im.v1.message.create.return_value = SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id='sent'))
    text = '原文内容。' * 3000
    deliver_response(client, text, 'target', request_id='stable-original')
    before = [c.args[0].request_body.uuid for c in client.im.v1.message.create.call_args_list]
    client.reset_mock()
    deliver_response(client, text, 'target', request_id='stable-original')
    after = [c.args[0].request_body.uuid for c in client.im.v1.message.create.call_args_list]
    assert before == after and before[0] == 'stable-original'
    assert len(before) > 1 and len(set(before)) == len(before)


def test_long_markdown_keeps_source_links_whole_at_chunk_boundary():
    from reportagent.services.feishu_replies import response_payloads
    link = '[source](https://example.org/research-paper)'
    text = '**Answer** ' + ('a' * 6955) + ' ' + link + ' end'
    parts = response_payloads(text)
    contents = [e['content'] for _, payload in parts for e in json.loads(payload)['elements'] if e['tag'] == 'markdown']
    assert sum(link in part for part in contents) == 1
    assert all('[source](' not in part or link in part for part in contents)
