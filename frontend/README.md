# AlphaReport 网页

React + TypeScript 研报阅读界面，提供研报列表、筛选、PDF、研究简报、论文速览和复现准备。
安装、模型配置和部署见 [项目首页](../README.md)。

开发环境使用 Node.js 22.13+。在项目根目录启动后端：

```bash
.venv/bin/python -m reportagent.main
```

在本目录执行：

```bash
npm ci
npm run dev
```

开发页面位于 `http://127.0.0.1:3000`，`/api` 请求转发到本机 8000 端口。
`npm run build` 生成 `dist/`，由 Python Web 服务提供。
前端格式、lint、测试与构建命令见 [CI 检查](../README.md#自动检查ci)。

采集和问答通过飞书或 CLI 使用。网页的简报与知识卡片读取已保存结果；
“复现准备”点击生成时可调用模型。任务资源位于项目根目录 `skills/`。
