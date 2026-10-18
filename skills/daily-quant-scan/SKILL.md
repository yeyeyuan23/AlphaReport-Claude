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
收集完成后请简要总结收集到的研报情况。只有用户明确要求收集时才执行；`/daily` 和 `/weekly` 只生成报告，不能调用这个采集工作流。自动多来源采集由服务中的 `daily_collection` 定时任务负责，最后完成达标论文的全文质量评估并保存，失败或延期项在后续采集时继续处理。手动执行同一流程可用 `.venv/bin/python scripts/collect.py`；加 `--report-date 2026-10-17` 可指定报告日，采集其前一天 10 月 16 日的资料，日志仍记录实际执行时间。

Use the shared Python tools/services for execution. The task template above is a workflow, not permission to send messages.

采集入库和批量重分类共用 `classification.md` 中的分类提示词与 `configs/classification_taxonomy.yaml` 中的类别定义。模型须逐标签附原文引文与理由，程序核对引文后保存分类证据；材料未说明的维度留空，不为凑数量增加主题。

Run from the project root:
```bash
.venv/bin/python -m reportagent.cli.collect --topics '量化策略,因子模型' --sources arxiv,bigquant --max 15 --no-analysis
```
