#!/bin/bash
# =============================================
# AlphaReport 一键部署脚本
# 用法: 在服务器上执行 bash deploy/deploy.sh
# =============================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# ---- 可配置项 ----
export DATA_DIR="${DATA_DIR:-/data1/alphareport/data}"
export MODEL_CACHE="${MODEL_CACHE:-/data1/alphareport/model_cache}"
export CONFIG_DIR="${CONFIG_DIR:-/data1/alphareport/configs}"
export ENV_FILE="${ENV_FILE:-/data1/alphareport/.env}"
export PORT="${PORT:-8080}"
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERROR]${NC} $1"; }

# ---- 前置检查 ----
log "检查环境..."
docker --version >/dev/null 2>&1 || { err "Docker 未安装"; exit 1; }
docker-compose --version >/dev/null 2>&1 || { err "docker-compose 未安装"; exit 1; }
[ -d "$PROJECT_DIR/reportagent" ] || { err "未找到项目目录，请在项目根目录下执行"; exit 1; }

# ---- 创建目录 ----
log "创建数据目录..."
mkdir -p "$DATA_DIR/pdf_library"
mkdir -p "$DATA_DIR/chroma_db"
mkdir -p "$MODEL_CACHE"
mkdir -p "$CONFIG_DIR"

# ---- .env 文件 ----
if [ ! -f "$ENV_FILE" ]; then
    err "环境变量文件不存在: $ENV_FILE"
    echo ""
    echo "请先创建（从模板复制）："
    echo "  cp $SCRIPT_DIR/.env.production $ENV_FILE"
    echo "  然后编辑 $ENV_FILE 填入:"
    echo "    - OPENAI_API_KEY  (DeepSeek API Key)"
    echo "    - OPENAI_BASE_URL (API 地址)"
    exit 1
fi

# ---- 生产配置 ----
if [ ! -f "$CONFIG_DIR/app.yaml" ]; then
    log "生产配置不存在，从模板复制..."
    cp "$SCRIPT_DIR/app.production.yaml" "$CONFIG_DIR/app.yaml"
    log "已创建: $CONFIG_DIR/app.yaml"
fi

# ---- 构建 ----
log "构建 Docker 镜像（首次约 5-10 分钟）..."
cd "$PROJECT_DIR"

docker-compose -f "$COMPOSE_FILE" build --no-cache

# ---- 启动 ----
log "启动容器..."
docker-compose -f "$COMPOSE_FILE" up -d

# ---- 等待 ----
log "等待服务启动..."
sleep 3

echo ""
echo "========================================="
echo "  AlphaReport 部署完成"
echo ""
echo "  访问地址请使用部署主机的域名或 IP，端口以 docker-compose.yml 的 ports 配置为准。"
echo ""
echo "  容器状态:"
docker-compose -f "$COMPOSE_FILE" ps
echo ""
echo "  常用命令："
echo "    docker-compose -f $COMPOSE_FILE logs -f    # 实时日志"
echo "    docker-compose -f $COMPOSE_FILE restart    # 重启"
echo "    docker-compose -f $COMPOSE_FILE down       # 停止"
echo "    docker logs alphareport-backend -f         # 后端日志"
echo "========================================="
