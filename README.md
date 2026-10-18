# AlphaReport

在飞书中查找、阅读和讨论量化金融研报，接收日报与周报；网页提供研报检索、PDF 阅读和分析详情。服务运行在自己的电脑或服务器，模型通过 API 调用。

- [功能与使用](#功能与使用)
- [安装与部署](#安装与部署)
- [维护与排查](#维护与排查)

## 功能与使用

### 飞书机器人

普通文本支持连续问答，也可以按标题或上下文查找、比较论文，进行分类、批量分析、相关性评分或整理复现准备。例如：

```text
找出库中和波动率预测有关的论文，比较它们的方法与局限。
分析刚才讨论的那篇论文，未报告的信息不要补写。
为这篇论文整理复现前需要核对的数据、划分和指标。
```

这些请求由 Claude Code 使用共享后台工具处理，用户无需提供数据库 ID。普通聊天单次限时 300 秒，实际可执行操作取决于部署账户的工具权限。

| 命令 | 作用 |
|---|---|
| `/help`、`/start` | 查看帮助 |
| `/stats` | 查看研报库统计 |
| `/search 因子模型` | 搜索库中研报 |
| `/recent` | 查看最近入库的研报 |
| `/chatid` | 查看当前会话 ID，供配置自动推送 |
| `/clear` | 开始新会话，不删除研报 |
| `/report 2005.13665` | 阅读一篇 arXiv 论文并发送分析，也接受 arXiv 链接 |
| `/daily` | 发送昨天的日报 |
| `/weekly` | 发送截至昨天的七天周报 |

单篇阅读、日报和周报命令仅限私聊。`/daily`、`/weekly` 使用库中资料和已保存评估，不触发采集；重复执行会再次发送本期报告。

### 研报库

网页支持关键词与分类筛选、统计、PDF 阅读、研究简报和论文速览。详情中可查看研究问题、方法、发现、局限与原文引证，也可生成“复现准备”来整理原文事实、待核查设置和建议验证方案。

后台支持 arXiv、东方财富、BigQuant 和本地 PDF。自动采集先按摘要判断相关性，达标后下载入库并评估全文；相关性与研究质量分别判断。报告分层呈现论文，周报按方向排列逐篇短结论和标题链接。模型阅读不代表已复现实验。

本地 PDF 放入配置的导入目录（默认 `data/pdf_library/`），下次启动服务时自动扫描并分类。只导入新增或变化的文件，不自动生成长篇分析或推送。分类、批量分析和评分的后台用法见[工具说明](docs/architecture.md#后台处理与维护工具)。

### 自动采集与推送

保持完整服务运行，默认按北京时间执行，报告通过飞书机器人发送，无需打开网页：

| 任务 | 时间 | 资料范围 |
|---|---|---|
| 采集与全文评估 | 每天 08:00，含周末 | 昨天公开发布的资料 |
| 日报 | 每天 09:00，含周末 | 昨天的库中资料 |
| 周报 | 每周五 09:00 | 截至昨天的七个自然日 |

日期使用来源提供的日期，不按入库时间筛选。报告附研报库首页与详情链接；缺少可用全文或模型明确确认上下文超限的资料会跳过，评估尚未完成的资料注明状态。

## 安装与部署

### 本地安装

需要 Python 3.12、Node.js 22.13+ 与 npm。普通聊天还需安装 [Claude Code](https://code.claude.com/docs/en/setup)，并确保运行账户能执行 `claude`。

在仓库根目录执行：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements/runtime.txt
cp .env.example .env  # 仅首次配置，已有文件不要覆盖
(cd frontend && npm ci)
claude --version
```

### 飞书与模型配置

在[飞书开放平台](https://open.feishu.cn/)创建企业自建应用：

1. 开启机器人能力，取得 App ID 和 App Secret。
2. 选择长连接，订阅 `im.message.receive_v1`。
3. 开通接收消息、发送/回复消息权限，设置可用范围并发布应用；群推送需先将机器人加入群。

编辑 `.env`，将以下占位值换成自己的配置：

```dotenv
FEISHU_APP_ID=cli_your_app_id
FEISHU_APP_SECRET=your_app_secret
FEISHU_ALLOWED_OPEN_IDS=ou_your_open_id
DEEPSEEK_API_KEY=your_model_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic
CLAUDE_BOT_MODEL=deepseek-v4-pro

FEISHU_DAILY_PUSH_IDS=oc_your_chat_id
FEISHU_DAILY_PUSH_TYPE=chat_id
ALPHAREPORT_BASE_URL=https://your-report-site.example
```

允许名单填用户的 **open_id**，空名单拒绝所有人；推送示例填 **chat_id**，可通过 `/chatid` 获取。多个账号或目标用逗号分隔。周报默认复用日报目标，也可单独配置 `FEISHU_WEEKLY_PUSH_IDS` 和 `FEISHU_WEEKLY_PUSH_TYPE`。手动日/周报直接发到当前私聊。

普通聊天使用 `CLAUDE_BOT_MODEL`；Python 分类、筛选和分析使用 `configs/app.yaml` 的 `llm` 配置。详情网址应能从收件人设备访问；自己的其他设备不能通过服务器的 `localhost` 打开页面。

### 启动与停止

```bash
bash start.sh --check  # 检查入口依赖
bash start.sh          # 启动网页后台和飞书机器人
```

默认网页为 `http://127.0.0.1:8000`，按 Ctrl+C 停止，修改代码或配置后重启。只启动机器人可用 `bash scripts/ops/run_bot.sh`，但不会运行自动采集和推送调度。

后台常驻可用 `bash scripts/ops/keepalive.sh`，日志在 `data/bot.log`；它不自动配置开机启动。监听端口、隧道和外部守护配置见[运维工具](scripts/README.md)。网页尚无完整登录认证，对外提供时需在部署层配置访问控制。

### Docker 部署

```bash
bash deploy/deploy.sh
```

首次执行会创建 `/data1/alphareport/.env` 模板并停止；填写凭据、允许名单、推送目标和服务器网址后，再执行一次。默认使用 80 端口，数据库、PDF 和会话保存在宿主机挂载目录；更新后重新运行脚本构建并重启。目录、端口和权限配置见[部署参数](scripts/README.md#docker-部署参数)。

### 配置与手动采集

| 位置 | 内容 |
|---|---|
| `.env` | 凭据、允许名单、推送目标、对外网址 |
| `configs/app.yaml` | 模型、数据源、PDF 下载策略、导入目录及调度 |
| `configs/push_directions.yaml` | 关注方向、相关性门槛和展示设置 |
| `configs/classification_taxonomy.yaml` | 分类定义 |
| `skills/` | 共享提示词、评估规则和报告模板 |
| `data/` | 数据库、PDF、分析产物与运行回执 |

修改调度的 `enabled`、`schedule_cron` 或周报 `day_of_week` 后重启。全文评估默认处理所有符合条件的论文，可通过 `daily_collection.max_reviews` 设置可选预算。

手动运行与定时任务相同的完整流程，不发送报告：

```bash
.venv/bin/python scripts/collect.py
# 假定报告日为 10 月 17 日，采集 10 月 16 日资料：
.venv/bin/python scripts/collect.py --report-date 2026-10-17
```

各来源结果、阶段耗时和回执保存在 `data/collections/`。来源范围、候选预检和维护命令见[配置说明](configs/README.md)与[后台工具](docs/architecture.md#后台处理与维护工具)。

## 维护与排查

| 问题 | 检查位置 |
|---|---|
| 机器人无响应 | `data/bot.log`、飞书发布/权限、允许名单与 `claude` 是否可用 |
| 分析或报告失败 | `data/report-jobs.log`、`data/daily_reports/` |
| 日/周报为空或来源不完整 | `data/collections/latest.json`、来源日期、相关性与全文评估状态 |
| 查看调度状态 | `/api/v1/health` 的 `schedule` 字段 |

迁移前备份 `.env`、`data/`、`.claude-bot/` 和修改过的配置。

- [架构与后台工具](docs/architecture.md)：处理流程、材料与评估规则、维护命令。
- [运维工具](scripts/README.md)：常驻、监听、隧道与部署参数。
- [开发检查与内容验收](tests/README.md)：离线测试、CI 和真实材料验收。
