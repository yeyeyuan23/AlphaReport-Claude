"""Claude Code process adapter; task instructions come from the shared registry."""
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from reportagent.utils.config import PROJECT_ROOT
from reportagent.bot.sessions import get_session_snapshot, save_session, del_session
from reportagent.skills.registry import format_skills_context, load_all_skills

logger = logging.getLogger(__name__)

CLAUDE_PROMPT = '你是 AlphaReport 智能助手。用户通过飞书与你交互。\n项目路径：{dir}\n\n规则：\n- 回复简洁精炼，用中文\n- 修改文件前先读取确认当前内容\n- 修改后说明改了什么\n- 量化金融问题时深入分析，主动推荐研报'

def build_system_prompt():
    return CLAUDE_PROMPT.format(dir=str(PROJECT_ROOT)) + format_skills_context(load_all_skills())

def claude_environment():
    """Use the configured DeepSeek account for normal Claude Code sessions."""
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(PROJECT_ROOT / ".claude-bot")}
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        env.update({
            "ANTHROPIC_API_KEY": key,
            "ANTHROPIC_AUTH_TOKEN": key,
            "ANTHROPIC_BASE_URL": os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/anthropic",
        })
    return env

def ask_claude(msg, chat_id, *, session_snapshot=None):
    claude = shutil.which("claude")
    if not claude:
        return "Claude Code 未安装"

    current_snapshot = get_session_snapshot(chat_id)
    stale_reply = "会话已清除或更新，请重新发送问题。"
    if session_snapshot is not None and session_snapshot[2] != current_snapshot[2]:
        return stale_reply
    sid, summary, revision = current_snapshot if session_snapshot is None else session_snapshot
    new = sid is None
    if new:
        sid = str(uuid.uuid4())
    session_dir = PROJECT_ROOT / ".claude-bot" / "projects" / str(PROJECT_ROOT).replace("/", "-").lstrip("-")

    # Carry the same revision through compaction and resume recovery. Never
    # restart from a fresh snapshot, which could turn a cancelled request new.
    while True:
        if not new:
            sf = session_dir / f"{sid}.jsonl"
            if sf.exists() and sf.stat().st_size > 50 * 1024:
                logger.info(f"Compacting session {sid[:8]} ({sf.stat().st_size//1024}KB)")
                new_summary = _compact_session(sid, summary, session_dir)
                new_sid = str(uuid.uuid4())
                next_revision = save_session(chat_id, new_sid, new_summary, expected_revision=revision)
                if next_revision is None:
                    return stale_reply
                revision, sid, summary, new = next_revision, new_sid, new_summary, True

        prompt = f"[历史摘要]\n{summary}\n\n---\n当前问题：{msg}" if summary else msg
        cmd = [
            claude, "-p", prompt,
            "--session-id" if new else "--resume", sid,
            "--append-system-prompt", build_system_prompt(),
            "--print", "--output-format", "text",
            "--model", os.environ.get("CLAUDE_BOT_MODEL", "deepseek-v4-pro"),
            "--permission-mode", "dontAsk",
        ]
        logger.info(f"Claude session={sid[:8]} new={new}")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                               cwd=str(PROJECT_ROOT), env=claude_environment())
            out = r.stdout.strip()
            if r.returncode == 0 and out:
                if save_session(chat_id, sid, summary, expected_revision=revision) is None:
                    return stale_reply
                return out
            if not new and "session" in r.stderr.lower():
                next_revision = del_session(chat_id, expected_revision=revision)
                if next_revision is None:
                    return stale_reply
                revision, sid, summary, new = next_revision, str(uuid.uuid4()), "", True
                continue
            return f"出错: {r.stderr[:200]}"
        except Exception as e:
            return f"失败: {e}"

def _compact_session(sid, old_summary, session_dir):
    """Summarize session into a paragraph, merging with previous summary."""
    sf = session_dir / f"{sid}.jsonl"
    try:
        with open(sf) as f: lines = f.readlines()
        recent = "".join(lines[-200:])[:8000]
        claude = shutil.which("claude")
        if not claude: return old_summary

        base = f"之前的摘要：{old_summary}\n\n" if old_summary else ""
        prompt = f"{base}将以下对话历史总结为简短中文摘要（不超过300字），保留关键决策、用户偏好和重要结论：\n\n{recent}"
        r = subprocess.run(
            [claude, "-p", prompt, "--print", "--output-format", "text",
             "--model", "deepseek-v4-flash", "--tools", ""],
            capture_output=True, text=True, timeout=120,
            cwd=str(PROJECT_ROOT),
            env=claude_environment(),
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()[:500]
    except Exception as e:
        logger.warning(f"Compact failed: {e}")
    return old_summary
