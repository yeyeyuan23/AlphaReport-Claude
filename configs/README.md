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
`daily_collection.enabled` 和 `daily_report.enabled` 默认分别开启，调度器由 Web 后端承载；单独启动飞书机器人不会启动调度器。默认每天北京时间 08:00 多来源采集入库，09:00 从论文库生成日报，周末也执行。两项开关独立，`schedule_cron` 为 `HH:MM`。自动采集与日报共用报告当天北京时间 08:00 前 24 小时的公告窗口，`max_results_per_source=None` 表示完整枚举；不再配置候选数量或独立3天回查窗口。周报查询同一截止点前 168 小时的库中公告。均包含起点、不含截止点，与入库时间无关；修改任务执行时间不会自动改变 08:00 报告截止点。在线报告按独立持久化的 `announced_at` 筛选，原始 `published_date` 不改写；缺少精确公告时间的来源提示证据不足。arXiv RSS 仅有最近批次，过期或无法覆盖所需窗口时记录部分完成。日/周报不调用采集，不记录逐篇已推送标记。

Web 的监听地址与端口由 `ALPHAREPORT_BIND_HOST`、`ALPHAREPORT_PORT` 环境变量控制。
部署检查与运行时共用 `feishu.app_id_env`、`feishu.app_secret_env` 和 `llm.api_key_env` 的凭据配置；更换模型服务时不额外要求 DeepSeek 专属密钥。
`push_directions.yaml` 中实际生效的 `push_settings` 包括：相关性阈值 `llm_filter_threshold`、
日报评分批量 `llm_batch_size`、日/周报共用的新全文评估预算 `max_reviews` 和日报重点推荐数 `max_featured`。
`max_reviews` 省略或为 `null` 时，日/周报默认评估所有符合条件且需更新的论文；
正整数只限制新发起的全文评估，命中有效缓存不占预算。各 CLI 的 `--max-reviews N` 优先于配置，
不限最大值；0、负数、布尔值和字符串均不接受。
