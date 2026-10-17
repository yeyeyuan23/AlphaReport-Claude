# 运维工具

功能使用和部署流程统一见 [项目首页](../README.md)。业务命令使用
`python -m reportagent.cli.<命令>`，实现位于 `reportagent/services/`。

| 工具 | 作用 |
|---|---|
| `collect.py` | 手动执行与定时任务相同的采集流程，含 DS 筛选、达标下载入库与耗时回执，不发报告 |
| `ops/run_bot.sh` | 只启动飞书机器人，可从其他目录调用 |
| `ops/keepalive.sh` | 检查 `data/alphareport.pid`，需要时后台启动 Web + 机器人，输出到 `data/bot.log` |
| `ops/sync_public_url.sh` | 从本机隧道日志更新 `.env` 中的公开网址，调用 `reportagent.cli.sync_public_url` |

这些脚本均相对自身位置定位项目。`keepalive.sh` 检查启动器进程是否存在；
它不验证飞书连接或模型服务，也不自动安装 cron、launchd 或 systemd 任务。
