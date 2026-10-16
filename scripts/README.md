# 运维工具

这里只保留启动和维护工具。功能使用和部署流程统一见 [项目首页](../README.md)。
业务逻辑在 `reportagent/services/`，业务命令统一用 `python -m reportagent.cli.<命令>`。

```text
scripts/
└── ops/
    ├── run_bot.sh
    ├── keepalive.sh
    └── sync_public_url.sh
```

| 工具 | 作用 |
|---|---|
| `ops/run_bot.sh` | 启动飞书机器人，可从其他目录调用 |
| `ops/keepalive.sh` | 检查 `data/alphareport.pid`，需要时后台启动 Web + 机器人，输出到 `data/bot.log` |
| `ops/sync_public_url.sh` | 调用 `reportagent.cli.sync_public_url`，从本机隧道日志更新 `.env` |


## 旧命令迁移

旧业务脚本已删除，外部定时任务或个人命令需要迁移到以下模块入口。
所有命令从仓库根目录执行，前缀为 `.venv/bin/python -m`：

| 原脚本名 | 现在的模块 |
|---|---|
| `collect.py` | `reportagent.cli.collect` |
| `analyze.py` | `reportagent.cli.analyze` |
| `classify.py` | `reportagent.cli.classify` |
| `llm_filter.py` | `reportagent.cli.llm_filter` |
| `llm_direction_filter.py` | `reportagent.cli.llm_direction_filter` |
| `narrative_brief.py` | `reportagent.cli.narrative_brief` |
| `daily_feishu_push.py` | `reportagent.cli.daily_feishu_push` |
| `weekly_report.py` | `reportagent.cli.weekly_report` |
| `run_one_report.py` | `reportagent.cli.report` |
| `parse_pdf.py` | `reportagent.cli.parse_pdf` |
| `db_query.py` | `reportagent.cli.db_query` |

原 `run_bot.sh`、`bot_keepalive.sh`、`sync_public_url.sh` 改为上述 `ops/` 路径；
守护脚本名称改为 `keepalive.sh`。本次整理没有修改本机 crontab 或其他系统调度。

原 `apply_classification.py` 是按固定报告 ID 修改旧数据库的一次性补丁，已删除；
日常重新分类使用 `reportagent.cli.classify`。历史内容仍可从 Git 找回。
本机 `.bak-*` 备份已移到 `data/archive/script-backups/`；
这些本地资料仍忽略提交，业务命令迁移不删除研报或凭据。
