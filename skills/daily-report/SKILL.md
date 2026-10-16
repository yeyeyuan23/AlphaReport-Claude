---
name: daily-report
description: 按六个研究方向筛选近期论文，全文评估后生成有主次的飞书简报；默认先预览，显式授权后发送。
metadata:
  aliases:
  - daily_report
---

按 `configs/push_directions.yaml` 的有效方向工作：
1. 执行真实采集并记录来源状态；失败不能称为“没有新内容”。
2. 对完整摘要做相关性初筛，不以摘要宣称研究质量高。
3. 各方向轮流选择最多12篇候选，取得全文并完成有原文引证的质量评估。
4. 根据已保存评估生成简报：概览、最多5篇重点解读、其他论文、券商/社区材料、待核验与来源覆盖。不凑数量。
5. 在明确授权的会话发送并记录回执；不在推送之后才补做用于推荐的分析。

Use the shared Python tools/services for execution. The task template above is a workflow, not permission to send messages.
Default to preparing a preview; send only when the user explicitly requests delivery and provides or has configured a destination. Feishu daily delivery uses `reportagent.services.daily_push`. The Feishu briefing format is the deterministic template [briefing.md](briefing.md), rendered from saved evaluations; it does not ask another model to change findings or scores. To change research judgments edit `analyze-report/quality-review.md` and `paper-selection` resources. To change field layout edit the renderer and keep saved evidence intact.

Run from the project root:
```bash
.venv/bin/python -m reportagent.cli.daily_feishu_push --dry-run
```

验证库中指定论文时使用 `--no-collect --report-ids 1 --dry-run`，不会将指定旧论文伪装成当天新论文。
`--dry-run` 仍可采集、分析、保存本地结果和消耗模型额度，但不会发送或记录投递成功。
