# Report quality implementation plan

> For agentic workers: implement the approved conversational design task by task, with failing regression tests before behavior changes.

**Goal:** Make source failures visible, screen for relevance, review up to 12 full-text candidates before recommending at most 5, and present grounded summaries consistently in Feishu and the library.

**Architecture:** Keep the newly organized skills/services/CLI structure. Prompts define research criteria; services enforce evidence, ordering, caching and delivery. SQLite stores assessments and run outcomes. No scheduled task is enabled and validation does not send messages.

**Spec:** The user-approved conversation plan, “先整理主流程，再优化研究报告”, and the subsequent code-read findings.

**Constraints:** Six configured directions; academic papers prioritized, broker/community sources separated; no Docker requirement; preserve credentials, data, receipts and existing entrypoints. Work in the user's requested checkout, do not commit or modify other windows' changes. Partial-source success must remain usable. Prompt changes invalidate analysis caches. Local reading is the acceptance environment.

## Tasks and interfaces

- [x] Source observations: `collect_reports()` returns `source_statuses` entries with source, status (`ok`, `empty`, `partial`, `error`, `unavailable`), fetched/valid/full_text counts, elapsed_ms, errors; overall `status` is `ok`, `partial`, `failed`. Actual search/parse outcomes determine status, never a constant availability flag. Preserve partial results and collection errors through storage. Own sources, collection service, source tests.
- [x] Relevance screening: `paper_scoring` loads enabled directions from the existing YAML, uses the complete abstract, and validates model indices/scores. Store `selection_direction` and `selection_relevance`; `selection_score` remains compatibility relevance. Own scoring service, paper-selection skills, scoring tests; root owns database migration.
- [x] Grounded review: add `report_quality` service, strict structured evidence review, stable content/prompt/model fingerprint and `evaluation_json` persistence. Invalid/missing evidence or partial text cannot yield a top recommendation. Existing single-report analysis caches include prompt/model fingerprints. Own quality service, analysis skills, cache code and tests.
- [x] Daily integration: choose at most 12 candidates fairly across enabled directions, review before render/send, recommend at most 5, deterministic rendering from saved evaluations; separate other sources and pending reviews. Add run records, lock duplicate runs, preserve per-recipient receipts, and explicit force-refresh CLI. No automatic schedule activation.
- [x] Library and verification: expose evaluation fields and render them in existing list/detail views. Add manual `/daily` using existing authorized job boundary. Run offline tests, frontend build/lint, source read-only probes and one bounded real review preview without sending. Document results and limitations.

## Review focus

Source errors swallowed into empty lists; invalid evidence promoted to top picks; stale prompt cache; dry-run sending or recording deliveries; stale reports receiving a newer revision's analysis.

## Verification ledger

Initial state: clean working tree on main; current architecture already organized. User authorized implementation in this checkout. No model calls or external sends before implementation checks.


Final verification (2026-10-16):

- `python -m pytest -q`: 175 passed, 7 subtests passed; 7 third-party deprecation warnings. Final log: `/tmp/alphareport-quality-tests-final.log`.
- Frontend production build passed. New evaluation component/types lint passed. Full lint retains the existing 11 errors and 1 warning in legacy components/pages; this was not presented as passing.
- `git diff --check` passed. Independent review's six findings were fixed and rechecked, including revision changes during later papers' reviews.
- Live read-only probes each returned one parsable item from arXiv, EastMoney and BigQuant; no source errors. Evidence: `data/validation/source-probe.json`.
- Two bounded DeepSeek quality reviews used existing library paper 1 (36,835 characters), without Feishu delivery. Latest run `b986e378-0506-4300-9f75-05cf89bfc6f0` records `preview`, completion time and zero deliveries. Actual model output contained non-contiguous/unmatched quotations, correctly retained as pending instead of a top recommendation. This tests the evidence gate, not independent verification of the paper's scientific conclusions.
- `data/quality-preview.md` was re-rendered from that saved real assessment with the final template; no additional model call. Unverified findings are omitted from the digest.
- Local SQLite migration was preceded by `data/backups/report_library-before-quality-20261016.sqlite`; no prior delivery receipts were deleted. Scheduled jobs remain disabled. No bot or Web process was running at final inspection; no service was started or external message sent.
- No commit or deployment. Remaining operational acceptance: start the normal services and review the private Feishu output when the user chooses to send; automatic scheduling remains a separate decision.

## 后续验收与用户修订（2026-10-16）

- 个性化邮件下线；保留飞书和独立采集流程，不删除本地历史资料。
- 测试分为 unit、integration、显式 acceptance；真实多来源与模型输出留档后对照原文评阅。
- 用户要求飞书日报调度默认开启，配置更新为 enabled=true；Web 进程持续运行才生效。
- README 合并库外搜索与导入，库内查询/管理集中列出，内部 PDF 处理不列作用户功能。
