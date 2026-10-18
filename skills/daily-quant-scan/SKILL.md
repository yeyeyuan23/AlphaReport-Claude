---
name: daily-quant-scan
description: 按 app.yaml 的来源与检索范围采集量化研究，统一完成摘要筛选、入库与全文评估。
metadata:
  aliases: [每日量化扫描]
---

用户明确要求采集时，从仓库根目录执行统一采集流程：
```bash
.venv/bin/python scripts/collect.py
```

`configs/app.yaml` 配置启用来源（arXiv、东方财富、BigQuant）、分类和主题检索；`configs/push_directions.yaml` 配置研究方向与相关性阈值。遵循这些配置，不另设15篇等隐藏数量上限，不硬编码只采两个来源。用户明确指定子集或预算时仅执行对应范围，先查看 CLI 帮助，不扩大请求。

默认采集指定报告日前一天的来源日期资料。加 `--report-date 2026-10-17` 可指定采集10月16日资料，执行时间仍记录真实时间。`/daily` 和 `/weekly` 仅读库生成报告，不能触发此流程；定时采集由 `daily_collection` 负责。

摘要筛选共用 paper-selection 的提示词和服务，相关度达标后下载并入库，再按 analyze-report 的统一质量口径评估全文。来源只用于追溯与覆盖统计，不能作为相关性或质量等级依据。分类共用 [classification.md](classification.md) 与 `configs/classification_taxonomy.yaml`；逐标签保留原文依据，不凑分类。

完成后报告各来源候选数、材料不足数、入选数、全文评估状态与失败阶段。采集数量、相关性入选和质量推荐分开说明，失败或延期项保留可重试状态。执行采集不授权发送飞书消息。
