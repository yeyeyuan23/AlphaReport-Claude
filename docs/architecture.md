# 架构与开发约定

AlphaReport 在本机运行 Python 服务和 Claude Code 子进程，通过模型 API 完成分析。
飞书负责交互与推送，网页负责检索和阅读研报，CLI 支持人工操作与外部调度。
安装、使用、部署和排查统一见 [项目首页](../README.md)。本文只维护开发架构与边界。

## 入口与调用关系

| 入口 | 实现 | 职责 |
|---|---|---|
| 飞书机器人 | `reportagent.bot.feishu` | 接收消息、校验发送者、处理快捷命令和会话 |
| 默认 Web | `reportagent.main:app` | 提供研报阅读 API 与前端静态文件 |
| 命令行 | `reportagent.cli.*` | 解析参数、调用共享服务 |
| 历史完整 Web | `reportagent.api.legacy_app:app` | 可选的 LangGraph 聊天与任务 API，需完整依赖 |

```text
飞书普通聊天 → Claude Code 子进程 → 共享技能说明与工具
飞书 /report、/daily → 对应 CLI ─┐
人工操作 / 外部调度 → CLI ──┼→ 共享服务 → 数据源、模型 API、数据库、飞书
网页 → Web API ─────────────┘
```

默认网页保留研报库、PDF、简报和知识卡片阅读功能。
旧网页聊天、任务监控、Ask AI 和删除入口已移除；历史 API 的全部能力并未接入当前网页。
本地启动与 Docker 默认均使用 `reportagent.main:app`。

## 配置、技能与实现的边界

| 位置 | 管理的内容 |
|---|---|
| `configs/` | 模型、数据库、数据源、分类、研究方向、定时设置与通用聊天提示词 |
| `skills/<任务>/` | 内置任务说明、专属提示词和输出模板 |
| `reportagent/skills/registry.py` | 技能发现、别名解析和资源定位 |
| `reportagent/services/` | 采集、分析、持久化、筛选、推送和数据转换 |
| `reportagent/bot/`、`api/`、`cli/` | 各入口的输入输出适配 |
| `reportagent/db/`、`llm/`、`sources/` | 数据访问、模型调用和数据源实现 |
| `scripts/ops/`、`scripts/dev/` | 运维启动器与本地手工调试工具 |

任务资源按用途归档：分析用的 YAML 跟随分析技能，端口和模型地址放运行配置。
详细配置归属见 [configs/README.md](../configs/README.md)。

内置技能只有根目录 `skills/` 一份来源。飞书 Claude Code、LangGraph ReAct 和
Plan-Execute 都通过 registry 加载完整技能说明；CLI 和 `/api/v1/skills` 使用同一目录。
用户自定义 YAML 位于 `data/user_skills/`，原中文技能名称保留为别名。
资源解析必须限制在对应技能目录内，用户提供的名称不能直接成为文件路径。

技能说明指导模型工作；权限校验、分析完整性检查和发送回执由 Python 代码执行。
网页可以直接读取已有结果，无需让每个 HTTP 请求经过模型。

## 主要业务流程

- **来源采集**：适配器记录实际请求与解析结果，采集服务返回整体 `status` 和逐源
  `source_statuses`，保留部分结果、错误及原有入库统计。空结果与失败分开处理。
- **摘要初筛**：`paper_scoring` 读取 YAML 中启用的方向，保存相关性、最佳方向和
  评分来源指纹。方向、提示词或阈值变化后重新评分；相关性不充当质量结论。
- **飞书日报**：检查来源 → 查询最近四个 UTC 日期中评分指纹有效的候选 →
  按方向轮流选取最多 12 篇 → 全文证据评估 → 最多 5 篇学术重点及其他分栏 →
  模板渲染 → 按收件人发送并保存回执。全部来源失败时中止；部分来源失败显示覆盖状态。
  私聊 `/daily`、CLI 与启用后的 Feishu 调度复用
  [daily_push.py](../reportagent/services/daily_push.py)，进程锁阻止重叠运行，运行结果写入数据库。
- **共享全文评估**：[report_quality.py](../reportagent/services/report_quality.py)
  校验研究问题、方法、发现、优势、局限与原文引文。内容、模型、提示词和相关性
  共同决定缓存指纹，结果存为 `evaluation_json`。缺少全文、输入截断或引文失配时
  保留待核验状态；文本块编号不是 PDF 页码，引文匹配不代表复现实验。
- **单篇研报**：固定 arXiv 版本 → PDF 全文 → 分阶段分析与同一套全文评估 →
  保存产物及研报库 → 按授权目标投递。用户指定阅读未完成方向初筛时不显示相关性分数。
  [one_report.py](../reportagent/services/one_report.py) 按 PDF、模型和技能资源指纹复用缓存，
  强制刷新不清除已有回执。待核验评估可重试，并复用仍有效的阶段分析。
- **网页与排版**：日报通过 [report_briefing.py](../reportagent/services/report_briefing.py)
  将保存的评估填入 `skills/daily-report/briefing.md`；网页读取同一份评估，显示引文和局限。
  排版不再调用模型改写研究结论。`--dry-run` 可更新评估和运行记录，但不投递、不写发送回执。
- **飞书周报**：仍使用独立的 [weekly_report.py](../reportagent/services/weekly_report.py)，
  查询上一个完整 UTC 周的全部库中资料，以配置的 Python API 刷新过期相关性和最多
  12 篇尚未核验的全文评估；已有有效评估不消耗这次预算。再复用同一评估和
  `skills/weekly-report/briefing.md` 模板分层排版，不调用 Claude 重写结论。
  缺失/失效材料单列，相关性不能充当质量；尚无日报的按目标去重回执。

分析技能中的 `grounding.md` 约束原文证据与推断，`quality-review.md` 定义质量评估；
`daily-report/briefing.md` 和 `weekly-report/briefing.md` 是格式模板。
通用批量分析与单篇分析仍使用各自的阶段配置，摘要汇总共用 `summary.yaml`。
完整提取文本最多 240,000 字符，以带偏移文本块组织；超过限额明确待核验，不静默截断。
PDF连字、安全英文断行和空白规范化保留原始字符位置，不放宽为删句或数学符号模糊匹配。
修改这些资源后按相应指纹刷新缓存；模型阅读不等于复现论文实验。

知识卡片由 `services/knowledge_cards.py` 从当前有效的 `evaluation_json` 确定性生成，
网页、旧 API 和批量 CLI 共用同一服务；无当前证据时只展示摘要和待核验状态。
复现准备使用 `services/reproduction.py` 与 `skills/prepare-reproduction/`，
原文事实逐项定位，建议方案独立标注，成功卡片存入 `analysis_json.reproduction_preparation`。
模型调用前后核对源资料，失败或过期结果不覆盖成功缓存。
研究创意使用 `services/research_ideation.py`；检索由程序执行，模型仅生成有范围声明的候选假设，
不再声称具备生成阶段未接入的工具能力。

内置 Feishu 日报调度默认开启。Web 运行时按 `Asia/Shanghai` 工作日设定时刻
调用同一日报服务；独立采集模式仅负责入库。手动 `/daily` 不依赖调度开关。

## 内部处理与维护工具

用户功能、使用入口和部署步骤统一在 [README](../README.md#功能与入口)。下载、PDF 解析、
正文清洗、公式/表格提取、去重、缓存和索引维护是这些功能的内部步骤，不单独列为用户功能。
手工 CLI 用于开发检查或排查已有资料；正常阅读和导入不要求用户先运行它们。

### PDF、公式与表格解析

相关实现位于 `sources/arxiv_source.py`、`sources/local_pdf.py`、
`services/pdf_extraction.py` 和 `processors/`。各入口使用的解析路径应分别核对：
arXiv 下载流程使用 PyMuPDF 提取全文，`cli.parse_pdf` 调用 MinerU 提取服务并在不可用时
回退；本地 PDF 采集可尝试 MinerU、Docling，再回退 PyMuPDF。
可选解析器的存在不代表每条采集路径都已经接入。

从项目根目录手动检查解析结果：

```bash
.venv/bin/python -m reportagent.cli.parse_pdf --pdf /path/to/paper.pdf --output data/pdf-preview.json
```

输出包含文本预览和公式、表格数量。回退解析不保证公式和表格结构；PDF 页眉、页脚和
公式编号可能混入正文，需要与引文定位一并处理。`parse_pdf --save` 的持久化路径尚待
修复核验，不作为可靠的入库方式；用户导入本地论文使用 `collect --sources local_pdf`。

### 向量索引准备与重建

语义检索的用户用途和查询入口见 [README](../README.md#按意思查找论文)。
`reportagent/vector_store/` 使用 Chroma 与 embedding 模型建立论文片段索引，
该索引独立于 SQLite。普通采集和 `/report` 只写研报库，不自动填充向量库。

```bash
.venv/bin/python -m reportagent.vector_store.populate --source arxiv --limit 100
.venv/bin/python -m reportagent.vector_store.populate --report-id 12
```

索引构建会运行 embedding，可能下载本地模型或调用付费 API。默认处理含全文论文；
`--clear` 会删除已有索引再重建。模型设置读取 `vector_store` 独立配置，配置好聊天模型
不代表索引服务已就绪。论文更新或删除后，也需核对索引是否与库中版本一致。

### 实验工具映射

实验 API 的启动、功能用途和可用限制见 [README](../README.md#实验聊天与管理入口)。
以下是 `reportagent/chat/tools.py` 保留的 LangGraph 工具，供开发与接口排查使用；
它们没有直接注册为飞书 Claude Code 的同名工具。

| 工具 | 实现职责 |
|---|---|
| `collect_reports`、`search_reports`、`semantic_search_reports` | 收集资料、关键词与向量查询 |
| `get_report`、`analyze_report`、`delete_reports` | 读取、分析和删除指定研报 |
| `web_search`、`parse_document` | Tavily 网页搜索、PDF 增强解析 |
| `list_skills`、`manage_skill` | 通过共享 registry 查询和管理用户技能 |
| `brainstorm_research`、`recall_discussed_reports` | 研究思路讨论和已讨论文献回顾 |

## 修改与验证

新增业务逻辑放共享服务，入口只负责适配；应用代码不反向导入 `scripts/`。
旧业务脚本入口已移除，统一使用模块 CLI；迁移映射见 [scripts/README.md](../scripts/README.md)。
日报通过模块 CLI 隔离采集与摘要初筛的超时，全文评估由共享服务在渲染和发送前完成。
数据库查询和 PDF 命令复用 repository 与解析服务；历史固定 ID 补丁仅作维护用途。

修改入口或文件位置时，同时检查 Shell、Docker、导入、技能说明与测试中的路径。
保留 API URL、数据库、模型选择、中文别名、用户自定义技能和已有发送回执的兼容性。
干净 checkout 缺少 `frontend/dist` 时仍应能导入应用和运行后端测试。
依赖研报 ID 的工具遇到缺失或多个候选时应停止，不能默认选择第一篇。
批量入库中单条记录的事务失败后先回滚，再处理后续记录。

回归命令见 [项目首页的测试说明](../README.md#测试与内容验收)。测试使用临时文件、数据库和替代的
外部接口，不调用真实模型、采集源或消息发送服务。前端构建通过与 lint 通过分别验证。
