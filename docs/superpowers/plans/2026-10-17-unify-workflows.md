# Workflow Consolidation Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans task-by-task. Independent modules may be delegated under dispatching-parallel-agents; dependent interfaces are integrated in the order below.

**Goal:** 让现有入口复用同一套业务执行，消除已确认的七组重复与双 Web 业务实现。

**Architecture:** 以现有共享服务为核心，删除旧 Graph/API/chat，现行 bot/CLI/API 调用共享服务。复用现有校验、
指纹、并发保护和发送回执，保留合理策略差异，不另建一套替代系统。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、现有 LLM/飞书 SDK。

**Spec:** `docs/superpowers/specs/2026-10-17-unify-workflows-design.md`

## Global Constraints

- Python 3.12，无新增依赖，不改密钥、系统调度和外部服务；自动测试必须隔离真实数据库。
- 服务保持停止；不调用真实模型、不发送飞书、不用电脑控制。
- 保留机器人功能、CLI 与阅读接口；按用户进一步明确的范围下线旧聊天/任务 API 和 legacy_app。

## Review Focus

- 旧采集入口精确时区/目录、取消发生在评分后及保存前，不能绕过统一门槛（任务 1）。
- 全方向明细不能与主 selection 字段产生两份不一致判断，失败不能擦除旧成功（任务 2）。
- 模型流中途失败、取消和并发修改不能保存不完整或过时分析（任务 3、4）。
- 维护 PDF CLI 的旧缓存、空结果和并发改动不能产生错误成功提示（任务 5）。
- 卡片失败、长文本某段失败、/clear 时序以及备用传输必须一致（任务 6、7）。

## Tasks

- [x] 1. 采集统一：为共享服务增加 criteria、进度、取消适配；删除旧 Graph/API/chat，
  删除独立业务节点。新回归证明预筛、精确条件与部分成功一致。
- [x] 2. 评分统一：保留全方向详细输出，统一调用与落库政策及指纹；DirectionFilter/CLI
  变适配层。回归覆盖明细、缺材料、失败、阈值、并发与缓存。
- [x] 3. 分析统一：删除旧 API 和聊天的重复编排，batch service保留统一进度/取消；
  测试覆盖 quick/standard/deep、失败、完整性与并发保护。
- [x] 4. 摘要统一：共享 skill 提示构造与生成服务，复用到 AnalysisAgent，删除旧 API/chat 的内嵌提示词；
  测试覆盖已有分析、原文输入上限、空返回、持久化与失败。
- [x] 5. PDF 统一：维护 CLI 复用安全获取/缓存，统一基础解析和 MinerU fallback；
  用临时 PDF/DB 覆盖保存和失效，保持可选解析策略。
- [x] 6. 投递统一：日报/周报使用共享 SDK 请求、回执与分片回退，保留模板；
  验证各消息类型、receive_type、message_id、单篇 UUID 与部分失败。
- [x] 7. 消息处理统一：规范化 WS/HTTP 事件后单一路径处理，保留 /clear 快照时序；
  验证鉴权、群/私聊、命令/聊天、空文本、失败与快照。
- [x] 8. 应用统一与清理：只保留 main.app，移除 legacy_app 与旧聊天框架及依赖；
  检查未接线旧业务删除、文档同步与所有调用方。
- [x] 9. 整体验证：全量离线 Python、Ruff、前端检查、shell/diff，独立审查统一边界；
  仅在所有检查通过后报告完成。

范围调整：用户在实施中明确以飞书机器人为唯一交互主线，确认删除 legacy_app；因此采用下线旧入口，而不是继续维护兼容适配。

每步实施顺序为失败回归 → 验证红色 → 最小共享实现 → 专项回归；最后整体验证。

## 集成记录

- 首先同步 Dot 更新至 44b8abe，保留 sources.factory、report_reads 与前端修复。
- 旧系统下线后，仅在现行服务/入口保留有效回归；不恢复无人调用的旧公式解释与流式摘要代码。
- 独立审查发现并修复私聊 p2p 类型规范化及全批缺摘要时的评分状态漏存。
- 测试隔离事故：早期一次采集回归触发真实本地 SQLite 初始化，至少新增 source_item_id 列；未回滚用户数据库。现已隔离引擎、会话和初始化入口。
- 验证使用临时数据库与离线替身；服务保持停止，不调用真实模型或发送飞书。
- 验证结果：第一批独立快照全量 844 项；第二批专项 175 项；第三批专项 167 项；最终快照全量 911 项（另有 4 个 subtests），前端 47 项测试及格式、lint、构建通过；Ruff、shell 与 diff 检查通过。
- 依赖清理保留 OpenAI SDK 为直接依赖，替代原 langchain-openai 的传递安装；未引入新的模型或框架。
