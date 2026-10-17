#!/bin/bash
# Build the image, then run the same Web + Feishu supervisor as local startup.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
export DATA_DIR="${DATA_DIR:-/data1/alphareport/data}"
export CONFIG_DIR="${CONFIG_DIR:-/data1/alphareport/configs}"
export BOT_STATE_DIR="${BOT_STATE_DIR:-/data1/alphareport/bot_state}"
export ENV_FILE="${ENV_FILE:-/data1/alphareport/.env}"
export PORT="${PORT:-80}"
export APP_UID="${APP_UID:-$(id -u)}"
export APP_GID="${APP_GID:-$(id -g)}"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

command -v docker >/dev/null || { echo "Docker 未安装" >&2; exit 1; }
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null; then
    COMPOSE=(docker-compose)
else
    echo "请安装 Docker Compose" >&2
    exit 1
fi
docker info >/dev/null
mkdir -p "$DATA_DIR/pdf_library" "$DATA_DIR/runtime-home" \
    "$CONFIG_DIR" "$BOT_STATE_DIR" "$(dirname "$ENV_FILE")"
if [[ ! -f "$ENV_FILE" ]]; then
    cp "$PROJECT_DIR/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "已创建 ${ENV_FILE}。请填写飞书凭据、允许名单、DeepSeek 密钥，" >&2
    echo "并将 ALPHAREPORT_BASE_URL 改为其他设备可访问的服务器网址，再重新部署。" >&2
    exit 1
fi
if [[ ! -f "$CONFIG_DIR/app.yaml" ]]; then
    cp "$PROJECT_DIR/configs/app.yaml" "$CONFIG_DIR/app.yaml"
fi
cd "$PROJECT_DIR"
"${COMPOSE[@]}" -f "$COMPOSE_FILE" config --quiet
"${COMPOSE[@]}" -f "$COMPOSE_FILE" build
# Validate without connecting to models or Feishu; leave an existing deployment
# running if the replacement configuration is incomplete.
PUBLIC_URL="$("${COMPOSE[@]}" -f "$COMPOSE_FILE" run --rm --no-deps -T backend python -m reportagent.cli.check_deployment)"
"${COMPOSE[@]}" -f "$COMPOSE_FILE" up -d --force-recreate --remove-orphans
for ((attempt=0; attempt<60; attempt++)); do
    status="$(docker inspect --format '{{.State.Health.Status}}' alphareport-backend 2>/dev/null || true)"
    if [[ "$status" == "healthy" ]]; then
        echo "AlphaReport 网页和机器人进程已启动：$PUBLIC_URL"
        echo "飞书详情链接：$PUBLIC_URL/library/<论文ID>"
        echo "日志：${COMPOSE[*]} -f $COMPOSE_FILE logs -f"
        exit 0
    fi
    if [[ "$status" == "unhealthy" ]]; then break; fi
    sleep 2
done
echo "服务未通过启动检查，请查看容器日志：${COMPOSE[*]} -f $COMPOSE_FILE logs" >&2
exit 1
