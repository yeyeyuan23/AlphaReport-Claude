---
name: paper-selection
description: 根据完整摘要评估论文与已启用研究方向的相关性，为全文评审筛选候选；支持预览和失败重试。
---

先采集入库，再用完整摘要筛选相关论文，随后进行有全文证据的质量评审。
`reportagent.services.paper_scoring` 使用 [scoring-system.md](scoring-system.md)
和 [scoring-user.md](scoring-user.md) 仅评估研究相关度。
研究方向和推送设置属于运行配置，见 `configs/push_directions.yaml`。

从仓库根目录预览最近四天发表、尚未评分的论文：

```bash
.venv/bin/python -m reportagent.cli.llm_filter --published-days 4 --dry-run
```

方向匹配的另一种评分方式使用 `reportagent.cli.llm_direction_filter`，
提示词见 [direction-scoring.md](direction-scoring.md)。
摘要筛选的 `score` 与 `relevance` 相同，兼容旧调用者；不把摘要相关度当作全文质量评分。
阈值取自 YAML 的 `push_settings.llm_filter_threshold`（缺省 6.0），可由 CLI 显式覆盖；模型的筛选建议不能形成另一个隐藏阈值。
方向每次从配置读取，仅接受启用方向。`selection_direction`、`selection_relevance` 和摘要范围说明独立保存。
筛选依据指纹包括启用方向定义、提示词和配置阈值；依据变化后重评窗口内旧结果。
缺失、重复索引及无效分数不能写成已评分；同批有效结果可保留，失败论文下次重试。评分本身不授权推送。

缺少有效摘要、只有标题/机构/类型/页数或占位符时，保存 `pending_needs_material` 状态，`selection_relevance=null`，不记为0分或确认不相关。模型也可以返回该状态。此状态不会进入精选。资料或筛选依据变化后重新评估；模型调用失败属于可重试错误，不等于材料不足。筛选输出按原批次索引匹配；未提交模型的材料不足条目留下编号空缺，不重新编号。
