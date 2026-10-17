# 架构与开发约定

飞书负责交互与推送，Web 提供研报检索和阅读，CLI 支持手工操作与外部调度。
Python 服务和 Claude Code 子进程在部署主机运行，模型推理通过 API 完成。
安装、使用、部署和排查见 [项目首页](../README.md)。

## 入口与调用关系

| 入口 | 实现 | 职责 |
|---|---|---|
| 飞书机器人 | `reportagent.bot.feishu` | 接收消息、校验发送者、处理快捷命令和会话 |
| Web | `reportagent.main:app` | 阅读 API、前端静态文件、启动导入与定时任务 |
| 命令行 | `reportagent.cli.*` | 解析参数、调用共享服务 |

```text
飞书普通聊天 → Claude Code → 技能说明、文件/Shell 工具 → 共享 CLI
飞书 /report、/daily、/weekly → 对应 CLI ─┐
人工操作 / 外部调度 → CLI ─────────────┼→ 共享服务 → 数据源、模型 API、SQLite、飞书
网页 → Web API ──────────────────────┘
Web 启动 → 本地 PDF 增量导入；独立调度 → 08:00 采集、09:00 日报
```

本机和 Docker 共用 `start.sh`，Web 入口为 `reportagent.main:app`。
WebSocket 和 HTTP inbox 只提取消息字段，交给 `handle_incoming_message` 处理
鉴权、会话快照、命令和回复。`/report`、`/daily`、`/weekly` 仅支持私聊，并共用任务锁。

## 配置、技能与实现的边界

| 位置 | 管理的内容 |
|---|---|
| `configs/` | 模型、数据库、数据源、分类、研究方向与定时设置 |
| `skills/<任务>/` | 任务说明、专属提示词和输出模板 |
| `reportagent/skills/registry.py` | 技能发现、别名解析和资源定位 |
| `reportagent/services/` | 采集、分析、持久化、评分、投递和数据转换 |
| `reportagent/bot/`、`api/`、`cli/` | 各入口的输入输出适配 |
| `reportagent/db/`、`llm/`、`sources/` | 数据访问、模型调用和数据源实现 |
| `scripts/ops/` | 运维启动与维护工具 |

任务定义跟随技能管理，运行设置归入 `configs/`，详见 [配置说明](../configs/README.md)。
普通聊天的通用系统提示位于 `bot/claude.py`，并附加 registry 读取的技能说明。
CLI 和 `/api/v1/skills` 使用同一 registry；用户技能默认保存在 `data/user_skills/`。
`cli.skills` 提供列表和详情，保存、更新与删除使用 registry 的 Python 接口。
资源解析须限制在技能目录内，用户提供的名称不能直接成为文件路径。

技能指导模型工作；权限、输入与响应校验、缓存和发送回执由代码执行。
读取已有结果不需要调用模型。

## 主要业务流程

- **采集与导入**：`collection.collect_reports` 负责手动和定时采集。在线来源先取得标题、摘要、
  日期和身份，分类并按方向筛选；达标才入库并获取全文。摘要不足时可先暂存补取材料，
  筛选未通过则清理暂存内容。返回整体 `status`、逐源 `source_statuses` 和入库统计，
  区分空结果、失败和部分成功。`local_pdf_import` 在启动时增量导入整个配置目录，
  共用分类与 repository，使用文件指纹和进程锁避免重复导入。
- **相关性评分**：`paper_scoring` 读取启用方向与完整摘要，同时生成各方向分数和最佳方向；
  `DirectionFilter` 是明细视图。统一保存 `selection_relevance`、`selection_direction`
  与 `selection_provenance_json`，后者包含方向明细、材料及规则指纹。
  缺材料与不相关分开处理；失败不覆盖已有成功结果，过期评分不进入候选。
- **分阶段分析**：`batch_analysis` 与 `one_report` 使用 `AnalysisAgent`，分别读取
  `analysis.yaml`、`one-report.yaml`；摘要汇总共用 `summary.yaml` 和 `services.summary`。
  批量缓存区分 quick/standard/deep、材料、模型与资源；完整性与并发检查通过后才保存。
  单篇阅读固定 arXiv 版本、提取 PDF 全文，保存产物和研报库，再按授权会话投递。
- **全文阅读评估**：`report_quality` 使用 `quality-review.md` 和 `grounding.md`，
  将研究问题、方法、发现、优势、局限与评估把握存入 `evaluation_json`。
  材料、模型、提示词和相关性决定缓存是否有效。默认完整输入上限为 240,000 字符；
  缺全文或超限保持待核验。引文供查阅，逐字匹配不参与推荐或展示判断；
  文本块编号是提取偏移，不是 PDF 页码。模型评估不代表复现实验。
- **日报**：`daily_push` 读取最近四个 UTC 日期的库中资料，刷新失效相关性，按方向轮流处理候选；
  复用有效评估，默认评估全部符合条件且需更新的论文，
  缺全文项不占预算。随后按 `daily-report/briefing.md` 渲染，最多重点推荐 5 篇。
  来源覆盖读取最近定时采集记录，不能说成本次在线检查；过程锁和 `ReportRun` 记录本次运行。
- **周报**：`weekly_report` 查询上一完整 UTC 周发表的全部库中资料，刷新相关性并默认完成全部符合条件的新全文评估，使用 `weekly-report/briefing.md` 分层排版。
  有效缓存不占预算，未完成评估的条目保留待核验；`--no-llm` 只读取缓存。
- **可选评估预算**：日/周报共用 `push_settings.max_reviews`，省略或 `null` 表示不限，
  正整数只计新全文评估；CLI `--max-reviews` 优先。预算由 `report_quality.resolve_review_budget`
  统一校验，报告说明超出预算的候选数量。日报的相关性刷新显式使用 `--max-papers 0`，
  候选查询不设固定条数上限。整批筛选和机器人报告任务不设固定总时限；
  单篇全文评估保留 240 秒时限，下载、解析和模型请求保留各自的超时保护。
- **报告投递**：日/周报不采集新条目；已在库中的 arXiv 资料缺全文时可补取 PDF。
  日/周报每次执行都按本期资料生成内容，重复执行会再次发送。
  单篇阅读按产物和目标会话保存发送回执，用于避免重复投递。
  普通回复、单篇、日报、周报共用 `feishu_transport` 的客户端、请求和消息 ID 校验；
  卡片失败时回退完整文本，任一分片失败不能视为完整投递成功。
- **阅读展示**：`report_reads` 组合 repository 查询、有效分析和知识卡片。
  网页简报映射已有分析，阅读评估展示保存的 `evaluation_json`。
  知识卡片由 `knowledge_cards` 根据有效评估确定性生成，规则见 `knowledge-card.md`；
  `cli.narrative_brief` 使用同一服务，不额外调用模型，无有效评估时只显示摘要和待核验状态。
  `POST /api/v1/reports/{report_id}/research-briefing?force=true` 可调用模型生成叙事文本，
  提示词位于 `analyze-report/briefing.md`；返回文本不写入质量评估或改变推荐。
- **复现准备与研究创意**：`reproduction` 从可用全文提取设置，原文事实须附可定位引文；
  建议方案单独标注，成功卡片保存到 `analysis_json.reproduction_preparation`。
  `research_ideation` 使用语义索引检索文献，再让模型提出有范围声明的候选假设，
  外部查新可按需使用 Tavily；检索失败与未命中分开处理。

Web 中两项定时任务独立配置，每天含周末执行：`daily_collection` 默认北京时间 08:00
调用 `scheduled_collection`，通过共享采集服务入库；`daily_report` 默认 09:00
调用与手动 `/daily` 相同的报告服务。`cli.scheduled_collect` 可按配置补跑采集。
手动 `/daily`、`/weekly` 不依赖定时开关，周报自动运行使用外部调度。

## 内部处理与维护工具

下载、解析、正文清洗、公式/表格提取、去重、缓存和索引维护是业务内部步骤。
正常阅读和启动导入不要求先运行维护命令。用户功能及部署入口见 [README](../README.md#功能与入口)。

### PDF、公式与表格解析

`services.pdf_extraction` 统一字节与路径提取。arXiv 和通用附件下载使用 PyMuPDF；
本地 PDF 先读取原生元数据，再尝试 MinerU，并按可用情况合并 Docling 结果。
`cli.parse_pdf` 尝试本地 MinerU，不可用或没有有效内容时回退 PyMuPDF。
可选解析器需另行安装，基础解析不保证公式与表格结构完整。

```bash
.venv/bin/python -m reportagent.cli.parse_pdf --pdf /path/to/paper.pdf --output data/pdf-preview.json
.venv/bin/python -m reportagent.cli.parse_pdf --report-id 12 --save
```

`--pdf` 用于预览，不接受 `--save`。`--report-id --save` 通过 `pdf_material`、`report_pdf`
复用下载限额和原子缓存，核对数据库与文件是否仍匹配，完整替换正文、表格、公式，
并使派生分析与展示失效；失败或并发更新不能覆盖原结果。

研报默认保存在 `data/report_library.db`，下载的 PDF 在 `data/pdfs/`。
本地导入的 PDF 留在配置目录，数据库记录其原路径；部署时应持久化整个资料目录。
单篇产物在 `data/one_reports/`，采集记录在 `data/collections/`，日报产物在 `data/daily_reports/`。

### 向量索引准备与重建

语义检索与研究创意使用 `vector_store` 的 Chroma 索引，它独立于 SQLite，
普通采集和 `/report` 不自动填充索引。用途和查询示例见 [README](../README.md#按意思查找论文语义检索)。

```bash
.venv/bin/python -m reportagent.vector_store.populate --source arxiv --limit 100
.venv/bin/python -m reportagent.vector_store.populate --report-id 12
```

构建索引会运行 embedding，可能下载本地模型或调用模型 API；设置由 `vector_store` 管理。
默认处理含全文论文，`--clear` 删除已有索引再重建。资料更新或删除后须核对索引版本。

## 修改与验证

业务逻辑放共享服务，入口负责适配；应用代码不反向导入 `scripts/`。
修改入口、文件位置或参数时，同步检查 Shell、Docker、技能说明与测试中的调用。
维护阅读 API 地址、材料及分析的缓存指纹、用户技能与单篇投递回执的一致性。
空研报 ID、多候选与参数错误须明确处理，不能默默选择第一项；单条入库失败须先回滚再处理后续记录。
干净 checkout 缺少 `frontend/dist/` 时应能导入应用和运行后端测试。

检查命令见 [README](../README.md#自动检查ci)，离线测试和真实材料验收的分工见
[tests/README.md](../tests/README.md)。前端构建与 lint、程序正确性与研究内容质量分别验证。
