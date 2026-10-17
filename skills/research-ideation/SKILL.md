---
name: research-ideation
description: 根据有限文献提出候选研究假设、最接近的已有工作、最小验证与否定条件；明确检索范围，不把检索未命中当成新颖性证据。
metadata:
  aliases: [研究创意头脑风暴]
---

使用 reportagent.services.research_ideation；飞书普通聊天可以调用同一 CLI：
```bash
.venv/bin/python -m reportagent.cli.ideation --topic '波动率预测' --angle methodology
```
可用角度：general、methodology、factor、cross_pollination、critique、data。
用户明确要求外部查新时增加 `--verify-external`，需已配置 Tavily。网页搜索只提供待核对线索，不代表核对全文或确认创新。

先按 `docs/architecture.md` 的索引维护说明准备向量索引，再取得相关文献，保留完整摘要；有当前有效全文评估时加入原文方法、发现与局限。
程序区分检索失败与未命中；失败时停止推断领域现状，空库时只生成“未查新的探索假设”。
模型只接收程序取得的资料，没有额外工具；提示词见 system.md 和 user.md。
输出每个候选假设的最接近文献、候选差异、数据需求、最小验证和否定条件。不要保证数据可得或收益改善。
只引用实际取得的论文ID；资料范围有限时，不声称领域无人研究。任务不授权发送消息。
