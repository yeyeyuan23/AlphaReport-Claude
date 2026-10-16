请仅根据以下完整摘要判断研究相关性，供后续全文评审选择候选：

{papers_text}

返回 JSON 数组（严格按照论文顺序）：
[
  {{
    "index": 0,
    "score": 8.0,
    "relevance": 8.0,
    "best_direction": null,
    "reason": "一句话说明摘要中的研究问题与该方向的关系；全文质量尚未评审"
  }},
  ...
]

注意：
- index 是上面论文编号对应的整数，不能遗漏、重复或超出范围
- best_direction 只能从 [{direction_ids}] 中选择最匹配的方向；无相关方向时为 null
- score 与 relevance 相同，均为 0 到 10 之间的有限数值，保留一位小数
- 不输出创新性、实操性或全文质量评分；不设置额外推送阈值
- 只返回 JSON 数组，不要包含其他文字
