---
name: daily-quant-scan
description: 从 arXiv 和 BigQuant 收集最新的量化策略、因子模型相关研报
metadata:
  aliases:
  - 每日量化扫描
  default_params:
    max_results: 15
---

请帮我从 arXiv 和 BigQuant 收集最新的量化金融研报，
主题包括：量化策略、因子模型。
每个数据源最多获取 {max_results} 篇。
收集完成后请简要总结收集到的研报情况。

Use the shared Python tools/services for execution. The task template above is a workflow, not permission to send messages.

Run from the project root:
```bash
.venv/bin/python -m reportagent.cli.collect --topics '量化策略,因子模型' --sources arxiv,bigquant --max 15 --no-analysis
```
