---
name: weekly-report
description: 汇总上周发表的量化金融论文，生成带原文和研报页面链接的周报，可预览或发送到已授权的飞书目的地。
---

按论文的发表日期筛选上一个完整 UTC 周（周一至周日），不要用入库日期替代。
读取 [briefing.md](briefing.md) 了解周报格式。查询、生成和发送使用
`reportagent.services.weekly_report`，不在入口里另写一套流程。

从仓库根目录预览：

```bash
.venv/bin/python -m reportagent.cli.weekly_report --dry-run
```

仅在用户已授权发送并指定或配置了目的地时，才去掉 `--dry-run`。
缺少模型时可用 `--no-llm` 生成资料列表，不把模板输出描述为模型分析。
