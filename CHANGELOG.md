# CHANGELOG.md — 变更日志

> 记录有意义的变更，帮助 AI 快速了解最新动态、避免回归。格式参考 Keep a Changelog，新条目放最上面。

## [Unreleased] — M3 分场景确认（**已完成 2026-08-30**，M4 已立项待启动）

- **【2026-09-03】fix(体验)：三处交互 Bug（合同误判 B 类 / F12 确认两跳 / 切页断流）**：用户实测问题修复。决策留痕 D-0903-6/7/8。
  - **fix(场景分类误触发，D-0903-6)**：`src/rag/scenes.py` contract_draft/contract_review **移除普通关键词里的裸「合同/协议/条款」**，只由动作强特征词触发，并补足常见触发词（帮我审/审一下/合同模板/范本…）——实测首页 6 条示例里 2 条含「劳动合同」的普通咨询此前全被判成 contract_draft（B 类弹确认），「合同纠纷去哪个法院起诉」「租房合同没到期想退租」同样中招；修复后全部回落 A 类自动回答。场景清单是数据（只改 `SCENES`），分类逻辑零改动。
  - **feat(F12 确认同连接续跑，D-0903-7)**：`approved=True` 的 `POST /api/chat/confirm` 在写确认标记后直接返回 SSE 事件流（与 `/chat/stream` 共用抽取出的 `_build_stream_response`：断线重连/主动取消/事件日志/归属登记全同语义），前端新增 `confirmSceneStream` 消费该流——点「确认执行」后直接出答案，不再"确认一次 + 再发一次 /chat/stream"两跳；`ConfirmRequest` 新增 `history`/`request_id`（续跑携带本提问之前轮次，避免重复把当前问题当历史）。标记仍写入 → 旧客户端重发 stream 兼容。F12 接口测试随新语义重写（假 Agent 防真 LLM）。
  - **fix(前端切页断流/延迟全冒，D-0903-8)**：`ChatView` 路由切换不再 abort 掉 SSE——生成在后台跑完并持续写回 Pinia store（新增 `activeStream` 镜像思考轨迹），组件卸载只禁局部/DOM 更新（`viewActive` 守卫）；回页 `onMounted` 有进行中请求则跳过历史重拉（防覆盖在途内容），无则走服务端历史并做「本地 ahead 保护」；会话保存基线与串行队列从组件迁入 chat store（跨路由存活，防重挂载基线丢失 → 误全量 replace 与旧保存竞争重复）。主动中断请用「停止」按钮。
  - **refactor(api/routes.py)**：`chat_stream` 尾部抽 `_build_stream_response()`（Agent/固定管线选路 + 断开监听 + watchdog + 取消标记 + 归属登记）供 `/chat/stream` 与 `/chat/confirm` 复用；拒绝/预算短流统一 `_sse_response()`。验证：场景/F12/重连相关测试全绿 + 前端 `vite build` 通过。

- **【2026-09-03】feat(F15)：日志与 Token 计费面板（6 commits，测试 →870+）**：M3 全部收尾后首个新功能，**旁路新增一套 token/金额观测层，不动 F14 次数熔断主链路**。方案 `docs/F15-日志与Token计费面板-技术方案.md`。
  - **DDL**：`docker/init.sql` 新增 `usage_logs`（每次付费调用一行：source/model/tool/backend、cache_hit/miss_tokens 拆分、est 估算标记、`cost_cny` 金额快照——改价不漂移历史，明细留原始 token/积分可重算）+ `pricing`（key-value 价格表）。`tests/test_f15_usage_ddl.py`（6 项文件级守卫防列失配）。
  - **存储层**：`src/observability/usage_store.py`——usage_logs 落库（失败 debug 吞掉，观测故障不拖垮主链路）、计价纯函数（deepseek 按 cache hit/miss 分档计价、ollama/qwen 免费、pkulaw 工具→积分映射 purpose 语义：语义检索 125/精确 25/识别 125）、聚合查询（纯 SQL GROUP BY **不建 rollup**，summary 补零到 N 天保证趋势图连续）、价格表 list/upsert/reset + 进程级缓存。`config.py` 新增 `PRICING_DEFAULTS`（官方刊例 2026-09-03 查证：deepseek-v4-flash 命中 ¥0.02/未命中 ¥1/输出 ¥2 每百万、tavily $0.008/credit 免费额度内仅估算、北大法宝 ¥18/6000 积分起）。`tests/test_usage_store.py`（26 项）。
  - **LLM 埋点**：`src/llm/usage_callback.py` `LLMUsageCallbackHandler`——**独立于预算 handler**（后者 on_llm_end 语义是「不再计数」防重复计数，token 采集是另一关注点不能混入）；on_llm_end 读 usage_metadata，DeepSeek cache 拆分三级降级（usage_metadata.input_token_details → response_metadata.usage.prompt_cache_hit_tokens → 全 miss 兜底），流式/Ollama 无 usage 时 tiktoken 估算标 est；on_llm_error 不记。openai/ollama 两后端 ChatModel 挂 `[*budget_callbacks(), *usage_callbacks(backend, model)]`，ChatOpenAI 开 `stream_usage=True`。`tests/test_usage_callback.py`（11 项）。
  - **外部 API 埋点**：Tavily `search()` 成功后 `record_tavily_usage`；pkulaw `_run()` 成功返回前 `record_pkulaw_usage(purpose)`——一个埋点覆盖 Agent 工具与固定管线两条路径；失败/超限不记。`tests/test_usage_instrumentation.py`（4 项）。
  - **API**：`/api/usage/summary|detail|breakdown|pricing(GET|PUT)` 五接口，全部 `require_registered_user` 硬鉴权（登记进 `test_route_auth_guard.py` MUST_BE_HARD）；lifespan 首启 `ensure_pricing_defaults` 幂等灌默认价（失败静默）。`tests/test_usage_api.py`（13 项）。
  - **前端**：`frontend/src/views/UsagePanel.vue`（/usage 路由）——KPI 卡（今日 tokens/费用/调用/熔断状态）+ 近 N 日费用柱状（纯 CSS 无新依赖）+ 按来源构成 + 调用明细分页（est 徽标）+ 价格设置抽屉（改价即时生效/恢复默认）；ChatView 顶栏加「用量计费」入口。`vite build` 通过（70 modules）。

- **【2026-09-03】fix(前端)：切换/新建对话丢消息与串会话（会话级隔离）**：`ChatView` 保存链路此前用**全局单值 savedCount**、且保存请求在异步链里才读 `chat.sessionId`/`chat.messages`——回答生成中切走或切走后旧请求收尾，会把内容：
  ① 保存进**新会话**（DB append 串话，B 会话看到 A 的回复）；② 或因为视图已切走、`isActiveView=false` 而**不保存**（切回 A "回复没了"，只剩已落库的问题）。改动：
  - `persistSession(sid, extraMsgs?)`：基线改**按会话独立**（`savedCounts[sid]`）；body 在**调用瞬间快照**、sid 固化，异步链不再读全局状态——已排队的保存不受切换影响；
  - `runStream(query, recent, sid)`：sid 由 `handleSend` 发起时固化贯穿；思考轨迹改 `localThinking` 独立收集；**中断（Abort/切走）时已有部分/完整回答也落库回原会话**（此前 catch 分支完全不保存）；视图写入加 `isActiveView()` 守卫（切走后不再污染新会话视图）；
  - `handleNewChat`/`handleSelect`/`confirmProceed`/`doRewrite` 全部传 sid；收尾共享状态清理加 `abortController.value===ctrl` 守卫（旧请求收尾不得清空新请求的 controller / 误置 sending）；
  - `stores/chat.js`：sessionId 存储 localStorage → **sessionStorage**（窗口级隔离，多标签页各自独立会话，杜绝 A/B 窗口共用同一 session 串话）；
  - `api/index.js`：`saveSession` 返回服务端 `total`，前端按会话精确推进基线。验证：`vite build` 通过。
- **【2026-09-03 下午】store 类接池收官（5 commits，测试 868→870）**：报告中期项 4（连接池）从「部分」升为全部完成。
  - **fix（池向量注册前置）**：`VectorAwarePool` 组合包装裸池时，register_vector 只覆盖直连路径，池自行创建的连接不注册——向量 store 接池必然类型错乱。改**子类化 ThreadedConnectionPool** 覆写 `_connect()`（池内所有连接的唯一起点），直连兜底同样补注册。
  - **perf（6 个 store 类逐一接池）**：`QueryLogger`/`ConversationMemoryManager`/`FAQCache`/`ConversationStore`/`PgvectorStore`/`PgvectorRetriever` 全部从「常驻单连接 + threading.Lock + 手写重连」改为**每操作从 `db_connection()` 借连接**。要点：conn 是方法内局部变量（挂实例会在并发时互相覆盖）；建表 DDL 惰性执行一次（加锁幂等）；save_memory 的 LLM 摘要生成不占连接；`ArticleRouter` 不再挖 store 私有 `_lock/_conn`。测试改拦模块级 `db_connection`（DB-free，CI 无 PG 可跑）。全仓无裸 `psycopg2.connect`。
  - **docs**：`.env.example` 补 `PG_POOL_MINCONN/MAXCONN` 与 `COOKIE_SECURE`；AGENTS.md 同步测试环境说明；审查报告长尾标注改为已收官。

- **【2026-09-03】审查报告中长期项收尾（9 commits，测试 810→868）**：按 `docs/代码审查报告-2026-09-01.md` 中期/长期清单逐项修复（每项红→绿→全量回归→独立 commit）。
  - **fix（测试环境隔离）**：conftest autouse 清空 `TAVILY_API_KEY`（patch 使用点 `src.agents.tools` 而非 src.config——模块级导入副本改不动）——修复 2 个随本机 .env 漂移的用例（`test_tool_result_failure_marked` / `test_parallel_tool_calls_sse_events`，基线 810→808 的 2 个失败）。
  - **fix（F14 预算计数 TOCTOU）**：check(GET) 与 record(INCRBY) 两次往返存在并发窗口，日限额被放大到 limit+(并发-1)。改**预占模型**：`reserve()`/`release()`/`check_and_reserve()`，Redis 走 Lua 原子脚本（INCRBY→比较→超限回滚），失败归还；`check()` 保持只读（入口前置拦截用）。Lua 失败退化**非原子 Redis 路径**而非进程内计数——否则 reserve 写内存、used() 读 Redis 两边不一致会让熔断彻底失效。enforce=false 观察期照实计数不拦截。三处埋点（LLM callback / Tavily / pkulaw）预占后不再重复 record（否则日限额腰斩）、失败路径 release。`tests/test_budget_atomic.py`（20 项，含 40 线程并发断言成功数恰等于 limit）。
  - **feat（降级可观测化）**：`/api/health` 新增 `degraded`/`degraded_reason`/`active_backend`/`budget_exceeded`——主后端一次 401 静默切 Ollama 的场景运维终于可见。FailoverLLMBackend 记录降级原因（创建期/运行期/回切清空）、LLMAdapter 透出；观测失败 fail-open（健康检查挂掉会让 LB 摘实例，比信息缺失严重）。`tests/test_health_degraded.py`（13 项）。
  - **perf（PG 连接池）**：新增 `src/db/pool.py`——进程级 ThreadedConnectionPool（minconn/maxconn 默认 2/20，惰性创建，import 不触发建连）、每连接自动 register_vector、`db_connection()` 上下文管理器（异常回滚、归还兜底、池不可用退化一次性直连 fail-open）。auth 的 4 处调用点（register/login/load_token_cache/verify_token——每个认证请求的热路径）与 intent/law_centroids 一次性直连接入。测试期连接池强制关闭（conftest：db-mock 测试打桩的是全局 psycopg2.connect）。`tests/test_db_pool.py`（12 项）。长尾：6 个 store 类「单连接+锁 → 每操作借用」另行改造。
  - **perf（前端流式渲染节流 + 会话增量保存）**：ChatView 每 token `nextTick()+scrollHeight`（强制同步布局）改 rAF 合并；会话保存从每轮上传全量 messages（O(n²) 流量）改 `mode=append` 增量——服务端 SQL 内 JSONB `||` 原子拼接，前端 savedCount 基线（首次全量/加载后追加/失败不推进基线重传/串行化防并发重复），记忆固化按追加后总条数触发且取全量历史。`tests/test_session_append.py`（6 项）。
  - **ci（前端 job）**：CI 新增 frontend-build（npm ci + vite build）——此前前端改动零校验。
  - **fix（Markdown 渲染器换成熟方案）**：自研渲染器每加语法都要重新证明安全 → markdown-it（html=false/linkify/breaks）+ DOMPurify 白名单双层防御 + 链接强制 target=_blank & rel=noopener。bundle 151→297KB（gzip 58→117KB）换安全可维护性。
  - **build（Vite 升级）**：vite 5.4 EOL（esbuild 0.21.5 在 CVE 范围）→ 7.3 + plugin-vue 6；`build.outDir='../static'` 显式 `emptyOutDir: true`（实证旧 hash 产物已积压 6+ 个）。
  - **fix（Token 迁移 HttpOnly Cookie）**：凭据从 localStorage 迁到 HttpOnly + SameSite=Strict Cookie（30 天，Secure 由 COOKIE_SECURE 控制）——XSS 无法再窃取。get_current_user 先 Cookie 后 Bearer（兼容通道保留）；新增无鉴权幂等 `POST /auth/logout` 清 Cookie；前端 stores/auth.js 只留非机密用户名 + 一次性清残留 lawrag_token，路由守卫改查 username，登录页 onMounted 用 /auth/me 实测 Cookie。`tests/test_auth_cookie.py`（9 项，DB-free）。

- **【2026-09-02】审查整改收尾（B1–B8，9 commits，测试 784→810）**：按 `docs/代码审查报告-2026-09-01.md` 剩余未完成问题逐项 TDD 修复（每项红→绿→全量回归→独立 commit，新增守护测试均做过「回退转红」验证）。
  - **fix（B1 降级盲区）**：重试耗尽此前抛裸 `RuntimeError` 丢状态码，failover 判定「非 4xx」不降级——持续 429/5xx 时整条链路失败，Ollama 兜底用不上。新增哨兵 `LLMRetryExhaustedError`（`retry.py`，携带 last_error 的 status_code），openai_backend 三处「已重试 N 次」改抛它，failover 视为主后端不可用 → 降级（裸 RuntimeError 仍不降级，防误判）。`tests/test_retry_exhausted_failover.py`（10 项）。
  - **fix（B2 降级单向不可恢复）**：一次瞬时 401/403 就让进程永久切 Ollama，只能重启。failover 降级后进入冷却窗口（默认 300s，`recovery_cooldown_seconds`=0 禁用回切），冷却结束后**下一次真实请求兼作健康探测**——成功回切、失败继续降级并刷新冷却（不为探测单独发 ping，零额外 Token/RTT，探测失败由备用端无感应答）。⚠️ 复用真实请求做探测 = 探测期一次额外的主后端调用失败会被吞掉并走备用，属有意取舍。`graph.py::_react_enabled` 由构造期固化改**动态属性**（能力构造期定、降级实时求值），ReAct 图与固定管线图预构建、运行时切换——回切后 ReAct 能力自动回来。`tests/test_failover_recovery.py`（14 项）。
  - **fix（B3 resume 跨用户重放）**：`/chat/stream/resume` 硬鉴权后仍可拿别人的 request_id 重放问答全文。`/chat/stream` 发起时登记创建者（`StreamEventLog.set_owner/get_owner`，Redis SETEX TTL 同事件日志，登记失败只告警不阻断），resume 校验「请求者==创建者」，不匹配 403；无归属登记的旧流放行保断线重连。`tests/test_stream_resume.py` +7 项。
  - **fix（B4 工具参数零校验）**：`registry.execute()` 直接 `executor(**arguments)` 展开 LLM 生成的参数，schema 的枚举/类型约束只在发给模型时生效。带 `langchain_tool` 的工具执行前经 `tool_call_schema`（与发模型的同一份 pydantic 约束）`model_validate` 再调 executor，ValidationError 归一化为「参数校验失败」、幻觉参数白名单丢弃。⚠️ 不走 `langchain_tool.invoke()`：`BaseTool.run` 会把 ToolResult 拍平成字符串，破坏结构化结果契约。`tests/test_tools.py` +6 项。
  - **fix（B5 前端资源泄漏）**：KnowledgeView 上传轮询 timer 仅存闭包（切路由后空转打到关标签页）→ timers Map 收集 + onBeforeUnmount 统一 clear（照搬 CrawlView）；ChatView 全文件无 onBeforeUnmount → 生成中跳路由时 abort SSE；`consumeSSE` 用 try/finally `reader.cancel()` 释放底层连接。
  - **fix（B6 前端 401 静默失败）**：token 过期后路由守卫只校验存在性，用户停留页面但所有请求空 catch 静默失败。api 层响应侧统一收口：401 → logout + 回登录页（登录/注册端点豁免；router/auth store 动态 import 避循环依赖；1s 防抖防并发跳转）。
  - **fix（B7 部署弱口令）**：docker-compose 三处明文 `lawrag123` → `${POSTGRES_PASSWORD:?required}`（未设置拒绝启动），**移除 db 5432 宿主映射**（db 仅需 compose 网络内被 app 访问）；`.env.example` 补变量与生产强口令提示。
  - **refactor+chore（B8 死代码清除）**：删除 `src/llm/client.py`（392 行，自建 ollama.Client、无预算埋点、无 Failover——误用即绕过 F14 与降级链路）。`Message` 数据类迁入 `src/llm/base.py`，routes/nodes/engine 引用清零、engine `llm: LawLLM` 注解改 `LLMBackend`；`evaluation/scripts/eval_answer_quality.py` 的 judge_batch 迁统一 `create_llm_backend`；删除专属冒烟脚本 `evaluation/scripts/test_llm.py`。`tests/test_llm.py` 保留 Message 用例。
  - 前端无测试框架，B5/B6 以 `vite build` 为门禁（46 模块）。

- **【2026-09-01】代码审查整改（安全/资源管理，7 commits，测试 607→788）**：全库静态走查 + 实证验证（`docs/代码审查报告-2026-09-01.md`），先修高/中危：
  - **fix（F14 熔断生效，高危）**：`LLMBudgetCallbackHandler.on_llm_start` 抛的 `BudgetExceededError` 被 LangChain 静默吞掉（callback `raise_error` 默认 False，实测 FakeListChatModel+抛异常 handler 照常返回）——请求内 18~20 次 LLM 调用无法中断，只有入口前置兜底。加类属性 `raise_error=True`。`tests/test_f14_budget_guard.py`（8 项）。
  - **fix（路由鉴权，高危）**：`GET /api/knowledge/documents`、`GET /knowledge/documents/{id}/chunks` 无鉴权匿名可分页拉全库；`POST /api/rewrite` 无鉴权+无预算+无限流可匿名刷 LLM 绕过 F14；`/api/budget` 文档写需登录实为匿名可达（`get_current_user` 回退匿名）；resume/crawl/status/types 无鉴权。全部改硬鉴权 `require_registered_user`（7 路由），rewrite 另加预算前置检查 + IP 滑动窗口限流（20/min，429+Retry-After）；前端 resumeChat 补 authHeaders。守护测试 `tests/test_route_auth_guard.py` + 审计脚本 `scripts/audit_route_auth.py`。
  - **fix（PG 连接泄漏）**：`_get_store()` 每请求新建 `PgvectorStore(_pg_conn)` 且全项目 `store.close()` 零调用 → 改模块级单例（双检锁），应用 shutdown finally 释放。`tests/test_store_singleton.py`（6 项，含 8 线程并发）。
  - **fix（密钥日志）**：factory 日志不再输出 API Key 前 8 位，只打长度。
  - **fix（后台任务 GC）**：`asyncio.create_task` 返回值被丢弃（只持弱引用，任务可能完成前被 GC）→ `_spawn_background()` + 模块级 `_BACKGROUND_TASKS` 强引用、完成即 discard；顺带修正 upload_document docstring 位置。
  - **fix（pkulaw 参数加固）**：`top_k` 钳制 [1,50]、可变默认参数 `=[]` 改 None 兜底。
  - **chore（死代码清理）**：retry.py 重复分支合并、`ToolCall.to_message` 删除。

- **fix（B2 二阶段 NO-GO，诚实入库）**：法名质心加权端到端实测**双集恶化**（colloq148 73→66.9、multi100 92→85，boost 单调伤害），判定 NO-GO 已回退（`LAW_NAME_BOOST_*` 默认关闭，代码与实验链保留）。⭐ 根因：**信号同源**——质心向量与主检索同为 bge-m3 产物，加权不提供正交增益只注入噪声；置信门控亦无效（gold 在/不在 top3 的相似度分布重叠）。质心法保留其正确用途（法名推断本身 Recall@3 70.3%：用户提示/M4 plan 信号）；无法名查询的真实提升路径 = 正交信号（LLM 查询改写/同义词扩展，优先改写）。报告 §6 已更新：docs/B2-法名推断spike报告-2026-08-30.md。新增 `src/rag/law_centroids.py`/`law_name_boost.py` + `tests/test_law_name_boost.py`（9 项）。

- **docs（B2 spike 报告）**：`docs/B2-法名推断spike报告-2026-08-30.md`——两个实现变体（描述文本法 vs 条文质心法）的实现方式、colloq148 实测对比（质心法 Recall@3 70.3% vs 51.4%）、归因分析（分布一致性/关键词有损/覆盖面）、**选型条文质心法**与转正形态（PG 小表 + 在线微秒点积）、端到端接入设计与双集验证判据。流程教训：两变体曾共用默认 tag 致报告覆盖（E-02），已独立 tag 留档。

- **feat（B1 评测集补强）**：多标注 + LLM 口语集，修正评测标尺（真实 DeepSeek API，冒烟→全量）。
  - **多标注**（`evaluation/scripts/annotate_multi_label.py`）：语义集 100 条单标注 → LLM 辅助扩为 2~5 条（候选=向量top10+BM25 top10 并集，从严判定「能直接作为回答依据」，**原标注永远保留**）；57/100 条被扩充，新增 130 条标注。**实测验证审计结论：Hit@5 67%（单标注口径）→ 92%（多标注口径）**——旧绝对值确实被系统性低估了 25 个点，MRR 0.489→0.738。人工抽审样本 25 条已输出（`evaluation/data/lexeval/multi_label_review_sample.md`，样例质量高：高空抛物→民法典1254条+侵权责任编解释第24条）。
  - **LLM 口语集**（`evaluation/scripts/generate_colloquial_llm.py`）：148 条**禁止出现法名与条号**的自然口语（规则模板做不到的最难场景），覆盖 148 部法律，硬校验零病句入库。
  - ⭐ **重要认知修正**：无 法名口语查询 PROD Hit@5 = **73.0%**（MRR 0.598，Hit@1 49.3%），远高于 8/30 规则集测出的 6.7%（n=15）——6.7% 那批是规则模板的病句级查询（"电梯有什么规定"），**系统对"无 法名但表述清晰"的真实口语能力比预想好得多**；B2 法名推断的价值定位据此修正：不是"救 6.7%"，而是"把 73% 推向 precise 的 97.5% 水平"（法名仍是最强信号，缺失时 Hit@1 掉 14 个点）。BM25 单路在无 法名集仅 34.5%，词面路对语义查询贡献有限（与 8/29「常开不划算」结论互洽）。
  - eval_retrieval.py 新增 `--tag`（换评测集必须带，防报告覆盖——E-02 制度化）；新报告 `retrieval_{prod,bm25}_{multi100,colloq148}.txt`。
  - 冒烟抓 bug：多标注去重 key 须归一化法名（原标注带 "(2025修订)" 后缀 vs 检索返回不带）。

- **feat（A3 / D-M3-12 实现）**：SSE 断线重连继续生成——**事件日志 + seq 游标重放**，零新增依赖（不接 checkpointer）。
  - 新增 `src/observability/stream_log.py::StreamEventLog`：每事件带递增 seq 写 Redis List（`lexagent:stream:{request_id}:events`，RPUSH+EXPIRE，TTL `STREAM_LOG_TTL_SECONDS` 默认 600s）；终局标记 `__stream_end__` 让重连方知悉流已结束（含取消场景）；Redis 不可用退化进程内、写失败告警后照常投递在线流（日志故障不阻断主链路）。
  - **桥接退出语义重构**（`_bridge_sync_stream`）：事件**先写日志再投递在线队列**（日志=重连补发的唯一真相源）；**只有主动取消杀 worker**——被动断线（有日志）worker 继续跑完持续写日志，在线协程立即返回不等待；无 request_id（无人能重连）保持旧行为立即停。判定收敛在 `_on_exit_gone`。
  - 新接口 `GET /api/chat/stream/resume?request_id=&after_seq=`：重放游标之后的事件 → 生成仍在进行则 0.5s 轮询跟进 → 终局标记/不活跃/再断开/兜底时限即止。**SSE 事件新增 `seq` 字段**（前端游标，向后兼容）。
  - 前端：SSE 解析重构为共用 `consumeSSE`；`runStream` 对**非用户主动取消**的网络中断自动 resume 续流（最多 2 次，thinking 提示"连接中断，正在重连续流…"）；用户点停止不重连。
  - 新增 `tests/test_stream_resume.py`（12 项：日志单元/被动断线跑完写全日志/取消立即停/无日志立即停/worker 注册注销/resume 400/404/重放+[DONE]）。全量 730 passed。

- **fix（CI，Python 3.12/3.13 行为分叉）**：工具 description 在 Python 3.12 下带源码缩进发给 LLM——LangChain `StructuredTool.from_function` 在 `parse_docstring=False` 时直接取裸 `source_function.__doc__`，而 **3.13 起编译器自动去 docstring 缩进、3.12 保留**；本地 venv 是 3.13.5 所以测试全绿，CI 的 3.12 才暴露（`test_docstring_becomes_description` 失败）。修复：`@tool` 装饰器派生 description 时显式 `inspect.getdoc`（=cleandoc，全版本确定）再传给 LangChain，显式 `description=` 参数优先级不变。**影响面**：3.12 部署环境所有多行工具描述此前一直带缩进（LLM 路由引导信息受损，非崩溃）。Docker `python:3.12` 实测 18 项全过。教训入库 E-13。

- **feat（F12 v1 完成 / D-M3-9a，M3 收官）**：B 类场景**进入图之前的一次人工确认**，路径 A 落地——确认发生在任何 LLM 调用之前（重跑零浪费），**零图改动、零新增依赖**（不接 `interrupt()`、不加 checkpointer，spike 结论兑现）。
  - 新增 `src/memory/confirmation_store.py::ConfirmationStore`：Redis `SETEX`（key=`lexagent:confirm:{user}:{session}`，value=已确认 query 原文防换题 R7，TTL `CONFIRMATION_TTL_SECONDS` 默认 600s=Q7 决策）；Redis 不可用退化进程内、读取异常 **fail-open 回落 A 类**（确认机制故障不阻断主链路，D-M3-8 同款原则）。
  - `ask()` / `stream()` 双路径同口径：场景分类后、FAQ 之前插入确认分支；B 类且未确认 → SSE 新事件 `confirmation_required`（scene/scene_name/prompt/options/confirm_id）或非流式 `ChatResponse.confirmation` 载荷，随即结束（`stream()` 增加 `session_id` 参数，`/api/chat`、`/api/chat/stream` 已透传）。
  - 新接口 `POST /api/chat/confirm`（approved=True 写标记 / False 清标记；校验仅接受 B 类场景 id）。
  - 前端确认卡：ChatView 复用改写卡样式，`confirmation_required` → 展示确认/取消 → `/chat/confirm` → 重新发起 stream（同 session_id）。
  - 新增 `tests/test_f12_confirmation.py`（18 项：存储 TLL/换题/fail-open、A 类不受影响、B 类拦截零消耗、确认后执行、会话隔离、接口校验）；`test_ask_writes_scene_into_state` 因 B 类查询被门闸拦截改为预确认后进图。
  - **已知边界**：确认标记在进程内回退模式下重启丢失（Redis 模式有 TTL）；B 类拦截依赖 F11 关键词准确率（R8，回落策略保守）。

- **docs（M4 立项 / D-M4-1）**：多 Agent 演进路线图定稿——D-M3-11 重启条件因产品方向（多 Agent 多场景、意图识别中的规划）正式满足。两条核心原则：**生长式迁移**（新 Agent 用生态标准件长成编译子图，主 Agent 自有循环不重写）、**按场景拆出口不按工具拆内脏**（检索/搜索/核验保持为工具）。阶段 0=M3 收尾 → 1=plan 对象+场景白名单（搭车切 BaseTool 绑定）→ 2=审核子图+类案子图 → 3=supervisor 定型。新增 `docs/M4-多Agent路线图.md`；前置条件：eval_answer_quality 基线先于阶段 2。
- **docs**：新增 `docs/常见错误清单.md`——复发错误知识库（症状→根因→预防），收录规则：同类错误第二次或高危静默故障。首发 13 条（E-00~E-12），含当日新教训 E-12「删兼容分支前必须追运行时数据流，grep 调用点证明不了数据形态」。
- **chore**：`ruff format` 进 CI 门禁（`format --check`，line-length=120 对齐存量风格），全量格式化一次（102 文件，独立 style 提交 `206550e`，`.git-blame-ignore-revs` 已收录）；提交前固定动作 = ruff check + format check（E-07 复发两次后制度化）。
- **refactor**：清理 D-M3-13 迁移遗留——删 `tools_node` 的 `parse_error` 死分支（LangChain tool_calls 已是解析后 dict）、删零调用的 `parse_tool_arguments`、`_tool_calls_to_openai` 三形态收敛为两种活形态（ToolCall + LangChain dict；OpenAI 原始 dict 形态无生产路径）。⚠️ 过程中曾误删 LangChain dict 分支（18 测试失败暴露 `_messages_to_dicts` 的运行时形态），教训入库 E-12。

- **fix（D-M3-14，高危回归）**：ReAct 决策调用绕过 `chat_with_tools` 入口，丢失重试与降级语义——D-M3-13 迁移时 `agent_node` 改为直接 `chat_model.bind_tools().invoke()`，同时绕过了该入口链路上的两层既有语义：**D-M1-3 重试**（瞬时 429/5xx/网络错误不再重试，一次抖动直接让整轮 ReAct 给出"模型调用失败"答案）与 **FailoverLLMBackend 4xx 运行期降级**（主后端 400/401 不再切 Ollama，AC-7/REQ-U1 语义在主路径失效）。修复为回到 `llm.chat_with_tools()` 公开入口（后端内部仍是 bind_tools + invoke，D-M3-13 成果不变）；预算 callback 挂在 ChatModel 上不受影响。新增 `TestAgentNodeCallSemantics` 3 项回归（4xx 降级 / 429 不降级 / 正常路径走公开入口）。
- **fix（D-PKULAW）**：`PkulawLegalClient` 把 `BudgetExceededError` 包装成普通 RuntimeError，丢失「法宝额度已用尽」熔断语义（legal_source_search 路径下额度用尽被混同于普通子源故障）。改为原样上抛，门面 `errors` 里保留完整熔断文案；参数化测试守两条路径（search_law / search_case）。
- **chore（CI）**：清理 2 处未使用 import（`pkulaw_search.py` 的 `Any`、`test_pkulaw.py` 的 `BudgetExceededError`）——8a58fa8/b526f6c 两个提交未本地跑 ruff，推送即会卡 CI（重演 8/27 教训：提交前必跑 `uv run ruff check src/ tests/`）。

- **feat（F9 扩展 / D-PKULAW）**：接入**北大法宝 MCP** 官方法律源（`src/search/pkulaw_mcp.py`）。
  - 高权威：法条原文 + 类案全文 + 核验 + 超链，融合验证状态 `verified_official`（与现有官方源同级），补齐国家法律法规数据库「仅目录无正文」、Tavily 域限定「仅线索」的短板。
  - 两条路径：① 后端源 `PkulawLegalClient` 注册进 `LegalSourceClient` 门面，`legal_source_search` 自动融合；② ReAct 工具 `pkulaw_search`（检索）/ `pkulaw_verify`（核验+加链），按 `PKULAW_ENABLED` 与可用性注册。
  - 懒加载 `mcp` SDK（未装不影响导入/单测）；运行时 `tools/list` 按用途关键词解析工具名（不硬编码，适配聚合端点前缀漂移）；参数平铺、结果按语义提取、清理 `.0` 锚点坏链。
  - 预算熔断：新增 `KIND_PKULAW`（`BUDGET_MAX_PKULAW_CALLS_PER_DAY` 默认 200），调用前 check / 后 record；超限工具层返回「法宝额度已用尽」不阻断主链路。
  - 配置仅 `.env`（`PKULAW_MCP_URL` / `PKULAW_MCP_TOKEN`），新增 `tests/test_pkulaw.py`（23 项：归一化/工具名解析/门面/融合/工具/预算/注册）。
  - 决策记录：用户给的 10 个独立端点经聚合 `mcp-law-agg` 一个 URL 暴露全部工具，故默认直连该聚合端点；按积分计费需预算兜底（AGENTS.md 规则 8）。
  - **真端点已联调通过**：`tools/list` 返回 10 个工具，8 个用途全部运行时命中零兜底；`get_article('中华人民共和国民法典','1077')` 返回正确条文原文 + `pkulaw.com` 官方链接 + `law_status=现行有效`。
    - 真实工具名为**点分隔**（`mcp-law-search-service.search_article`），与 SKILL 快照的下划线兜底名不同——运行时解析是必需的，不是优化。
    - 另有 `mcp-case.get_case_list`、`mcp-fatiao.get_law_item_content` 两个工具暂未映射用途，需要时加进 `_PURPOSE_KEYWORDS`。

- **fix（D-PKULAW）**：`pkulaw_mcp._a_call` 调用 `async def _discover` 漏 `await`，协程从不执行 → `_tool_map` 恒空、静默退化为静态兜底名。因兜底名与真端点命名规则不同（下划线 vs 点），退化后**所有真实调用必然失败**，且不抛异常、Fake 单测绕过 `_a_call` 抓不到，属高危静默故障。
  - 新增 `TestPkulawToolDiscovery` 两项回归（已反向验证：移除 `await` 即失败并报 `coroutine was never awaited`）。

- **fix**：HybridRetriever 权重重定（w=3.0 → 0.5）——向量路排查（`docs/向量路质量排查-2026-08-29.md`）实证 w=3.0 的 BM25 词面排序在「法名+语义」查询上碾压向量排名，语义集净丢 6 条（"盗窃罪的立案标准"向量 top1 命中、生产链路丢失），恰好抵消 ArticleRouter 的全部收益。
  - 双集实验定值：语义集 Hit@5 62%→**67%**、MRR 0.469→**0.489**；法条级集（339 条）86.1%→**85.3%** 仅让 0.8 点——两集最优平衡
  - **"收紧激活条件"不可行**：受损查询与受益查询结构完全相同（法名+主题词），正则无法区分；激活本身合理，问题在权重
  - 方法论教训：8/3 的"零干扰验证"只看 Hit@5 聚合值，掩盖了"救 6 丢 6"的净零假象，问题存活 26 天——检索层改动必须逐条 diff + 语义/法条级双集验证
  - `src/config.py` 默认值同步 0.5（含原因注释）；`.env` 同步（本地配置，不入库）
- **docs（结论）**：**BM25「常开」不采用**——双集实测均不如条件激活（语义集 Hit@5 67%→61%、法条级 85.3%→84.1%）。新增 `HYBRID_ALWAYS_ON` 开关（默认 false）供后续重新评估。
  - 值得记的形态：常开的 Hit@10 反而最高（74% vs 72%）而 Hit@5/MRR 最低——BM25 能补长尾召回，代价是把词面相近但语义不相关的条文挤进前排、搞坏头部精度。生产上进 prompt 的主要是前 5-8 条，故 Hit@5/MRR 才是关键指标
  - 这同时解释了为何「BM25 只补充不重排」不可行：BM25 对语义查询是"广而不精"、只适合补长尾，对法条级查询却主要贡献重排收益，单一策略无法兼顾，条件激活正是按查询类型区分二者的机制
  - 新增 `tests/test_hybrid_retriever.py`（10 项）——HybridRetriever 此前零测试覆盖，现守住激活判据、always_on 默认值、权重对排序的影响（含 w=3.0 反超向量这条旧病灶回归）
- **fix**：`article_map.json` 重建（991 部法律 / 46520 条，原 887 / 44749）——数据增长后未重建导致相邻扩展对新法律空转
- **chore**：`ADJACENT_WINDOW` 2 → 1——恢复 8/3 报告 §4.4 既定结论（窗口大小对召回指标零影响，大窗口塞引用噪声进 prompt）
- **fix**：BM25 检索质量修复三连（碎片过滤 + 法名加权 + 输出按条文去重）——来自 2026-08-29 首次建立的检索基线评测（`evaluation/data/lexeval/results/`），BM25 单路 Hit@1 20%→24%、MRR 0.278→0.308；典型案例「刑法第二十条」修复前 top5 全是信托法/关税法等其他法律的第二十条，修复后 top1 正确命中。
  - **碎片过滤**：库里 ~3400 个「第九条」「第十七条、」纯条号引用碎片（占 6.4%）无任何语义，却因 DF 极高 + 文档极短（BM25 长度归一化占便宜）系统性挤占 top-k。判定：去掉条号字符后实质内容 ≤6 字（阈值经全库校准，放宽到 10 会误杀「正当防卫不负刑事责任」这类短而完整的表述）。索引 53235→50867 chunks
  - **法名加权**：法名只拼一次时 DF=该法 chunk 数，IDF 被稀释到低于条号 token（实测「刑法」3.53 < 「第二十条」4.11）——文档内重复 BOOST 次不改变跨 chunk DF，是纯增益加权
  - **输出去重**：同一条文的多 chunk（长条多段，实测「专利法实施细则 第四十二条」12 个 chunk 占满 top5）只保留最高排名，空位由后续条文补足
  - 已知遗留：「专利法第四十二条」仍命中实施细则（"专利法"是"专利法实施细则"的子串，法名 token 无精确匹配概念）——根治需查询侧法名识别 + metadata 精确过滤（两阶段检索），另行评估
  - `eval_retrieval.py` 新增 `--mode {vector,bm25}`；`tests/test_bm25_retriever.py` 扩至 20 项
- **fix**：BM25 停用词表误删 `万元`——金额类查询（"赔偿标准是多少万元"）丢失关键 token，属极隐蔽的召回损失（索引能建、查询能跑，只是"某些问题搜不到"）。顺带清掉死代码 `年月日`：jieba 会把日期切成 `['2023','年','1','月','1','日']`，该词作为整体**从未被产出过**。
  - 停用词表定位收紧为「只放真正的虚词」：BM25 靠 IDF 自动压制高频词，人工停用词边际收益很小，误删有区分度的词却是实打实的损失
  - 法律模态词（不得/应当/可以/规定）**暂不过滤**并在代码注释标明「待评测」——「用人单位不得解除劳动合同」里「不得」就是核心语义，是否过滤应由评测数据决定
  - `中华人民共和国` 保留为停用词（法名前缀归一化，让"中华人民共和国劳动合同法"与"劳动合同法"等价）
  - 新增 `tests/test_bm25_retriever.py`（13 项），覆盖停用词表内容、金额/日期 token 保留、法名归一化、纯虚词查询兜底
- **refactor（重大）**：LLM 层与工具层迁移到 LangChain 标准生态（D-M3-13，推翻 D-M1-1「不用 bind_tools」）——目标为生态标准化、降低学习成本。
  - **阶段① LLM 层**：`OpenAICompatibleBackend` / `OllamaBackend` 内部改用 `ChatOpenAI` / `ChatOllama`，经 `.chat_model` 暴露（`.model` 仍是模型名字符串，18 处调用点不动）；`agent_node` 改为标准写法 `chat_model.bind_tools(schemas).invoke(messages)`
  - **阶段② 工具层**：`@tool` 装饰器底层改为委托 LangChain 的 `@tool`，**删除自研 schema 推导与 `Param` 类**（约 100 行）；`ToolSpec.langchain_tool` 持有 `BaseTool`，新增 `registry.langchain_tools()` 供 `bind_tools()` 直接消费
  - **预算熔断埋点改 callback**：新增 `src/llm/budget_callback.py`，`LLMBudgetCallbackHandler` 在 LangChain 调用链路上 check + record——上层可绕过 `LLMBackend` 三个公开入口直接 `invoke()`，挂 callback 才不会漏计。原 `base.py` 的 `_budget_check` / `_budget_record` 已删除
  - 重试策略**不变**：仍是自研 `is_retryable` + `wait_and_log`（D-M1-3），包在 LangChain 调用外层；`ChatOpenAI(max_retries=0)`
  - 依赖：`langchain-core` 1.4.8→1.6.1、`ollama` SDK 0.4.9→0.6.2，新增 `langchain-openai` 1.6.0、`langchain-ollama` 1.1.0
  - ⚠️ **迁移后确认的行为差异**（测试已同步）：① `str | None` → `anyOf [string, null]`（原扁平 string）② schema 带上 `default`（原不写，对 LLM 是有益引导）③ dataclass 类型展开为对象 schema（原降级 string）④ **枚举必须用 `Literal`**：历史上的 `Param` 类 LangChain 不认识，会**静默丢弃** description 与 enum（不报错，只是发给模型的 schema 少了引导信息，极难发现）
  - 顺带清理：删除从未被生产代码使用的 `OllamaLangChainWrapper`（ChatOllama 直接替代）
  - ⚠️ **遗留**：本机 Windows 安全策略拦截文件删除，`uv add` / `uv sync` / `uv lock` 在重建 jieba（只有 sdist、无 wheel）时静默失败，**`uv.lock` 尚未更新**（pyproject.toml 已正确声明）。需在能正常 lock 的环境补做

- **feat**：F11 场景分类配置（D-M3-10）——新增 `src/rag/scenes.py`，将用户查询映射到 PRD §5.2 的 10 个业务场景并判定 A 类（全自动）/ B 类（需人工确认）。
  - **场景清单是数据、分类逻辑是代码**：`SCENES` 元组即清单（id/名称/A\|B/关键词/工具），产品后续调整只改清单不动函数，「需产品定清单」因此不再阻塞开发
  - 打分三级权重：正则 3.0 > 强特征词 2.0 > 普通关键词 1.0。三级是必需的——中文查询里「合同」这类通用词会同时命中起草/审查/检索多个场景，必须让「起草」「审查」等强特征词和「第X条」正则压过通用词
  - 未命中任何场景时**保守回落**默认 A 类场景 `legal_qa`（`matched=False`），绝不因分类失败阻断回答（REQ-UW）
  - `AgentState` 新增 `scene_id` / `scene_kind` / `scene_matched` 三个字段，`ask()` 与 `stream()` 两条路径在意图识别后、进入图之前完成分类（REQ-E1）；**不新增图节点，不改动图结构**
  - 流式路径新增一条 `thinking` 事件展示场景识别结果（复用现有事件类型，前端无需改动）
  - 新增 `tests/test_scene_classification.py`（55 项），含关键词冲突回归用例（查法条不能被误判为合同类 B 类场景）与 5 项 graph 集成测试（stream 产出场景事件 / ask 写入 state / 闲聊跳过分类）
- **docs**：F12 人工确认节点技术底座 spike 结论——新增 `docs/M3-F12-人工确认技术方案.md` 与验证脚本 `scripts/f12_spike_demo.py`（5 项验证全过）。
  - **核心结论：F12 v1 不需要 `interrupt()`、不需要 checkpointer、不改图结构**。确认点放在进入图之前时图还没开始执行，没有任何状态需要保存恢复，一个「已确认」标记就够了。F12 由此从「M3 唯一会动执行架构的改动」降级为「不碰图的小改动」，成本约为逐步骤方案的 1/4（~2 人日 vs ~6-7 人日）
  - **重要实证（风险 R1）**：无 checkpointer 时 `interrupt()` **不报错**，图静默停住、答案为空，前端只收到挂起事件后永远等不到结果，后端日志无任何异常；`get_state` 才抛 `ValueError: No checkpointer set`、恢复时抛 `RuntimeError: Cannot use Command(resume=...) without checkpointer`。这个失效模式极隐蔽，若将来上逐步骤确认必须在图构建处加自检
  - 另：`ChatRequest.session_id` 已存在，可直接作为 `thread_id`（`f"{user_id}:{session_id}"`）；当前为单进程 uvicorn 部署，`MemorySaver` 可用但重启丢状态
- **docs**：F13 结论为**仅文档收尾**（D-M3-11）——保留 `sub_agent` state 字段，不建规划节点扩展位。无具体多 Agent 需求时的预留大概率推翻，触发重新立项的条件已写入 `DECISIONS.md`
- **docs**：SSE 断线重连继续生成方案定稿（D-M3-12，**代码待开发**）——**Redis 事件日志 + seq 游标重放**，不接 checkpointer：每个 SSE 事件带递增 seq 写入 Redis List（`stream:{request_id}:events`，TTL 10 分钟），重连时 `GET /api/chat/stream/resume?request_id=&after_seq=N` 先重放后订阅。
  - **区分主动停止与被动断线**（关键设计）：用户点 `/chat/cancel` → 立即停（省 Token，现状不变）；网络断开（`http.disconnect`）→ 继续跑完并持续写 Redis（LLM 调用已发生、成本已沉没，跑完即可重连补发）。与现有 `/chat/cancel` 机制天然契合，改造集中在 watchdog
  - **与 F12 人工确认正交，不要混为一谈**：一次确认是等人确认、发生在任何 LLM 调用之前；断线重连是网络断了接着看，属 SSE 层可靠性问题。checkpointer 保存的是**图状态**，解决不了「已流式输出的事件流怎么补给重连用户」——所以两个功能都不需要 checkpointer，PostgresSaver 不引入，零新增依赖
  - 工作量估算：后端 ~100-120 行 + 前端 ~80-100 行，约 2-3 人日
- **feat**：F14 预算熔断——新增 `src/observability/cost_budget.py`，监控外部付费 API 日用量并自动熔断。
  - 计数口径：LLM 按**逻辑调用次数**（埋点于 `LLMBackend` 公开入口，SDK 重试不重复计数）；Tavily 按次（按次计费，口径精确）。流式一次调用只计一次
  - 存储：Redis 原子 `INCR` + TTL 到次日零点自动失效；Redis 不可用时自动退化为进程内计数并告警，**不因监控组件故障拖垮主链路**
  - 熔断粒度：LLM 超限 → 整体熔断（API 前置检查，流式/非流式都返回友好提示）；Tavily 超限 → 只停网络搜索，工具返回 `ok=False`「搜索额度已用尽」，内部库与官方源照常，**回答正常生成**
  - 可观测：超限打 ERROR 日志（同日同种类仅一次）；新增 `GET /api/budget` 状态接口（需登录）
  - 配置：`BUDGET_ENABLED` / `BUDGET_MAX_LLM_CALLS_PER_DAY`(5000) / `BUDGET_MAX_TAVILY_CALLS_PER_DAY`(500) / `BUDGET_ENFORCE`(true，设 false 只告警不拦截)；阈值设 0 = 不限制
  - 新增 `tests/test_cost_budget.py`（23 项）
  - **实测参考**：一次复杂查询约消耗 18~20 次 LLM 调用（ReAct 多轮 + 校验），默认 5000 次 ≈ 250~280 次查询/天，部署时按实际用量调整

## 2026-08-28 — M2 双路融合（已完成）

> 联调结论见 `docs/M2联调结论-2026-08-28.md`（双路径口径 / 配额策略 / 前端渲染约定）。

- **feat**：前端引用溯源可视化（M2 / F10 收尾）——`ChatMessage.vue` 每条来源加验证状态徽章（内部库绿 / 官方源蓝 / 第三方橙 / 网络未验证灰），标题栏显示来源构成汇总，含未验证线索时顶部显示警示条；`App.vue` 主题新增 `--color-warning` / `--color-success-light`（light + dark 双套）
- **fix**：网络线索融合后被系统性挤掉——`fuse_evidence` 纯按 `fused_score` 排序截断，网络（≤0.5）永远排在权威来源（≥0.5）之后，`top_k=8` 被填满时一条都不展示，Tavily 调用成本花了用户却看不到。新增 `_truncate_with_web_quota` 保底配额（`FUSION_WEB_MIN_SLOTS`，默认 2，设 0 关闭）；新增 6 项测试
- **fix**：非流式 `/api/chat` 未接融合结果——此前直接用原始 `retrieved_docs`（实测 74 条、无 verification、无去重），与流式路径行为不一致。改为优先用 `fused_sources`（去重 + 来源加权 + 验证状态），实测 8 条；`_dicts_to_retrieved` 与 `ChatResponse.from_rag_answer` 同步支持 dict 形态并透传 `source` / `verification` / `url`；新增 `tests/test_chat_response_sources.py`（11 项）
- **refactor**：工具定义改用 `@tool` 装饰器（D-M3-3）——`base.py` 新增 `Param` 与 `@tool`，从类型注解 + `Annotated` 元数据推导 OpenAI JSON Schema、docstring 作 description，产出与手写 class 一致的 `ToolSpec`。三个内置工具改为 `build_xxx_spec(dep)` 工厂 + 闭包注入依赖；新增 `tests/test_tool_decorator.py`（13 项）。零第三方依赖，纯语法糖，工具行为与 schema 不变
- **refactor**：SSE 流式路径改为 LangGraph 图执行驱动（D-M3-1）——新增 `_build_react_loop_graph()`（纯 ReAct 循环子图，入口 agent），`_stream_react` 用 `graph.stream(state, stream_mode=["updates","values"])` 替换手写 `while` 循环；消息累积交由 `add_messages` reducer、循环终止交由条件边，SSE 事件从图事件流映射。删除 `_merge_stream_update`（手工补框架行为的补丁）与手写 guard 计数器
- **fix**：SSE 流式路径 ReAct 消息历史覆盖 Bug——`_stream_react` 用 `state.update()` 整体替换 messages，导致第 2 轮 `tool` 消息前丢失 `assistant(tool_calls)`，DeepSeek 返回 400 并降级 Ollama。当时用 `_merge_stream_update` 补丁对齐 `add_messages` 语义，新增 2 项回归测试（消息序列完整性 + user 查询保留）——**根因已由上条重构用框架消除，补丁随之删除，回归测试保留**
- **fix**：轮数上限强制作答时 DeepSeek 在纯文本输出 DSML 工具调用语法——`agent_node` 在 `schemas=[]` 时注入引导 system 消息（"禁止再输出工具调用语法"），并新增 `_strip_dsml_tool_calls` 兜底清除答案中的 DSML 块
- **fix**：flk.npc.gov.cn API 改版适配——端点 `/api/search` → `/law-search/search/list`（POST JSON），参数 `keyword` → `searchContent`、`searchType` 改为整型（2=模糊），响应 `result.data.records` → `rows`，状态码映射更新（3=现行有效、2=已修改、1=已废止、4=尚未生效），title 含 `<em>` 高亮标签需清除，详情 URL 由 `bbbs` 构造
- 新增 `src/search/legal_sources.py`：官方法律源客户端（国家法律法规数据库 API + 人民法院案例库域内搜索 + 小包公可选），封装为 `legal_source_search` 工具（PRD F9）
- 新增 `src/search/fusion.py`：双路结果融合去重、按来源加权排序、冲突裁决（内部库优先，网络结果标注验证状态）（PRD F6/F7/F8）
- `AgentState` 新增 `web_results` / `legal_results` / `fused_sources` 字段，`tools_node` 累计三路证据
- SSE `meta.sources` 携带 `source` 与 `verification` 状态（内部库 / 官方源已验证 / 网络未验证）（PRD F10）
- 更新 `REACT_SYSTEM_PROMPT`：三工具决策规则 + 引用必须标注来源与验证状态

## 2026-08-13 — M1 工具调用型 Agent（已完成）

- **fix（2939ab3）**：修复 DeepSeek V4 空 tool_call 空转 Bug——parallel_tool_calls 恒启用，模型想直接回答时返回 name="" 占位 tool_call，导致空转 4 轮到上限。`openai_backend`/`ollama_backend` 的 `_parse_tool_calls` + `react_nodes.agent_node` 三处过滤空 name；新增 `tests/test_empty_tool_call.py`
- **fix（2939ab3）**：前端 `ChatView.vue` runStream 增加 tool_call/tool_result 分支，thinkingTraces 升级 `{text,kind}` 结构，`ChatMessage.vue` 历史渲染兼容
- **feat（393170f）**：LexAgent 初始提交（173 files，+39120 行），完整项目骨架 + M1 代码；README 重写反映 M1 新能力
- M1 核心能力：`LLMBackend.chat_with_tools()` + LangGraph 手动 StateGraph ReAct（N=5）；`FailoverLLMBackend` 降级；`ToolRegistry` + `retrieve_knowledge` / `web_search`（Tavily）工具；SSE tool_call/tool_result 过程透传；454+ 测试通过，QA 独立复核 488 passed / 0 failed
- 项目迁移：M1 代码从 Law-RAG-Agent 迁入 LexAgent，上游仓库恢复干净（M1 新增代码已回退）

## 2026-08-12 — 重构 PRD 与任务拆解

- 产出 `docs/自主Agent重构PRD.md` v0.1：固定管线 RAG → 工具调用型自主 Agent，确认 9 个关键决策（双后端、Tavily、官方法律源、内部库优先、Docker Compose、预算熔断、SSE 透明化等）
- M1 任务拆解为 8 个子事项（配置就绪/后端切换/工具框架/ReAct/Tavily/SSE 透传/提示词/测试回归）

## 2026-08-03 — 检索质量与流式稳定性（继承自上游）

- 检索评测与优化：条件 BM25 混合（RRF，w=3.0），法条级 Hit@5 75.8%→86.1%；相邻扩展窗口 ±3→±1；章级摘要 chunk 过滤（`docs/检索评测与优化报告-2026-08-03.md`）
- 流式响应稳定性修复（`docs/流式响应稳定性修复报告-2026-08-03.md`）

## 2026-07-30 — 审计与测试基线（继承自上游）

- 代码审计与首轮测试报告；ADR-001 检索配置对齐、ADR-002 移除章级摘要
