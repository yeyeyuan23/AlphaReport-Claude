# AlphaReport 网页

React + TypeScript 研报阅读界面。当前提供研报列表、筛选、PDF 阅读、研究简报和知识卡片。

在项目根目录启动后端：`.venv/bin/python -m reportagent.main`。

在本目录运行 `npm ci`，再运行 `npm run dev`。开发服务器把 `/api` 请求转发到本机 8000 端口。
`npm run build` 生成 dist/，由默认 Web 入口直接提供。

采集与问答通过飞书或 CLI 操作；已移除停用的网页聊天和任务入口。共享技能定义在项目根目录 skills/，网页读取共享服务保存的分析结果。
