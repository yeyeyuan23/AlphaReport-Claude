# 运行配置与任务资源

本目录回答“程序以什么设置运行”：

- `app.yaml`：模型、数据源、数据库、服务端口、技能库位置。
- `classification_taxonomy.yaml`：分类维度与标签。
- `push_directions.yaml`：用户关注的研究方向与推送设置。
- `prompts/chat_system.txt`：整个交互 Agent 的通用角色与工具使用约定。

任务专属的说明、提示词、输出模板归入根目录 `skills/<任务>/`，和任务一起维护。
例如 `skills/analyze-report/analysis.yaml` 是分析任务定义，虽然使用 YAML，
它仍属于分析技能；`app.yaml` 中的端口即使写成文本，也仍属于运行配置。
按职责划分，不按文件扩展名划分。

头脑风暴任务的角色提示词在 `skills/research-ideation/system.md`。
`chat.brainstorm_prompt_path` 可显式覆盖其路径，旧的自定义路径仍可使用。
密钥保存在忽略提交的 `.env` 中。

`llm.thinking` 控制 Python Anthropic 兼容请求的思考模式；当前 DeepSeek 结构化流程设为
`disabled`，避免分类预算被思考内容耗尽。单次调用可显式覆盖；OpenAI 路径不传此参数。
`daily_report.enabled` 默认开启，调度器由 Web 后端进程承载；单独启动飞书机器人不会启动调度器。
