你是量化金融研究助手。请根据以下论文内容，生成一份结构化的 Knowledge Card。

论文标题：{title}

论文内容：
{content}

请严格按照以下 JSON 格式返回（只返回 JSON，不要任何其他文字）：
```json
{{
  "summary": "原文总结 — 用2-3句话概括论文的研究问题、方法和核心结论",
  "highlights": ["关键发现1", "关键发现2", "关键发现3"],
  "methodology_steps": ["方法步骤1：...", "方法步骤2：...", "方法步骤3：..."],
  "results": "实证结果描述 — 论文的主要实证发现和数据支撑",
  "marginal_contributions": "边际贡献 — 相对于现有文献，这篇论文的增量贡献是什么",
  "implications": ["实践启示1", "实践启示2", "实践启示3"]
}}
```

要求：
- summary: 2-3句精炼总结
- highlights: 3-5个关键亮点，每条一句话
- methodology_steps: 按步骤顺序描述方法，3-6条
- results: 描述主要实证结果，包含关键数据
- marginal_contributions: 说明相对已有研究的增量贡献
- implications: 2-4条对量化投资实践的启示
- 所有文本用中文
- **公式格式**：所有数学公式必须使用 LaTeX 语法。行内公式用 $...$ 包裹，独立公式用 $$...$$ 包裹。示例：$\alpha_i$、$$R_t = \mu + \sigma \varepsilon_t$$。变量名、希腊字母、上下标、分数、求和等一律用 LaTeX 表达，禁止使用纯文本如 "alpha" 或 "R_t"。