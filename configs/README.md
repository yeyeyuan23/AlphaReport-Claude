# 运行配置与任务资源

本目录回答“程序以什么设置运行”：

- `app.yaml`：模型、数据源、数据库、调度与技能库位置。
- `classification_taxonomy.yaml`：分类维度与标签。
- `push_directions.yaml`：用户关注的研究方向与推送设置。

任务专属的说明、提示词、输出模板归入根目录 `skills/<任务>/`，和任务一起维护。
例如 `skills/analyze-report/analysis.yaml` 是分析任务定义，虽然使用 YAML，
它仍属于分析技能；`app.yaml` 中的调度时间即使写成文本，也仍属于运行配置。
按职责划分，不按文件扩展名划分。

头脑风暴任务的角色提示词在 `skills/research-ideation/system.md`。
密钥保存在忽略提交的 `.env` 中。

`llm.thinking` 控制 Python Anthropic 兼容请求的思考模式；当前 DeepSeek 结构化流程设为
`disabled`，避免分类预算被思考内容耗尽。单次调用可显式覆盖；OpenAI 路径不传此参数。
`daily_collection.enabled` 和 `daily_report.enabled` 默认分别开启，调度器由 Web 后端承载；单独启动飞书机器人不会启动调度器。默认每天北京时间 08:00 多来源采集入库，09:00 从论文库生成日报，周末也执行。两项开关独立，`schedule_cron` 为 `HH:MM`；`daily_collection.lookback_days: 3` 从 UTC 当天往前 3 天零点起采集，覆盖日报最近四个 UTC 日期窗口。日/周报不调用采集，不记录逐篇已推送标记。

Web 的监听地址与端口由 `ALPHAREPORT_BIND_HOST`、`ALPHAREPORT_PORT` 环境变量控制。
`push_directions.yaml` 中实际生效的 `push_settings` 包括：相关性阈值 `llm_filter_threshold`、
日报评分批量 `llm_batch_size`、日报新全文评估预算 `max_reviews` 和日报重点推荐数 `max_featured`。
周报评估预算使用其 CLI 的 `--max-reviews`（默认 12）。
