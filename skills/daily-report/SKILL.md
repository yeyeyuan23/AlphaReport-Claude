---
name: daily-report
description: 按启用的研究方向筛选报告当天北京时间 08:00 前 24 小时发表且日期明确到日的论文，全文评估后生成有主次的飞书简报；默认先预览，显式授权后发送。
metadata:
  aliases:
  - daily_report
---

按 `configs/push_directions.yaml` 的有效方向工作：
1. 只读取报告当天北京时间 08:00 前 24 小时按 `published_date` 发表且日期明确到日的资料；窗口包含起点、不含截止点，正文显示完整起止时间，与入库时间无关；仅年份、仅年月、日期未知或发表时间不在窗口内的资料不进入日报。相关性刷新、候选选择与发送前复核使用同一日期窗口，显式指定论文 ID 也不能绕过；报告标题使用报告当天，每篇保留真实发表日期；不搜索来源或采集新条目；库中 arXiv 资料缺少全文时可补取 PDF。可展示最近定时采集的时间和来源状态，但不得说成本次报告重新检查了来源。
2. 对完整摘要做相关性初筛，不以摘要宣称研究质量高。
3. 按方向轮流处理候选，复用有效评估，默认完成所有符合条件且需更新的全文评估；`push_settings.max_reviews` 省略或为 `null` 时不限，正整数启用新全文评估预算，CLI `--max-reviews N` 可覆盖；缺全文项不堵住后续候选，有效缓存不占预算；保留可提供的原文引证供查阅，不以逐字匹配作为筛查条件。
4. 根据已保存评估生成简报：概览、最多5篇重点解读、其他论文、券商/社区材料、待核验与来源覆盖。不凑数量。
5. 在明确授权的会话发送，运行日志保存发送结果；不记录逐篇“已推送”标记，不按发送历史排除论文。重复生成同一期报告会再次发送；不在推送之后才补做用于推荐的分析。

重点推荐表示阅读优先级：相关性至少6分、质量为 sound/strong、评估把握为 medium/high 且无明确关键疑虑即可候选，不要求最高质量/置信度或单独列出优势。普通局限随文说明；学术、券商和社区材料均可入选，必须标明材料类型。

Use the shared Python tools/services for execution. The task template above is a workflow, not permission to send messages.
Default to preparing a preview; send only when the user explicitly requests delivery and provides or has configured a destination. Feishu daily delivery uses `reportagent.services.daily_push`. The Feishu briefing format is the deterministic template [briefing.md](briefing.md), rendered from saved evaluations; it does not ask another model to change findings or scores. To change research judgments edit `analyze-report/quality-review.md` and `paper-selection` resources. To change field layout edit the renderer and keep saved evidence intact.

Run from the project root:
```bash
.venv/bin/python -m reportagent.cli.daily_feishu_push --dry-run
```

验证库中符合日报时间窗口的指定论文时使用 `--report-ids 1 --dry-run`；发表时间不在窗口内或日期未明确到日的论文不能进入日报。
`--dry-run` 仍可更新库中论文的相关性与全文阅读评估、保存本地结果并消耗模型额度，但不采集新论文、不发送。
