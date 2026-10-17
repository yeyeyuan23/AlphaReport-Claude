"""Validate a complete deployment without contacting models or Feishu."""

import ipaddress
import os
import re
import shutil
import sys
from urllib.parse import urlsplit

from reportagent.utils.config import get_llm_api_key


def check_deployment() -> str:
    value = os.environ.get("ALPHAREPORT_BASE_URL", "").strip()
    message = (
        "ALPHAREPORT_BASE_URL 必须是其他设备可访问的服务器网址，"
        "例如 http://192.168.1.20:8080；不能使用 localhost 或监听地址。"
    )
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower().rstrip(".")
        if (
            url.scheme not in {"http", "https"}
            or not host
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
            or any(c.isspace() for c in value)
            or host == "localhost"
            or host.endswith(".localhost")
        ):
            raise ValueError(message)
        if url.port is not None and url.port == 0:
            raise ValueError(message)
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
                raise ValueError(message) from None
        else:
            if address.is_loopback or address.is_unspecified:
                raise ValueError(message)
    except ValueError:
        raise ValueError(message) from None
    for name in (
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_ALLOWED_OPEN_IDS",
        "DEEPSEEK_API_KEY",
    ):
        if not os.environ.get(name, "").strip():
            raise ValueError(f"缺少 {name}，请在部署使用的环境变量文件中填写。")
    if not get_llm_api_key():
        raise ValueError("configs/app.yaml 的 llm.api_key_env 对应的模型密钥未配置。")
    if not shutil.which("claude"):
        raise ValueError("缺少 Claude Code，无法处理飞书普通聊天，请重新构建镜像。")
    return value.rstrip("/")


def main():
    try:
        print(check_deployment())
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
