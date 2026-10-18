请仅根据以下完整摘要判断研究相关性，供后续全文评审选择候选：

{papers_text}

返回 JSON 对象，papers 数组严格按照论文顺序：
{{"papers": [
  {{
    "index": 0,
    "status": "scored",
    "score": 8.0,
    "relevance": 8.0,
    "best_direction": "该示例最高分的启用方向ID",
    "reason": "一句话说明摘要中的研究问题与该方向的关系；全文质量尚未评审",
    "direction_scores": [
      {{"direction_id": "启用方向ID", "score": 8.0, "reason": "摘要研究问题与此方向的具体关系"}}
    ]
  }},
  ...
]}}

注意：
- index 是上面论文编号对应的整数，不能遗漏、重复或超出范围
- best_direction 只能从 [{direction_ids}] 中选择最匹配的方向；无相关方向时为 null
- status 为 scored 时，score 与 relevance 相同，均为 0 到 10 之间的有限数值，保留一位小数
- 无足够研究材料时 status 为 pending_needs_material，score、relevance、best_direction 均为 null，并说明缺少的材料
- 不输出创新性、实操性或全文质量评分；不设置额外推送阈值
- 只返回包含 papers 数组的 JSON 对象，不要包含其他文字

- status=scored 时，direction_scores 必须恰好覆盖所有启用方向，各方向独立评估；不遗漏、重复或返回未启用方向。
- relevance 和 score 等于最高方向分数，best_direction 为最高分方向（全部为 0 则为 null）。程序从明细计算入选判断。
- 材料不足时 direction_scores=[]，不得用一组 0 分替代材料不足。
