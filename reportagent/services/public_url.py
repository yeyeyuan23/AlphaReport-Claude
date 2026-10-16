"""Synchronize the local reading URL from the most recent tunnel log entry."""
import os
import re
from pathlib import Path

from reportagent.utils.config import PROJECT_ROOT


def sync_public_url(root: Path = PROJECT_ROOT) -> bool:
    env_file = root / ".env"
    log = root / "data" / "cloudflared.log"
    if not env_file.exists() or not log.exists():
        return False
    urls = re.findall(r"https://[a-z0-9.-]+\.trycloudflare\.com", log.read_text(encoding="utf-8"))
    if not urls:
        return False
    content = env_file.read_text(encoding="utf-8")
    line = "ALPHAREPORT_BASE_URL=" + urls[-1]
    if re.search(r"^ALPHAREPORT_BASE_URL=.*$", content, flags=re.M):
        content = re.sub(r"^ALPHAREPORT_BASE_URL=.*$", line, content, flags=re.M)
    else:
        content = content.rstrip("\n") + "\n" + line + "\n"
    env_file.write_text(content, encoding="utf-8")
    os.chmod(env_file, 0o600)
    return True
