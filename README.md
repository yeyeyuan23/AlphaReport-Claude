# AlphaReport

量化金融研报的采集、分类、分析和推送工具。Python 服务和 Claude Code 在你自己的电脑或服务器上运行，模型推理通过 API 完成。飞书用于聊天与推送，网页用于检索和阅读研报。

本页是用户使用与部署说明；开发结构见 [docs/architecture.md](docs/architecture.md)。

- [功能与入口](#功能与入口)
- [安装与运行](#安装与运行)
- [飞书聊天与快捷命令](#飞书聊天与快捷命令)
- [采集与分析](#采集与分析)
- [飞书日报与周报](#飞书日报与周报)
- [部署与定时任务](#部署与定时任务)
- [扩展功能与可用范围](#扩展功能与可用范围)
- [配置与目录](#配置与目录)
- [维护与排查](#维护与排查)

## 功能与入口

| 用户功能 | 从哪里使用 | 用途与当前范围 |
|---|---|---|
| 连续聊天、研报问答、按指令操作项目 | 飞书普通文本 | 连续讨论研报、提出问题，并按允许的范围操作项目文件 |
| 一篇 arXiv 论文的全文分析与推送 | 飞书 `/report`、`cli.report` | 获取研究问题、方法、发现、局限及原文依据；机器人命令仅限私聊 |
| 研报库查询与管理 | 飞书快捷命令、网页、`cli.db_query`；删除使用实验聊天 API | 查询已保存的研报、最近入库内容和数量，支持命令行导出；默认网页不提供删除入口 |
| 搜索与导入研报 | `cli.collect`；库外网页搜索使用实验聊天 API | 搜索 arXiv、东方财富、BigQuant 或导入本地 PDF；网页搜索另需 Tavily，搜索结果不会自动入库 |
| 整理标签与批量重分析 | `cli.classify`、`cli.analyze` | 手动调整库中研报的分类，或重新分析一篇、多篇已有研报 |
| 按研究方向筛选论文 | `cli.llm_filter`、`cli.llm_direction_filter` | 找出摘要与关注方向匹配的论文；相关性初筛不代表研究质量结论 |
| 阅读原文、简报与知识卡片 | 网页、`cli.narrative_brief` | 对照 PDF 阅读研究解读，查看方法、发现、原文引证和局限 |
| 飞书日报 | 私聊 `/daily`、`cli.daily_feishu_push` | 收到有主次的新文章集合，最多重点推荐 5 篇，另列其他材料和待核验项 |
| 飞书周报 | 私聊 `/weekly`、`cli.weekly_report`；自动运行用外部调度 | 汇总上一个完整周发表的论文并发送 |
| 按意思查找论文（语义检索） | Python 查询、实验聊天 API | 按研究问题寻找相近论文与正文片段；需先准备索引，网页搜索框仍只支持关键词 |
| 围绕主题寻找研究思路 | Python 调用、实验聊天 API | 结合相关文献生成研究机会和方法建议，引用与新颖性需人工核对 |
| 保存可复用的聊天任务要求（自定义技能） | Python 调用、实验聊天 API | 将常用任务指令保存下来供聊天复用；不会自动改写单篇或日报的固定分析规则 |

下文的 `cli.*` 均表示 `python -m reportagent.cli.*`。**内置飞书日报调度默认开启：Web 持续运行时，工作日北京时间 08:00 自动采集、筛选并推送；单独启动机器人不启动调度器。**

表中实验入口的用途与限制见[扩展功能与可用范围](#扩展功能与可用范围)。下载、PDF/公式/表格解析、去重和索引维护属于内部处理，开发说明见[内部处理与维护工具](docs/architecture.md#内部处理与维护工具)。

## 安装与运行

### 1. 安装本机依赖

- Python 3.10+。
- 构建网页需要 Node.js 和 npm。当前锁文件的 Vite 要求 Node 20.19+ 或 22.12+；若也运行 lint，Node 22 应至少为 22.13。
- 普通飞书聊天需要在同一个运行账户的 `PATH` 中找到 `claude`。安装方式见 [Claude Code 官方文档](https://code.claude.com/docs/en/setup)。

在仓库根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements/runtime.txt
cp .env.example .env                # 首次配置时执行；已有 .env 不要覆盖
(cd frontend && npm ci)
mkdir -p data
claude --version
```

只运行机器人和单篇分析时，可安装轻量依赖 `requirements/bot.txt`；它不能替代完整 Web、调度器和向量检索所需的依赖。MinerU、Docling、Tavily 均为按需安装的扩展。

### 2. 配置飞书应用和模型

在 [飞书开放平台](https://open.feishu.cn/) 创建企业自建应用并开启机器人能力：

1. 记录应用的 App ID、App Secret，配置应用可用范围。
2. 在事件与回调中选择长连接接收事件，订阅代码使用的 `im.message.receive_v1`。
3. 开通接收用户消息、以机器人身份发送/回复消息所需权限；使用群聊时还需相应群消息权限，代码也会尝试添加消息表情。具体权限按后台及官方[接收消息](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)、[发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)说明配置。
4. 按平台要求发布应用版本，使配置对目标用户生效；群推送需先把机器人加入目标群。

长连接由本机主动连接飞书，无需为接收消息开放公网回调地址。第一次配置时可能需先启动机器人，再回开发者后台保存长连接订阅。

编辑本地 `.env`：

```dotenv
FEISHU_APP_ID=cli_your_app_id
FEISHU_APP_SECRET=your_app_secret
FEISHU_ALLOWED_OPEN_IDS=ou_your_open_id
DEEPSEEK_API_KEY=your_model_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic
CLAUDE_BOT_MODEL=deepseek-v4-pro
```

允许名单填操作者的 **open_id**，多个用逗号分隔；空名单拒绝所有人，群聊内也按发送者校验。open_id 可由飞书开发者工具查询，或发送消息后从本机拒绝日志确认。它和后面的 chat_id 是不同用途。

模型设置分几条路径：

| 路径 | 使用的设置 |
|---|---|
| 飞书普通聊天 | `CLAUDE_BOT_MODEL`；机器人会把 `DEEPSEEK_*` 映射给 Claude Code 子进程 |
| 单篇、相关性初筛和全文评估 | Python `LLMClient` 读取 `configs/app.yaml` 的 `llm` 设置；分析阶段及评估要求由 `skills/` 管理 |
| 日报排版 | 从保存的评估结果填充 `skills/daily-report/briefing.md`，此步不调用模型 |
| 周报的简报生成 | Python 模型 API 刷新过期相关性和全文评估，再从保存的证据填充 `skills/weekly-report/briefing.md` |

普通聊天需要可非交互运行的 Claude Code；单篇、批量分析以及日/周报的相关性筛选和全文评估使用配置的 Python 模型 API。周报复用全文评估，不再单独调用 Claude 改写研究结论。

### 3. 启动

```bash
bash start.sh --check                 # 只检查入口 Python 依赖
bash start.sh                         # 构建前端，启动 Web + 飞书机器人
bash scripts/ops/run_bot.sh           # 只启动飞书机器人
.venv/bin/python -m reportagent.main  # 只启动 Web
```

默认网页是 `http://127.0.0.1:8000`。`start.sh` 在前台运行，Ctrl+C 会停止它启动的子进程；端口被其他进程占用时退出，不会主动杀掉占用者。

仅启动 Web 不会接收飞书 WebSocket 消息。内置调度器遵循 `daily_report.enabled`，默认是 `true`；代码或配置更新后需重启对应服务。

## 飞书聊天与快捷命令

在已授权的私聊或群聊中使用：

| 输入 | 作用 |
|---|---|
| `/help`、`/start` | 查看帮助 |
| `/stats` | 总研报数、含全文和仅摘要篇数 |
| `/search 因子模型` | 搜索库中研报，最多返回 5 条 |
| `/recent` | 查看最近入库的 5 篇 |
| `/chatid` | 查看当前会话 ID，用于配置推送目标 |
| `/clear` | 清除当前会话映射与摘要，不删除研报 |
| `/report 2005.13665` | 分析一篇 arXiv 论文并推送回当前私聊，**仅限私聊** |
| `/daily` | 手动采集、筛选与全文评估，生成分层日报并推送到当前私聊，**仅限私聊** |
| `/weekly` | 汇总上一完整 UTC 周的库中资料，刷新评估并推送到当前私聊，**仅限私聊** |

`/report` 也接受 `https://arxiv.org/abs/2005.13665v3` 这样的链接，目前支持现代 arXiv 编号，一次只处理一篇。

其他文本进入 Claude Code 聊天，可用自然语言提出问题，例如：

```text
在研报库里找和波动率预测有关的论文，先列出标题与 ID。
比较库中第 12 和第 18 篇论文的方法，缺失信息注明未报告。
用现有 CLI 生成一份周报预览，先不要发送。
```

这些是交给模型理解的请求，不是固定命令。模型需要可用的 Shell/文件工具权限；旧 LangGraph 的工具名称没有直接注册给飞书 Claude Code。普通聊天上限 300 秒，长回复完整分段发送；短提示使用纯文本，含标题、链接或代码的回复使用卡片，表格转为逐项字段；每个 chat_id 保持独立会话，群里共用该群的会话。能收到哪些群消息取决于飞书订阅权限，代码没有强制“必须 @ 才处理”的过滤。

单篇分析要求可用 PDF 全文，关键分析阶段缺失时停止发送；同时保存可在网页查看的证据评估。用户指定阅读未完成方向初筛时不显示相关性分数。只有版本、PDF、模型与技能资源指纹一致时才复用分析；`--force` 可强制刷新，已有发送回执保留。产物位于 `data/one_reports/`，并写入研报库：

```bash
.venv/bin/python -m reportagent.cli.report --arxiv-id 2005.13665
# 确认产物和目的地后，显式发送已保存的简报：
.venv/bin/python -m reportagent.cli.report --artifact data/one_reports/2005.13665v3.json --chat-id oc_your_chat_id
```

产物文件名以实际返回的 arXiv 版本为准。同一产物对同一会话已有成功回执时不重复发送。

## 采集与分析

### 数据源与采集

| 来源 | 适用方式 | 全文情况 |
|---|---|---|
| arXiv | 关键词搜索、日期范围、`--broad` 广泛拉取量化金融论文 | 尝试下载 PDF；一般采集失败时可能只保存摘要 |
| 东方财富 | 券商研报、机构、标的等 | 取得真实文章正文，优先下载可访问的公开 PDF |
| BigQuant | 指定 Wiki 合集与文章 | 提取文章正文或公开 PDF 附件；提取明确的原报告或文章发表日期，仅有更新时间时保留未知 |
| 本地 PDF | 递归扫描 `sources.local_pdf.library_path`，默认 `data/pdf_library/` | 提取本地文件文本，按已安装解析器增强 |

```bash
.venv/bin/python -m reportagent.cli.collect --topics '因子模型,高频交易' --sources arxiv,eastmoney,bigquant --days 7 --max 15 --no-analysis
.venv/bin/python -m reportagent.cli.collect --topics '量化金融' --sources arxiv --broad --days 3 --no-analysis
.venv/bin/python -m reportagent.cli.collect --topics '因子模型' --sources local_pdf --no-analysis
```

`--max` 是每个来源的上限。`--keywords` 可补充关键词，`--date-from/--date-to` 指定日期范围，`--output` 保存采集统计 JSON。`--no-analysis` 只跳过深度分析，**仍会尝试模型分类**，失败再退回规则分类。

采集结果含整体 `status` 和逐源 `source_statuses`：逐源状态区分正常、空结果、部分成功、错误与不可用，并记录抓取数、有效数、全文数和安全错误摘要。分页、解析或 PDF 失败会保留已经取得的有效资料。

### 分类、评分、知识卡片与数据库工具

下面的研报 ID 应替换为自己库中的 ID；所有模块都支持 `--help`。

```bash
# 分类：默认规则，也可 --llm 或 --hybrid；--dry-run 预览分类
.venv/bin/python -m reportagent.cli.classify --recent 20 --hybrid --dry-run
# 分析：--report-id / --recent / --all / --unanalyzed / --topic 五选一
.venv/bin/python -m reportagent.cli.analyze --report-id 12 --depth standard
.venv/bin/python -m reportagent.cli.analyze --unanalyzed --depth quick
.venv/bin/python -m reportagent.cli.analyze --topic factor_model --depth standard
# 相关性初筛：最近四天发表、尚未评分或评分指纹已过期的论文
.venv/bin/python -m reportagent.cli.llm_filter --published-days 4 --threshold 6 --batch-size 10 --dry-run
# 另一种评分：按研究方向逐项打分
.venv/bin/python -m reportagent.cli.llm_direction_filter --report-ids 12,18 --dry-run --json
# 从已有分析生成叙事型知识卡片
.venv/bin/python -m reportagent.cli.narrative_brief --report-id 12 --dry-run
# 数据查询和导出
.venv/bin/python -m reportagent.cli.db_query --stats
.venv/bin/python -m reportagent.cli.db_query --search '因子模型' --limit 20
.venv/bin/python -m reportagent.cli.db_query --list --json-output data/reports-export.json
```

分析深度包括 `quick`、`standard`、`deep`；`--force` 可重新分析或生成卡片。`db_query` 还支持读取全文、手动更新分析/摘要/知识卡片。日常 PDF 入库使用 `collect --sources local_pdf`，无需单独运行解析命令。

网页提供关键词搜索、市场/资产/频率/主题筛选、统计、分页、PDF、研究简报和知识卡片。网页关键词搜索不是语义检索。知识卡片优先使用缓存或分析映射；叙事卡片重新生成用上面的 CLI。当前网页没有聊天、任务监控或删除入口。

## 飞书日报与周报

### 配置目的地

在 `.env` 中设置；以下 ID 仅是占位符：

```dotenv
FEISHU_DAILY_PUSH_IDS=oc_your_chat_id
FEISHU_DAILY_PUSH_TYPE=chat_id
FEISHU_WEEKLY_PUSH_IDS=oc_your_chat_id
FEISHU_WEEKLY_PUSH_TYPE=chat_id
ALPHAREPORT_BASE_URL=https://your-report-site.example
```

`open_id` 适合指定个人，`chat_id` 适合指定会话/群。多个目标用逗号分隔；CLI 的 `--receive-ids`、`--receive-type` 优先。周报未设置目标时同时复用日报目标和接收者类型；设置周报目标时使用周报类型（默认 `chat_id`）。

详情链接需要收件人的设备能够访问 `ALPHAREPORT_BASE_URL`；本机的 `localhost` 链接不能直接供其他设备访问。

### 日报

流程：来源诊断与采集 → 按当前启用方向进行摘要相关性初筛 → 从最近四天、评分指纹有效且待送达的论文中轮流选取各方向候选 → 最多 12 篇全文质量评估 → 最多 5 篇学术重点推荐 → 模板排版与按目标投递。

券商研报、社区材料、其他论文和待核验内容分栏展示。缺少全文、文本截断或引文无法定位的论文不能进入重点推荐；全部来源失败时中止，部分失败时保留可用资料并在简报中显示来源状态。飞书日报和网页展示同一份保存的 `evaluation`，不再由另一个模型重写结论。

```bash
# 使用库中论文预览；仍会刷新过期相关性评分和全文评估，可能调用模型
.venv/bin/python -m reportagent.cli.daily_feishu_push --no-collect --dry-run --output data/daily-preview.md
# 完整流程预览：仍会采集、入库、评分、全文评估
.venv/bin/python -m reportagent.cli.daily_feishu_push --dry-run --max-per-source 15
# 检查指定论文并强制刷新全文评估；跳过自动采集与摘要初筛
.venv/bin/python -m reportagent.cli.daily_feishu_push --no-collect --report-ids 12,18 --force-review --dry-run
# 确认配置与目标后实际发送
.venv/bin/python -m reportagent.cli.daily_feishu_push --receive-ids oc_your_chat_id --receive-type chat_id
```

`--max-reviews` 控制本期评估数量，默认读取 `push_settings.max_reviews`（当前 12），允许 1–12；`--force-review` 刷新全文评估缓存。`--dry-run` 不投递、不写发送回执，但会保存评分、评估和运行记录；产物保存在 `data/daily_reports/`，JSON 包含最终状态、阶段错误和投递结果。重复运行由进程锁防护，发送失败可按收件人补发。

研究方向读取 `configs/push_directions.yaml` 中启用的定义；修改方向、筛选提示词或阈值后，旧相关性评分须重新计算。`lookback_days` 控制采集回溯，`llm_filter_threshold` 控制初筛门槛，`llm_batch_size` 控制评分批量；投递窗口仍为最近四个 UTC 日期。部分历史展示参数尚未接通，评估数量使用上述 CLI 参数。

### 周报

按发表日期查询上一个完整 **UTC 周一至周日**的全部库中论文，不自动重复采集，也不限于精选。默认通过配置的模型 API 更新过期相关性与最多 12 篇全文评估，复用保存的证据分层排版；只凭摘要相关性不能进入重点推荐。缺正文、材料不足或引文待核验的条目单独列出。`--max-reviews` 可设 1–12，`--force-review` 刷新评估，`--no-llm` 仅读取缓存，过期或缺失的评估保留待核验。

在机器人私聊发送 `/weekly` 即可生成并发送到当前会话，无需配置推送目标；空周会明确告知暂无资料。与 `/daily`、`/report` 共用任务锁，同一时间处理一个任务。命令在后台运行，不占用普通聊天的 300 秒调用窗口。此命令不安装周报定时任务。

```bash
# 不调用模型的模板预览；--weeks-ago 0 为上周，1 为上上周
.venv/bin/python -m reportagent.cli.weekly_report --dry-run --no-llm --output data/weekly-preview.md
.venv/bin/python -m reportagent.cli.weekly_report --weeks-ago 1 --dry-run
# 确认目标后发送
.venv/bin/python -m reportagent.cli.weekly_report --receive-ids oc_your_chat_id --receive-type chat_id
```

日报有按目标的发送回执与补发机制；**周报目前没有同样的去重回执，重复执行可能重复发送**。所有 `--dry-run` 都应按具体流程理解，不代表无网络、无模型费用或完全不写库。

## 部署与定时任务

### 本机或常驻服务器

需要聊天、飞书日/周报和网页时，在持续开机的主机上按前面的完整安装步骤部署即可，不要求另租机器。运行账户需要能够访问飞书、采集源和模型接口，并在其 `PATH` 中找到 `claude`。

```bash
bash scripts/ops/keepalive.sh    # 检查整套服务的 PID；需要时后台启动 start.sh
```

该脚本守护的是 Web + 机器人，日志在 `data/bot.log`；不是模型或飞书通道的健康检查。后台常驻不等于开机自启，后者需本机的 cron、launchd 或 systemd 配置。

### 哪些任务会自动运行

| 任务 | 如何启动 | 调度来源 |
|---|---|---|
| 飞书聊天与快捷命令 | 启动 `reportagent.bot.feishu` | WebSocket 常驻，无需定时 |
| 飞书日报 | 私聊 `/daily`、`cli.daily_feishu_push` 或 Web 内置调度 | 内置 Feishu 模式默认开启；也可改用外部调度 |
| 飞书周报 | 私聊 `/weekly` 或调用 `cli.weekly_report` | 手动按需运行；自动运行需外部调度 |
| 服务进程守护 | 调用 `scripts/ops/keepalive.sh` | 需自己配置外部调度 |
| arXiv 独立采集 | 选择独立采集模式并启用调度后注册 | `daily_report.arxiv_collection`，Feishu 模式不单独注册 |

`configs/app.yaml` 默认是 `daily_report.enabled: true`、`mode: feishu`、`timezone: Asia/Shanghai`。Web 按工作日 `schedule_cron: "08:00"` 调用与手动入口相同的 Feishu 日报流程，包含采集、筛选、全文评估和发送；该字段填写 `HH:MM`。需配置推送目标并只保留一个调度 Web 进程。开关开启不代表 Web 已在运行；修改配置后需重启 Web。

如需暂停自动任务，可设 `enabled: false`；仍可手动使用 `/daily` 和 CLI，也可自行配置外部调度。独立采集模式只采集入库。

### 外部 cron 示例

下面仅是手动安装时的示例，**不会随仓库自动生效**。时间假设调度主机使用 `Asia/Shanghai`：每天 09:00 日报、周一 08:00 周报、每五分钟检查服务。先把工作目录与 `PATH` 替换成实际值；`PATH` 必须包含本机 `claude`，不要把占位路径直接装入 crontab。

```cron
PATH=/home/your-user/.local/bin:/usr/local/bin:/usr/bin:/bin
*/5 * * * * cd /absolute/path/to/AlphaReport-Claude && /bin/bash scripts/ops/keepalive.sh
0 9 * * * cd /absolute/path/to/AlphaReport-Claude && .venv/bin/python -m reportagent.cli.daily_feishu_push --max-per-source 15 >> data/cron-daily.log 2>&1
0 8 * * 1 cd /absolute/path/to/AlphaReport-Claude && .venv/bin/python -m reportagent.cli.weekly_report >> data/cron-weekly.log 2>&1
```

安装前先手工生成预览并确认收件人。若用外部 cron 取代内置日报调度，先设置 `daily_report.enabled: false` 并重启 Web，避免两个调度器重复执行；只安装周报任务不要求关闭内置日报。机器上已有旧路径 `scripts/daily_feishu_push.py`、`scripts/weekly_report.py` 或 `scripts/bot_keepalive.sh` 的任务，需要按上例迁移；旧兼容入口已移除。本次目录整理没有修改本机系统调度。

### 监听地址、隧道与 Docker

默认网页仅监听 `127.0.0.1:8000`。启动参数由 shell 读取，需要导出或在命令前赋值，单写 `.env` 不生效：

```bash
ALPHAREPORT_BIND_HOST=127.0.0.1 ALPHAREPORT_PORT=8001 bash start.sh
ALPHAREPORT_PUBLIC_TUNNEL=true bash start.sh     # 需安装 cloudflared
bash scripts/ops/sync_public_url.sh              # 同步最新临时隧道地址到 .env
```

公网隧道默认关闭。对外提供网页时应在部署层配置访问控制；网页读取接口本身不是经过完整登录认证的服务。

`deploy/docker-compose.yml` 保留了 Nginx + Python Web 的部署示例。使用前需调整硬编码的 `/data1/alphareport/` 挂载路径、凭据位置和映射端口，再手动运行 `docker compose -f deploy/docker-compose.yml up -d --build`。当前模板实际映射宿主 80 端口，`deploy.sh` 中导出的 `PORT` 等变量并未接入 compose，旧生产配置也需要逐项核对。

**现有 compose 不启动飞书机器人或飞书日/周报任务，镜像也没有安装 Claude Code。** 因此它不是完整飞书部署方案；本页以原生主机部署说明完整功能。这里未验证实际服务器部署。

## 扩展功能与可用范围

以下功能保留，但需要额外准备；默认网页没有完整操作入口。它们不影响普通聊天、单篇分析和飞书日/周报的使用。

| 功能 | 解决什么问题 | 何时值得使用 |
|---|---|---|
| 语义检索 | 用一句研究问题寻找意思相近的论文与原文片段，例如“有哪些方法预测尾部风险” | 论文库变大，关键词搜索不够用时 |
| 研究创意 | 围绕一个主题整理已有研究，并提出可进一步验证的研究思路 | 阅读文献后需要讨论下一步研究时；不等于已证明有新颖性 |
| 自定义技能 | 保存经常重复的聊天任务要求，例如固定的文献比较格式 | 同一种任务反复使用时；它是任务指令，不会新增代码功能 |

### 按意思查找论文

输入研究问题，返回相关论文及匹配的正文片段。当前可通过 Python 或实验聊天 API 使用；网页搜索框仍是关键词搜索。

使用前需按[索引维护说明](docs/architecture.md#向量索引准备与重建)准备研报索引。普通采集和单篇分析不会自动建立该索引。准备完成后可查询：

```bash
.venv/bin/python - <<'PY'
from reportagent.vector_store.store import get_vector_store
for match in get_vector_store().search("波动率预测", limit=5):
    print(match)
PY
```

### 实验聊天与管理入口

研究创意、自定义技能、语义检索，以及按指令采集、读取、分析和删除研报，可通过保留的实验聊天 API 调用。另有库外网页搜索，需要配置 `TAVILY_API_KEY`。这些操作没有对应的默认网页按钮，也不是新增的飞书快捷命令。

工具名与实现归属见[实验工具映射](docs/architecture.md#实验工具映射)。

在安装完整依赖后，可启动实验 API，使用它的 `/docs` 查看接口。它同样遵循
`daily_report.enabled` 开关；与默认 Web 并行试用时，先关闭调度，避免两个 Web 进程注册相同任务。只保留一个
调度 Web 进程，避免重复采集或投递。

```bash
.venv/bin/python -m uvicorn reportagent.api.legacy_app:app --host 127.0.0.1 --port 8002
```

它有流式聊天、采集任务状态、流式分析与论文问答等接口，但当前前端未接通这些功能，部分旧功能仍有兼容性限制，不能把它当作已经完成认证的公网服务。飞书普通聊天走另一条 Claude Code 路径。

### 围绕主题寻找研究思路

输入研究主题和角度，程序检索最多 8 篇相关库内文献，保留完整摘要；有当前有效的全文评估时加入原文方法、发现与局限。
输出候选假设、最接近的已有工作、候选差异、数据需求、最小验证与否定条件。检索失败会明确提示；未命中不代表领域内无人研究。

```bash
.venv/bin/python -m reportagent.cli.ideation --topic '波动率预测' --angle methodology
```

`angle` 还可用 `general`、`factor`、`cross_pollination`、`critique`、`data`。
显式需要外部查新时添加 `--verify-external`，需配置 `TAVILY_API_KEY`；结果仍只是网页线索，不能确认新颖性。

### 论文速览与复现准备

网页详情右侧的“论文速览”就是知识卡片：展示已保存的研究问题、方法、发现、阅读建议和可展开的原文引证。
它不另调模型补写数字或打质量分；原文、模型或评估规则变化后，过期卡片显示待核验。
“刷新展示”只更新展示，不重做评估。`cli.narrative_brief` 也使用相同的证据卡片服务。

“复现准备”页整理研究/预测目标、推断时可用信息、训练目标、数据、划分、基准、指标和假设。
点击生成时调用模型核对全文；原文事实附引文，缺失设置列为待核查，最小验证与否定条件标为建议。
理论研究核对命题、假设与反例，不强求训练或回测。准备卡不会下载数据、训练模型或证明复现成功。

```bash
.venv/bin/python -m reportagent.cli.reproduction --report-id 1
# 预览不写数据库，但仍可能调用模型
.venv/bin/python -m reportagent.cli.reproduction --report-id 1 --dry-run
```

前提是已有可用全文和当前有效的证据评估；单篇 arXiv 可用 `cli.report` 准备。
成功准备卡按原文、评估、模型与提示词缓存；`--force` 可重新提取，失败不会覆盖旧成功结果。

### 保存可复用的聊天任务要求

例如每次比较论文都要求按“研究问题、数据、方法、证据、局限”组织，可以保存成自己的任务模板，后续聊天复用。当前可通过实验聊天中的技能管理工具，或在本机保存：

```bash
.venv/bin/python - <<'PY'
from reportagent.skills.registry import save_user_skill
print(save_user_skill(
    name="论文比较",
    description="按统一格式比较已有论文",
    prompt_template="比较用户指定的论文，依次说明研究问题、数据、方法、证据、局限，并附原文依据。",
))
PY
```

保存的指令供聊天 Agent 参考执行；不会自动改变 `/report`、日报或评分 CLI 的固定分析流程。要调整这些流程的阅读标准，应修改根目录 `skills/` 中对应的任务资源。

## 配置与目录

| 位置 | 职责 |
|---|---|
| `configs/app.yaml` | 模型、数据源、数据库、调度器、技能目录等运行设置 |
| `configs/push_directions.yaml` | 研究方向与推送参数；部分字段尚未接通 |
| `configs/classification_taxonomy.yaml` | 市场、资产、频率、主题分类 |
| `skills/` | 分析、扫描、评分、日/周报、研究创意六类任务的说明、提示词和输出模板 |
| `reportagent/skills/` | 唯一共享技能加载器；用户技能在 `data/user_skills/` |
| `reportagent/services/` | 业务实现；`bot/`、`api/`、`cli/` 适配各入口 |
| `frontend/` | React 网页；构建结果在 `frontend/dist/` |
| `scripts/ops/` | 运维工具；业务命令统一用模块 CLI |
| `data/`、`.claude-bot/` | 研报、向量库、会话等本地资料，忽略提交 |
| `tests/`、`deploy/`、`docs/` | 回归测试、部署示例、开发架构说明 |

```bash
.venv/bin/python -m reportagent.cli.skills list
.venv/bin/python -m reportagent.cli.skills show analyze-report
```

内置任务只有 `skills/` 一份来源，Claude Code 与两种 LangGraph Agent 共享读取。分析共用 `analyze-report/grounding.md` 的证据规则；`analyze-report/quality-review.md` 定义全文评估，`paper-selection/` 定义摘要相关性初筛。摘要汇总的提示词和各深度长度预算位于 `analyze-report/summary.yaml`。`daily-report/briefing.md`、`weekly-report/briefing.md` 和 `analyze-report/single-brief.md` 是确定性排版模板，不是模型提示词。修改研究判断应修改对应评估资源；修改排版不应改动已保存的证据。

分析与质量评估默认覆盖完整提取文本（最多 240,000 字符），以带偏移的文本段组织；超过上限会明确停止或待核验，不截前半篇冒充全文。引文核验只容许可追溯的空白、英文连字与安全断行还原，保存原始文本位置；删句、改数学符号仍被拒绝。材料不足独立于不相关，补全文/摘要后重新评估。模型、任务资源、分析深度或来源内容变化会令缓存失效；失败刷新保留原成功结果。

`CLAUDE.md` 是编程 Agent 的项目指南；技能说明不能替代代码中的权限、完整性校验和回执。配置归属见 [configs/README.md](configs/README.md)，脚本用途与旧命令迁移见 [scripts/README.md](scripts/README.md)。

## 维护与排查

- 飞书单篇任务：`data/report-jobs.log`；守护启动的输出：`data/bot.log`；隧道：`data/cloudflared.log`；外部定时任务使用上面指定的 cron 日志。
- 飞书无响应：先检查进程、应用订阅与权限、允许名单、运行账户的 `claude` 路径和模型凭据。`start.sh --check` 不验证外部接口联通。
- 有论文但没有日报：检查来源状态、四天日期窗口、当前相关性评分指纹与门槛、全文评估状态，以及目标回执。`data/daily_reports/` 保存本次产物，数据库保存运行结果；周报没有同样的去重回执。
- 旧 `pushed_at` 没有收件人信息，无法转换成新回执；首次使用新逻辑时，窗口内旧论文可能重新发送一次。
- 迁移机器时另行保存所需的 `.env`、`data/`、`.claude-bot/`，并核对模型配置与系统调度；重装依赖不会恢复历史数据。它们都不提交 Git。
- PDF 文本可能丢失公式或图表，长论文按完整提取文本组织输入，超过限额时停止或保持待核验；模型分析没有复现论文实验，重要结论需对照原文。

<details>
<summary>可选的飞书 HTTP 备用通道</summary>

默认 WebSocket 已足够。需要备用 HTTP 通道时，在 `.env` 配置：

```dotenv
FEISHU_HTTP_CALLBACK_ENABLED=true
FEISHU_VERIFICATION_TOKEN=your_real_verification_token
FEISHU_INBOX_TOKEN=your_separate_random_secret
```

`POST /api/v1/feishu/callback` 校验 Verification Token 和发送者；URL challenge 也需正确令牌。只支持未加密事件 JSON，没有实现加密事件解密。机器人通过带独立 Bearer 凭据的 `POST /api/v1/feishu/inbox` 消费按 message_id 去重的 SQLite 队列。

</details>

### 测试与内容验收

```bash
.venv/bin/python -m pip install -r requirements/test.txt
.venv/bin/python -m pytest
bash -n start.sh scripts/ops/run_bot.sh scripts/ops/keepalive.sh scripts/ops/sync_public_url.sh
(cd frontend && npm run build)
```

自动测试默认只收集 `tests/unit/` 和 `tests/integration/`，使用固定输入、临时数据库和替代外部接口。离线护栏禁止真实网络、模型和消息发送。前端构建和 lint 是不同检查。

真实多来源采集、DeepSeek 分类/批量分析、全库相关性筛选和周报内容验收单独放在 `tests/acceptance/`，显式运行并保存产物。自动检查可判断日期、去重、状态和证据完整性；分析是否准确、有洞察，需要对照原文评阅。目录分工、执行方式和评阅标准见 [tests/README.md](tests/README.md)。
