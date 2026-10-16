# 检查与内容验收

| 目录 | 验证对象 | 外部依赖 |
| --- | --- | --- |
| `unit/` | 参数解析、模型响应校验、证据定位、推荐规则、简报渲染、调度配置 | 固定输入与替身 |
| `integration/` | CLI、采集适配器、数据库、单篇/每日流程、发送结果、独立采集调度、HTTP 与启动器之间的协作 | 临时文件、内存 SQLite、模拟 HTTP/模型/飞书 |
| `acceptance/` | 真实模型与多来源材料形成的研报和简报是否有阅读价值 | 显式运行，可能需要网络、模型配置与费用 |
| `support/` | 共用模型/论文/分析替身和仓库根路径 | 不作为测试收集 |

在仓库根目录运行：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/unit -q
.venv/bin/python -m pytest tests/integration -q
.venv/bin/python -m pytest --collect-only -q
```

默认只收集 `unit/` 与 `integration/`。离线护栏阻止真实网络连接、模型 SDK 构建和 Claude 模型命令；测试应明确注入替身，不能借用真实 API、下载材料或发送消息。HTTP 用例使用进程内客户端或模拟 transport，启动器用例使用临时替身程序。

自动检查能证明固定输入下的控制流程与数据约束：失效缓存不会冒充新评审，未定位的引文不会成为有效发现，未完成的分析不会成为成功投递，失败与部分成功状态可追踪。这些断言不等于真实模型写出了有洞察的分析，也不证明来源当天可用。

`acceptance/` 用于单独的真实材料验收，不纳入默认 pytest。人工需要对照原文检查研究问题、方法与结论是否准确，证据是否支持主张，局限是否具体，阅读建议是否有用，并比较学术论文、券商研报与社区文章的差异。查看完整分析和最终简报，不能只看状态、分数或引文匹配。真实发送需另行显式授权。

## 显式执行真实验收

从仓库根目录运行，下列命令使用当前 `.env` 与实际研报库，会联网、调用模型并更新资料。每次验收换一个目录；目录首次使用时保存 `before.sqlite` 备份。结果、分类、评分、完整分析、提取文本与周报预览都留在该目录，不提交 Git。脚本始终只预览，不发送飞书消息。

```bash
# 1. 先采集上一完整 UTC 周：将日期换成实际的周一至周日
.venv/bin/python -m tests.acceptance.workflows collect --run-dir data/acceptance/my-run --label weekly-collection --date-from 2026-10-05 --date-to 2026-10-11 --max-per-source 4
# 2. 扩充近期资料，再用真实模型重新分类；检查 classify-library.json 中的标签
.venv/bin/python -m tests.acceptance.workflows collect --run-dir data/acceptance/my-run --label recent-collection --days 7 --max-per-source 6
.venv/bin/python -m tests.acceptance.workflows classify --run-dir data/acceptance/my-run
# 按实际分类选择一类；仅复用深度、模型、资源和原文一致的分析，先确认预计调用量
.venv/bin/python -m tests.acceptance.workflows batch --run-dir data/acceptance/my-run --topic portfolio_optimization --depth standard
# 3. 对整个库重新做摘要相关性评分；--review 再对所有过线候选做全文评估
.venv/bin/python -m tests.acceptance.workflows score --run-dir data/acceptance/my-run --review
# 周报只查询库中的上一完整 UTC 周，不会自动补采；默认先验证模板
.venv/bin/python -m tests.acceptance.workflows weekly --run-dir data/acceptance/my-run --label weekly-template
.venv/bin/python -m tests.acceptance.workflows weekly --run-dir data/acceptance/my-run --label weekly-model --llm-weekly
```

`score` 不受近期日期或默认 200 条限制，也会重评已有分数。它验证的是摘要相关性；`--review` 才执行有全文依据的质量评估。数据量大时先查看库规模，避免一次触发过多调用。`batch --topic` 使用完整分类标签匹配；`evidence_scope` 区分全文与摘要分析，不能把摘要分析称为全文精读。

周报使用配置的 Python 模型 API 刷新相关性与全文评估，再以保存证据排版；`--no-llm` 不调用模型，缺失或过期评估显示待核验。结果记录刷新审计、来源、文章 ID 与预览状态。BigQuant 和东方财富取得可访问的正文或 PDF，附件失效时记录错误；仅有更新时间的条目仍可能缺发表日期。来源连通不能代表已穷尽所有近期论文，摘要相关性也不代表研究质量。

## 如何评阅内容

每次验收另写一份 `review.md`，记录版本、选样理由、原文位置、生成内容、判断和待修问题。逐项检查：

- 采集：逐源区分网络成功、候选命中、实际入库和全文可用；检查日期缺失、版本与重复，不能把总任务成功当作来源覆盖合格。
- 分类与批量分析：检查同类样本和边界样本；研究问题、方法、结果是否对应原文，是否把未报告细节补成事实，理论研究是否被错误要求实证回测。
- 评分与精选：把高分和低分论文对照关注方向，检查相关性理由；全文质量与方向相关性分开判断，记录漏选、误选及证据不足。
- 周报：核对每篇是否在规定周内，重点与其他材料是否分层，概要是否有依据，标题/链接是否对应同一篇，是否暴露缺失来源和资料限制。

没有人工校准样本时，自动化只能提供检查信号；不设置“模型自评分高于某值即质量通过”。自动护栏可以进入 pytest，真实文章与主观质量判断留在显式验收。
