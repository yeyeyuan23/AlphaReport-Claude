# 运行配置与任务资源

本目录回答“程序以什么设置运行”：

- `app.yaml`：模型、数据源、数据库、调度与技能库位置。
- `classification_taxonomy.yaml`：分类维度与标签。
- `push_directions.yaml`：用户关注的研究方向与推送设置。

任务专属的说明、提示词、输出模板归入根目录 `skills/<任务>/`，和任务一起维护。
例如 `skills/analyze-report/analysis.yaml` 是分析任务定义，虽然使用 YAML，
它仍属于分析技能；`app.yaml` 中的调度时间即使写成文本，也仍属于运行配置。
按职责划分，不按文件扩展名划分。

密钥保存在忽略提交的 `.env` 中。

`llm.thinking` 控制 Python Anthropic 兼容请求的思考模式；当前 DeepSeek 结构化流程设为
`disabled`，避免分类预算被思考内容耗尽。单次调用可显式覆盖；OpenAI 路径不传此参数。
`daily_collection.enabled` 和 `daily_report.enabled` 默认分别开启，调度器由 Web 后端承载；单独启动飞书机器人不会启动调度器。默认每天北京时间 08:00 多来源采集入库，09:00 从论文库生成日报，周末也执行。两项开关独立，`schedule_cron` 为 `HH:MM`。自动采集与日报共用昨天至今天的来源日期范围，包含起止日期；相邻日报可能包含同一日期的资料。周报查询含今天在内最近七个日期的库中资料。`max_results_per_source=None` 表示完整枚举；不再配置候选数量或独立3天回查窗口。统一按 `published_date` 筛选，保留来源原始日期，不补具体时刻或按时区挪动日期；任务执行时刻与日期筛选独立。arXiv 根据官方带日期的列表查询各批次，东方财富使用 `publishDate` 的日期，BigQuant 使用 `publishedAt` 的日期。缺少可靠来源日期的条目记录证据不足。日/周报不调用采集，不记录逐篇已推送标记。

`weekly_report` 与上述两项调度独立，默认 `enabled: true`、`day_of_week: "fri"`、
`schedule_cron: "09:00"`、`timezone: "Asia/Shanghai"`，即每周五北京时间 09:00 从库中生成并推送周报。
星期使用 `mon/tue/wed/thu/fri/sat/sun`（周一至周日），时间格式为 `HH:MM`；修改后重启 Web 后端。
三项任务可分别关闭，手动 `/weekly` 不受定时开关影响。周报目标由 `.env` 的
`FEISHU_WEEKLY_PUSH_IDS`、`FEISHU_WEEKLY_PUSH_TYPE` 配置，未设目标时沿用日报目标和类型。
周五 09:00 日报和周报分别运行；已有外部周报调度应移除或关闭对应内置任务，避免重复发送。
健康接口 `/api/v1/health` 返回周报的 `weekly_enabled` 与 `weekly_next_run`。

`sources.arxiv.search_scope` 同时控制自动公告候选和手动 broad 检索范围：
`full_categories: ["q-fin"]` 保留全部九个量化金融子分类及跨分类公告；
`topic_groups` 分别为 `cs.LG/stat.ML` 的预测、分布漂移、在线学习、排序与表格学习主题，
以及 `math.OC` 的组合构建、交易成本和最优执行主题。每组配置 `name`、`categories`、`keywords`，
同组关键词按“或”匹配标题和摘要，不要求金融关键词，匹配会统一大小写、标点/连字符与空白。
修改分类或词组后重启服务。自动采集仍完整枚举公告窗口且无数量上限，跨分类按 arXiv 编号去重；
完整 q-fin 范围不受扩展主题词限制。旧的未生效 `default_categories` 已由此配置取代。
候选预检命令为 `.venv/bin/python -m reportagent.cli.arxiv_candidates`；它只读公开元数据，
不进行模型评分、下载或写库；统计及总耗时估计应区分元数据、分类和全文证据评估阶段。

Web 的监听地址与端口由 `ALPHAREPORT_BIND_HOST`、`ALPHAREPORT_PORT` 环境变量控制。
部署检查与运行时共用 `feishu.app_id_env`、`feishu.app_secret_env` 和 `llm.api_key_env` 的凭据配置；更换模型服务时不额外要求 DeepSeek 专属密钥。
`push_directions.yaml` 中实际生效的 `push_settings` 包括：相关性阈值 `llm_filter_threshold`、
摘要筛选批量 `llm_batch_size`、并发 `screening_concurrency` 和日报重点推荐数 `max_featured`。
全文评估预算配置在 `app.yaml` 的 `daily_collection.max_reviews`：省略或 `null` 表示不限，正整数只限制采集阶段的新模型调用，有效缓存不占预算，缺材料不堵住后续候选。
失败或超出预算的条目由后续采集处理。0、负数、布尔值和字符串均不接受；日/周报不再提供模型评估参数。
