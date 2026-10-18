# 运维工具

功能使用和部署流程统一见 [项目首页](../README.md)。业务命令使用
`python -m reportagent.cli.<命令>`，实现位于 `reportagent/services/`。

| 工具 | 作用 |
|---|---|
| `collect.py` | 手动执行与定时任务相同的采集流程，含 DS 筛选、达标下载入库、全文质量评估与耗时回执，不发报告 |
| `ops/run_bot.sh` | 只启动飞书机器人，可从其他目录调用 |
| `ops/keepalive.sh` | 检查 `data/alphareport.pid`，需要时后台启动 Web + 机器人，输出到 `data/bot.log` |
| `ops/sync_public_url.sh` | 从本机隧道日志更新 `.env` 中的公开网址，调用 `reportagent.cli.sync_public_url` |

这些脚本均相对自身位置定位项目。`keepalive.sh` 检查启动器进程是否存在；
它不验证飞书连接或模型服务，也不自动安装 cron、launchd 或 systemd 任务。

## 监听、常驻与隧道

`start.sh` 在前台运行，Ctrl+C 停止它启动的后台和机器人；端口被占用时退出。
监听参数由 Shell 读取，需导出或在命令前赋值，单写 `.env` 不生效：

```bash
ALPHAREPORT_BIND_HOST=127.0.0.1 ALPHAREPORT_PORT=8001 bash start.sh
ALPHAREPORT_PUBLIC_TUNNEL=true bash start.sh # 需安装 cloudflared，默认关闭
bash scripts/ops/sync_public_url.sh         # 将临时网址同步到 .env
```

`keepalive.sh` 需要主机持续运行。启动后的进程号在 `data/alphareport.pid`；
修改代码或配置后应停止旧启动器，再重新启动。外部守护示例不会自动安装，
使用前替换工作目录和包含 `claude` 的实际 PATH：

```cron
PATH=/home/your-user/.local/bin:/usr/local/bin:/usr/bin:/bin
*/5 * * * * cd /absolute/path/to/AlphaReport-Claude && /bin/bash scripts/ops/keepalive.sh
```

只保留一个承载内置调度的后台进程。若用外部任务替代采集、日报或周报，
应先关闭 `configs/app.yaml` 中对应的内置任务，避免重复执行。

## Docker 部署参数

`bash deploy/deploy.sh` 构建并启动网页后台和飞书机器人，镜像包含运行依赖、
Claude Code 与构建后的网页。首次运行创建环境模板，填写后再次执行。

| 变量 | 默认值 |
|---|---|
| `DATA_DIR` | `/data1/alphareport/data` |
| `CONFIG_DIR` | `/data1/alphareport/configs` |
| `BOT_STATE_DIR` | `/data1/alphareport/bot_state` |
| `ENV_FILE` | `/data1/alphareport/.env` |
| `PORT` | `80` |

默认使用执行者的 UID/GID，也可指定 `APP_UID`、`APP_GID`；挂载目录需允许该身份写入。
首次部署复制仓库 `configs/app.yaml`，已有配置不覆盖。凭据变量名应与该文件的
`llm.api_key_env`、`llm.base_url_env` 一致。

`ALPHAREPORT_BASE_URL` 必须是收件人可访问的服务器 IP/域名，包含实际端口；
部署检查拒绝缺失地址、localhost、回环地址和 `0.0.0.0`。
更新配置后重新运行部署脚本以加载环境变量，查看日志：

```bash
docker compose -f deploy/docker-compose.yml logs -f
```

自行执行 Compose 时需导出与部署时一致的目录和端口变量。
健康检查确认网页接口与机器人进程；真实飞书连接和模型调用需分别验证。
