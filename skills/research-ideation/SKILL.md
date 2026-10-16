---
name: research-ideation
description: 围绕用户指定的量化金融主题，从已有研报中寻找灵感、发现研究空白、提出新思路
metadata:
  aliases:
  - 研究创意头脑风暴
  default_params:
    topic: ''
    angle: general
---

我想围绕「{topic}」进行头脑风暴。

请按以下步骤执行：
1. 用 semantic_search_reports 搜索与「{topic}」相关的已有研报
2. 用 brainstorm_research 对「{topic}」进行深度创意分析（思考角度：{angle}）
3. 如果发现了特别相关的论文，用 get_report 查看详情
4. 综合所有信息，给我一份有深度、有启发性的回答

注意：不要只是罗列论文，要帮我发现研究机会、识别空白、提出创新思路。

Use the shared Python tools/services for execution. The task template above is a workflow, not permission to send messages.

The research partner persona is defined in [system.md](system.md), loaded by
`brainstorm_research` unless its path is explicitly overridden in app settings.
