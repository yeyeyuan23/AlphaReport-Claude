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
鉴权、按重置代次排序的会话队列、命令和回复。`/report`、`/daily`、`/weekly` 仅支持私聊，并共用任务锁。

普通聊天在单个机器人进程中按 `(chat_id, reset_generation)` 排队；入队时绑定重置代次，
出队时读取最新会话映射与 revision。正常保存、压缩和缺失会话恢复只更新 revision，
显式 `/clear` 才增加持久化重置代次，因此正常的前一轮保存不会作废后一轮。
会话保存仍使用 revision 和代次校验，清除后的新代次可立即独立运行。

`bot.sessions` 在现有 `sessions.db` 中兼容添加重置代次与 `message_receipts` 表。
收件 ID 绑定原会话、发送者、路由类型和正文指纹；完成结果在发送前持久化，
发送成功还须取得全部有效消息 ID。活动中的重复收件由进程内 claim 忽略，
已完成但未投递的收件只重发缓存，失败执行可重试，已投递或被清除的收件不重复执行。
`/clear` 的状态变更与自身确认消息缓存同事务保存，报告命令接受后缓存确认再投递。
收件 ID、阶段和分片生成稳定 UUID；群回复同样使用 UUID，且每个尚未发送的分片都核对重置代次。
该设计不改造 HTTP inbox 消费机制、不协调多个机器人进程，也不取消已运行的子进程；
外部操作与本地完成记录之间的崩溃窗口不具备全局恰好一次保证。

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
CLI 和 `/api/v1/skills` 使用同一 registry，只读取配置的仓库技能目录。
`cli.skills` 提供内置任务列表和详情，资源解析限制在各技能目录内。
聊天根据用户提到的标题或上下文查库，内部解析研报 ID 后调用共享 CLI；
不要求用户查询数据库编号，也不为分类、评分和卡片增加独立交互步骤。

技能指导模型工作；权限、输入与响应校验、缓存和发送回执由代码执行。
读取已有结果不需要调用模型。

## 主要业务流程

在线新鲜度由独立的 `announced_at` / `announcement_info_json` 保存，不改写 `published_date`。
`report_periods.publication_time/publication_column` 共用相同窗口查询：arXiv、东方财富和 BigQuant
必须具备来源公告时间，不能回退到原始提交、引用报告或入库日期；本地 PDF 保留有证据的发表日期规则。
迁移只加列和索引，不用旧提交日期猜测历史公告时间；后续来源证据可补全，较新的合格公告可更新该字段，
缺时间的材料补取不能清空它。后台查询返回两类日期及来源证据。

arXiv 公告模式由 `arxiv_catalog` 解析官网 `/list/<category>/recent` 的日期索引与条数，
以 q-fin 全归档及 cs.LG/stat.ML/cs.CL/cs.AI/math.OC 发现候选。根据索引直接定位窗口对应的批次偏移，
仅分页取齐目标日期，不扫描整月历史；2000 是单页容量，100 是摘要批量请求容量，都不是总候选上限。
列表保留最近 5 个公告批次；超出范围、日期/计数在分页中变化、重复或缺页时记录覆盖不足。
列表的新论文和跨分类条目进入候选，替换更新不会出现在这个入口。
`AnnouncementBatch.released_at` 根据官方日程，将列表日期换算为前一晚 `America/New_York` 20:00，
使用 ZoneInfo 处理夏令时，并将列表日期、链接及日程依据保存在公告证据中；不把 API 提交日期或 RSS 午夜批次时间当公开时刻。
`arxiv_announcements` 按编号批量读取 API 摘要，复用现有 Atom 解析；API 不可用或漏项时，
`arxiv_metadata` 从官方摘要页补齐。返回的版本号固定摘要与后续 PDF；原始提交日期独立保留。
网络请求串行限速，超时、429 和临时服务错误有限重试；一个分类失败仍保留其他分类的结果。
三个来源及采集服务共用 `date_window.candidate_in_window`，统一公告/原发表日期选择、UTC 比较与截止点排除规则。
东方财富在来源端按日期查询并遵循 TotalPage。BigQuant 通过公开 `collections.info` 解析集合身份，
再用 `documents.list` 的 `publishedAt DESC` 顺序发现候选；公告采集跨过窗口下界即停止，
不翻后续历史页、不逐篇请求 HTML 找日期。列表已有完整页面材料和附件链接，摘要优先提取标明的 Abstract。
每页容量是传输分页参数，不是总候选上限；接口返回的分页偏移、记录身份与排序都需校验。
日期缺失、重复页、偏移异常或排序违约保留可验证结果并记录覆盖不足，不回退为全历史扫描。
两者公告模式不使用标题关键词预筛，也不把日期字段补成午夜来定位小时窗口。
自动采集不设固定整批总时限，单次网络请求及PDF下载/解析保留超时。
手动 `publication` 搜索与定时 `announcement` 采集共用来源与入库服务，保留各自明确的日期语义。

- **采集与导入**：`collection.collect_reports` 负责手动和定时采集。自动在线采集先取得完整公告窗口内的标题、摘要、
  来源公告时间和身份，不按前N篇或标题词截断；先按方向筛选，达标才分类、入库并下载全文。缺摘要只允许复用库内同版本材料，
  仍不足则标为待补，不提前下载 PDF。返回整体 `status`、逐源 `source_statuses` 和入库统计，
  区分空结果、失败和部分成功。`local_pdf_import` 在启动时增量导入整个配置目录，
  共用分类与 repository，使用文件指纹和进程锁避免重复导入；元数据版本变化时对未变文件只复查日期/类型，不重新分类。日期未知或仅明确到年/月时不具备日/周报资格，不影响保留在库中；日期精度和证据、材料类型分别持久化，类型不等于来源；与手动采集共用不区分 PDF 扩展名大小写、只返回文件的目录扫描。
  单篇保存和报告补全文同样遵守 repository 的日期、版本及派生状态合并规则；
  仅补材料且分类输入不变时保留当前分类与标签，不能用全文关键词重写摘要分类。
  导入与批量重分类共用分类材料解析：优先完整摘要，缺摘要时只读标明的 Introduction；
  从正文提取的 Introduction 参与输入指纹和并发保护。
- **相关性评分**：`paper_scoring` 读取启用方向与完整摘要，同时生成各方向分数和最佳方向；
  `DirectionFilter` 是明细视图。统一保存 `selection_relevance`、`selection_direction`
  与 `selection_provenance_json`，后者包含方向明细、材料及规则指纹。
  缺材料与不相关分开处理；失败不覆盖已有成功结果，过期评分不进入候选。
  方向明细和普通评分共用批次/保存失败判定，有效同批结果先保存，失败再向 CLI 传播。
- **分阶段分析**：`batch_analysis` 与 `one_report` 使用 `AnalysisAgent`，分别读取
  `analysis.yaml`、`one-report.yaml`；摘要汇总共用 `summary.yaml` 和 `services.summary`。
  批量缓存区分 quick/standard/deep、材料、模型与资源；完整性与并发检查通过后才保存。
  单篇阅读固定 arXiv 版本、提取 PDF 全文，保存产物和研报库，再按授权会话投递。
  分阶段分析与全文评估独立复用缓存；评估优先使用库中当前有效结果，再考虑有效产物缓存。
- **全文阅读评估**：`report_quality` 使用 `quality-review.md` 和 `grounding.md`，
  将研究问题、方法、发现、优势、局限与评估把握存入 `evaluation_json`。
  材料、模型、提示词和相关性决定缓存是否有效。默认完整输入上限为 240,000 字符；
  缺全文或超限保持待核验。引文供查阅，逐字匹配不参与推荐或展示判断；
  文本块编号是提取偏移，不是 PDF 页码。模型评估不代表复现实验。
- **日报**：`daily_push` 按来源 `announced_at` 读取报告当天北京时间 08:00 前 24 小时公开发布的在线资料，刷新失效相关性，按方向轮流处理候选；
  复用有效评估，默认评估全部符合条件且需更新的论文，
  缺全文项不占预算。随后按 `daily-report/briefing.md` 渲染，最多重点推荐 5 篇。
  来源覆盖读取最近定时采集记录，不能说成本次在线检查；过程锁和 `ReportRun` 记录本次运行。
- **周报**：`weekly_report` 按同一来源 `announced_at` 查询报告当天北京时间 08:00 前 168 小时公开发布的库中在线资料，刷新相关性并默认完成全部符合条件的新全文评估，使用 `weekly-report/briefing.md` 分层排版。
  有效缓存不占预算，未完成评估的条目保留待核验；`--no-llm` 只读取缓存。
- **可选评估预算**：日/周报共用 `push_settings.max_reviews`，省略或 `null` 表示不限，
  正整数只计新全文评估；CLI `--max-reviews` 优先。预算由 `report_quality.resolve_review_budget`
  统一校验，报告说明超出预算的候选数量。日报的相关性刷新显式使用 `--max-papers 0`，
  候选查询不设固定条数上限。`report_periods` 统一以报告当天北京时间 08:00 为截止点生成 24/168 小时窗口并转换为 UTC 查询，均使用 `[start, end)`；入库日期维护查询单独使用自然日边界；日报通过 `--published-date` 让评分与候选范围一致，标题为报告当天，每篇分别保留公告时间与原始提交/发表日期。整批筛选和机器人报告任务不设固定总时限；
  单篇全文评估保留 240 秒时限，下载、解析和模型请求保留各自的超时保护。
- **报告投递**：日/周报不采集新条目；已在库中的 arXiv 资料缺全文时可补取 PDF。
  日/周报每次执行都按本期资料生成内容，重复执行会再次发送。
  单篇阅读按产物和目标会话保存发送回执，用于避免重复投递。
  普通回复、单篇、日报、周报共用 `feishu_transport` 的客户端、请求和消息 ID 校验；
  卡片失败时回退完整文本，任一分片失败不能视为完整投递成功。
- **阅读展示**：`report_reads` 组合 repository 查询、有效分析和知识卡片。
  网页简报由 `report_views.map_analysis_to_briefing_sections` 唯一映射已有分析，
  详情 API 返回 `briefing_sections`，前端直接渲染；阅读评估展示保存的 `evaluation_json`。
  CLI 详情与网页详情共用有效性校验，过期分析及旧卡片不能直接作为当前结果展示。
  单篇保存和各查询入口的知识卡片都由 `knowledge_cards` 根据有效评估确定性生成，规则见 `knowledge-card.md`；
  `cli.narrative_brief` 使用同一服务，不额外调用模型，无有效评估时只显示摘要和待核验状态。
  `POST /api/v1/reports/{report_id}/research-briefing?force=true` 可调用模型生成叙事文本，
  提示词位于 `analyze-report/briefing.md`；返回文本不写入质量评估或改变推荐。
- **复现准备**：`reproduction` 从可用全文提取设置，原文事实须附可定位引文；
  建议方案单独标注，成功卡片保存到 `analysis_json.reproduction_preparation`。
  网页和普通聊天按需调用同一服务，不下载数据或运行实验。

Web 中三项定时任务独立配置：`daily_collection` 默认每天北京时间 08:00
调用 `scheduled_collection`，通过共享采集服务入库；`daily_report` 默认 09:00
调用与手动 `/daily` 相同的报告服务，两项每天含周末执行；`weekly_report` 默认每周五 09:00
在线程中调用与手动 `/weekly` 相同的周报服务。三项均由 `configs/app.yaml` 的 `enabled`、
`schedule_cron`、`timezone` 配置，周报另用 `day_of_week` 指定星期（`mon` 至 `sun`）。
`cli.scheduled_collect` 可按配置补跑采集。手动 `/daily`、`/weekly` 不依赖定时开关。
健康接口的 `schedule` 包含周报开关和下次执行时间；已有外部周报调度需移除或关闭内置任务以避免重复发送。

## 后台处理与维护工具

下载、解析、正文清洗、公式/表格提取、去重、缓存和索引维护是业务内部步骤。
正常阅读和启动导入不要求先运行维护命令。用户功能及部署入口见 [README](../README.md#功能与使用)。

### PDF、公式与表格解析

`services.pdf_extraction` 统一字节与路径提取；新下载和已缓存 arXiv PDF 共用解析阶段的超时与日志策略。arXiv 和通用附件下载使用 PyMuPDF；
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

### 分类、批量分析与评分

这些 CLI 供聊天工具和部署者调用，普通用户无需记住研报编号或分类代码。
分类默认在采集与导入时完成，相关性评分由采集和报告流程调用；只有需要重跑时才手动使用。
以下 ID、分类和日期均为示例，调用前查询当前资料；模型操作可能产生费用。

```bash
# 查询已存材料；--full 包含提取全文，详情自动隐藏过期分析
.venv/bin/python -m reportagent.cli.db_query --search '因子模型' --limit 20
.venv/bin/python -m reportagent.cli.db_query --get 12 --full
.venv/bin/python -m reportagent.cli.db_query --list --json-output data/reports-export.json
# 分类：--report-id / --recent / --all 三选一；可用 --dry-run 预览
.venv/bin/python -m reportagent.cli.classify --recent 20 --hybrid --dry-run
# 分析：--report-id / --recent / --all / --unanalyzed / --topic 五选一
.venv/bin/python -m reportagent.cli.analyze --report-id 12 --depth standard
.venv/bin/python -m reportagent.cli.analyze --topic factor_model --depth standard
.venv/bin/python -m reportagent.cli.analyze --unanalyzed --depth quick
# 摘要相关性初筛，不是全文质量评估；预览也会调用模型
.venv/bin/python -m reportagent.cli.llm_filter --published-date 2026-10-17 --threshold 6 --batch-size 10 --dry-run
.venv/bin/python -m reportagent.cli.llm_direction_filter --report-ids 12,18 --dry-run --json
# 从已保存且有效的全文评估投影卡片，不额外调用模型
.venv/bin/python -m reportagent.cli.narrative_brief --report-id 12 --dry-run
# 按需核对复现设置；--dry-run 不写数据库，但可能调用模型
.venv/bin/python -m reportagent.cli.reproduction --report-id 12 --dry-run
```

分析深度为 `quick`、`standard`、`deep`。批量分析结果保存在 `analysis_json`，
摘要分析与全文分析分别标明材料范围；它不会自动成为全文质量评估或发送飞书消息。
`--force` 可重新分析；缺失原文设置不能推测成已报告事实。
普通评分与方向明细共用输入、提示词、材料指纹和失败判定；
`cli.llm_filter` 默认最多处理 200 篇，`--max-papers 0` 表示所选窗口内不限，日报已使用不限设置。
有效同批结果先保存，模型或保存失败仍向调用方返回失败，不将待补材料当成低分。

部署镜像的 Claude Code 权限只放行已列出的业务 CLI；这里新增调用方时应同步检查
`deploy/claude-settings.json`，不放开通用 Shell 来绕过缺失的专用权限。

### 采集补跑与报告预览

默认自动采集与日报由 Web 调度，CLI 用于部署者补跑或聊天按需调用；
`--dry-run` 只控制投递/保存范围，不保证免费或完全只读。

```bash
# 按当前部署配置补跑同一采集任务；会联网、分类和更新资料
.venv/bin/python -m reportagent.cli.scheduled_collect
.venv/bin/python -m reportagent.cli.collect --topics '因子模型,高频交易' --sources arxiv,eastmoney,bigquant --days 7 --max 15 --no-analysis
# 单篇产物不指定目标时仅生成；已有产物可在明确授权目标后投递
.venv/bin/python -m reportagent.cli.report --arxiv-id 2005.13665
.venv/bin/python -m reportagent.cli.report --artifact data/one_reports/2005.13665v3.json --chat-id oc_your_chat_id
# 日报预览会刷新评分与评估、保存运行记录，不发送
.venv/bin/python -m reportagent.cli.daily_feishu_push --dry-run --output data/daily-preview.md
# 周报仅读缓存的模板预览；weeks-ago 每增加1，将7天窗口前移7天
.venv/bin/python -m reportagent.cli.weekly_report --dry-run --no-llm --output data/weekly-preview.md
.venv/bin/python -m reportagent.cli.weekly_report --weeks-ago 1 --dry-run
```

`--force-review` 用于日/周报强制更新评估，`--max-reviews N` 覆盖共享的可选评估预算，
默认不限。日报的 `--report-ids` 是显式检查已选论文，仅跳过摘要相关性候选限制，仍须满足报告当天北京时间 08:00 前 24 小时的来源公告窗口，
不用于自动采集。单篇产物文件名以实际版本为准，成功投递回执保留；产物中的旧评估不能覆盖库中当前有效的评估。

## 修改与验证

业务逻辑放共享服务，入口负责适配；应用代码不反向导入 `scripts/`。
修改入口、文件位置或参数时，同步检查 Shell、Docker、技能说明与测试中的调用。
维护阅读 API 地址、材料及分析的缓存指纹、内置任务资源与单篇投递回执的一致性。
空研报 ID、多候选与参数错误须明确处理，不能默默选择第一项；单条入库失败须先回滚再处理后续记录。
干净 checkout 缺少 `frontend/dist/` 时应能导入应用和运行后端测试。

检查命令见 [README](../README.md#维护与排查)，离线测试和真实材料验收的分工见
[tests/README.md](../tests/README.md)。前端构建与 lint、程序正确性与研究内容质量分别验证。
