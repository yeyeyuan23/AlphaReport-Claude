---
name: daily-report
description: 按启用的研究方向筛选报告前一天公开发布的论文，读取已有全文评估，生成有主次的飞书简报；默认先预览，显式授权后发送。
metadata:
  aliases:
  - daily_report
---

按 `configs/push_directions.yaml` 的有效方向工作：
1. 只读取库中昨天的资料，统一按 `published_date` 筛选；来源日期只有年月日，不补造时刻或按时区换日，不使用入库时间替代。arXiv 使用官网列表批次日期；东方财富、BigQuant 使用来源返回的发布日期。本地 PDF 只有年/月或日期未知时不进入日报。复用采集阶段有效的相关性评分和全文评估，不重新筛选、下载或调用模型；显式指定论文 ID 也不能绕过同一日期范围。可展示最近采集状态，但不得声称本次报告重新检查了来源。
2. 只读取采集时保存且当前有效的相关性结果，不重新初筛，不以摘要宣称研究质量高。
3. 按方向轮流展示候选，仅读取当前有效的全文评估；缺失或过期显示待核验，交由后续采集任务处理，不在报告阶段补做。保留原文引证供查阅，不以逐字匹配作为筛查条件。
4. 根据已保存评估生成简报：概览、最多5篇重点解读、其他论文、券商/社区材料、待核验与来源覆盖。不凑数量。
5. 在明确授权的会话发送，运行日志保存发送结果；不记录逐篇“已推送”标记，不按发送历史排除论文。重复生成同一期报告会再次发送；不在推送之后才补做用于推荐的分析。

重点推荐表示阅读优先级：相关性至少6分、质量必须为 strong、评估把握必须为 high 且无明确关键疑虑即可候选，sound 或 medium 等其余可读研究归为值得浏览，不要求单独列出优势。普通局限随文说明；学术、券商和社区材料均可入选，必须标明材料类型。

Use the shared Python tools/services for execution. The task template above is a workflow, not permission to send messages.
Default to preparing a preview; send only when the user explicitly requests delivery and provides or has configured a destination. Feishu daily delivery uses `reportagent.services.daily_push`. The Feishu briefing format is the deterministic template [briefing.md](briefing.md), rendered from saved evaluations; it does not ask another model to change findings or scores. To change research judgments edit `analyze-report/quality-review.md` and `paper-selection` resources. To change field layout edit the renderer and keep saved evidence intact.

Run from the project root:
```bash
.venv/bin/python -m reportagent.cli.daily_feishu_push --dry-run
```

验证库中符合日报时间窗口的指定论文时使用 `--report-ids 1 --dry-run`；来源来源日期不在范围内或无法确认的论文不能进入日报。
`--dry-run` 只读已保存资料并保存本地预览，不更新相关性或全文评估、不调用模型、不采集或发送。
