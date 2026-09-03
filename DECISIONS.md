# DECISIONS.md — 决策记录

> 记录"为什么选 A 而非 B"。修改相关模块前先查这里，**不要重新争论已定决策**；推翻旧决策时必须在本文留痕（新增条目说明替代关系）。

格式：`D-编号 | 状态 | 决策 | 备选与否决原因`

## 重构总纲（PRD 已确认，2026-08-12）

| # | 决策 | 否决的备选 | 原因 |
| :--- | :--- | :--- | :--- |
| D-R1 | 模型双后端：外接 API（DeepSeek）为主，Ollama 本地降级 | 纯本地 / 纯 API | 实时性需要强模型，降级保可用性（REQ-U1/UW2） |
| D-R2 | Agent 形态先工具调用（M1），预留多 Agent（M3） | 直接上多 Agent 规划 | 风险递进，先验证工具编排可靠性 |
| D-R3 | 通用搜索用 Tavily，Key 存宿主 `.env` 经 compose 注入 | Bing/Serper 等 | 价格与易用性；Key 后端统一管理 |
| D-R4 | 法律垂直源：官方免费库（国家法律法规数据库 + 人民法院案例库）为核心，小包公可选补充 | 只用第三方付费 API | 合规与成本；官方源权威性最高 |
| D-R5 | 双路冲突裁决：内部库优先；网络信息仅作线索，法律依据必须回源官方库二次验证 | 网络结果直接采信 | 幻觉与错误信息风险（REQ-U3/UW3/E4） |
| D-R6 | 部署：本地物理机/VM + Docker Compose，pgvector/Redis 容器化 + 宿主机持久化 | 云托管 K8s | 数据主权（《数据安全法》）、无 GPU 环境约束 |
| D-R7 | 预算熔断必须做：日上限 50 元，超阈值自动暂停（M3/F14） | 事后对账 | API 费用失控风险 |
| D-R8 | 前端过程透明化必须做：SSE 推送工具调用过程 | 只给最终答案 | 用户信任度（US-4，PRD P4） |

## M1 架构决策（2026-08-12/13，详见 `docs/M1-架构设计.md`）

| # | 决策 | 否决的备选 | 原因 |
| :--- | :--- | :--- | :--- |
| D-M1-1 | **不用** langchain `bind_tools`，自研 `LLMBackend.chat_with_tools()` + LangGraph 手动 StateGraph ReAct | langchain 原生 AgentExecutor | 双后端（OpenAI 兼容 + Ollama）统一抽象、对 DeepSeek V4 parallel_tool_calls 行为可控 |
| D-M1-2 | 模型用 `deepseek-v4-flash`（2026-07-24 deepseek-chat 已弃用） | deepseek-chat | 官方弃用；v4-flash 非思考模式，function calling 完整支持 |
| D-M1-3 | `FailoverLLMBackend`：4xx（认证/权限）→ 降级 Ollama；429/5xx/408 → 走 retry 不降级 | 一律重试 / 一律降级 | 4xx 重试无意义；429/5xx 是暂时性故障 |
| D-M1-4 | Ollama 降级后**退化为固定管线**（不支持工具编排） | Ollama 也跑 ReAct | 小模型 tool calling 不可靠，固定管线保底质量 |
| D-M1-5 | `AGENT_REACT_ENABLED=false` 回退旧固定管线图（AC-7） | 只保留新路径 | 向后兼容与故障逃生通道 |
| D-M1-6 | 空 name 的占位 tool_call 一律过滤（agent_node + 双 backend `_parse_tool_calls`） | 当作未知工具回灌错误 | DeepSeek V4 想直接回答时的固有行为，回灌错误会引发空转（Bug 2939ab3） |
| D-M1-7 | 项目迁移：新代码只写 LexAgent，Law-RAG-Agent 保持干净上游 | 在上游继续改 | 用户明确要求上游可随时对照/复用 |

## M2 决策（2026-08-27）

| # | 决策 | 否决的备选 | 原因 |
| :--- | :--- | :--- | :--- |
| D-M2-1 | 双路并行靠 LLM 的 parallel_tool_calls（一轮同时发 retrieve_knowledge + web_search），不做独立并行节点 | 图内固定并行扇出 | 保持 Agent 自主决策语义（PRD G1）；并行性由模型决策天然获得 |
| D-M2-2 | 融合在**最终 sources 组装阶段**做（`fusion.py`），不阻断 ReAct 循环 | 每轮工具结果即席融合 | 答案由 LLM 基于工具 summary 生成，融合价值在来源标注与冲突裁决（F10），即席融合增加循环复杂度 |
| D-M2-3 | 人民法院案例库无公开 API → 用 Tavily 域限定搜索（include_domains）发现案例线索，标注"官方域线索" | 爬虫采集 | PRD 风险清单 Q4 待定；域限定搜索零维护成本，合规 |
| D-M2-4 | 国家法律法规数据库走其公开 JSON 接口（flk.npc.gov.cn/api/search），失败时工具返回 ok=False 不阻断 | 失败自动回退 Tavily | 官方源不可用时应让 LLM 知道"验证不可用"，而非静默降级为未验证线索（REQ-UW1 语义） |
| D-M2-5 | 冲突裁决规则：web 结果提及内部库已有法名 → 标注 `web_unverified` 且内部条目排前；官方源命中 → `verified_official` | 复杂语义比对 | 法条级语义冲突检测需要 LLM 参与，M2 用来源级裁决满足 F8，语义级留给 validate 节点 |
| D-M2-6 | ~~SSE 流式路径状态合并用 `_merge_stream_update`~~ **已被 D-M3-1 取代并删除** | 直接 `state.update()` | 旧实现整体替换 messages 导致 DeepSeek 400 并降级 Ollama；补丁只治标（手工复刻框架行为），根治见 D-M3-1 |
| D-M2-7 | 轮数上限强制作答时注入 system 引导消息 + 兜底清除 DSML 工具调用语法 | 仅移除 tools 参数 | DeepSeek 在无 tools 参数时仍会以纯文本输出 DSML 工具调用语法，不引导/不清除则答案是一堆标记 |
| D-M2-8 | flk.npc.gov.cn 接口改版后走 `/law-search/search/list`（POST JSON），不回退旧 `/api/search` | 兼容旧端点 | 旧端点返回 405（Method Not Allowed），官方已废弃；新端点参数名/响应结构全变（`searchContent`/`rows`/`sxx` 等） |

## M3 决策（2026-08-28）

| # | 决策 | 否决的备选 | 原因 |
| :--- | :--- | :--- | :--- |
| D-M3-1 | SSE 流式路径走 **LangGraph 编译图执行**：新增 `_build_react_loop_graph()` 纯 ReAct 循环子图（入口 agent），`_stream_react` 用 `graph.stream(state, stream_mode=["updates","values"])` 驱动——updates 映射 SSE 事件、values 取终态；删除手写 `while` 循环与 `_merge_stream_update` | ① 保留手写循环 + 补丁合并；② 切 `create_react_agent` prebuilt | 手写循环绕开框架 reducer，是 D-M2-6 那个 400 Bug 的**根因**，补丁只治标。走编译图让 `add_messages` reducer 与条件边各司其职，Bug 从结构上不可能复发，代码更短。prebuilt 要求 `BaseChatModel` + LangChain Tool、需重写 LLM 层（D1 已否决），成本远高于收益 |
| D-M3-2 | 循环子图与完整管线图**并存**（`_react_loop_graph` 供 stream、`_graph` 供 ask），不强行统一 | 流式也走完整图（入口 intent） | 流式路径调图前已完成意图识别/FAQ 检查/记忆检索，且有 FAQ 命中提前 return 分支，走完整图会重复执行。两图共享同一套 `react` 节点字典（节点无状态、闭包注入依赖），无行为分叉风险 |
| D-M3-3 | 工具定义用自研 `@tool` 装饰器（`base.py`），从类型注解 + `Annotated` 元数据推导 schema | ① 保持 class + `build_spec()`；② 用 LangChain `@tool` | 装饰器让工具定义从 ~40 行 class 缩到 ~10 行函数，schema 与实现同源（改签名即改 schema，不会漂移）。**明确不用 LangChain `@tool`**：它产出的 LangChain Tool 对象只有 `BaseChatModel.bind_tools` 能消费，会倒逼重写 LLM 层（D1 已否决）。自研版零依赖、产出仍是与手写完全一致的 `ToolSpec`，下游 `ToolRegistry` / `chat_with_tools` 无需任何改动 |
| D-M3-4 | 融合截断给网络线索**保底配额**（`FUSION_WEB_MIN_SLOTS=2`）：网络先占 min(配额, 网络条数, top_k) 个位置，其余给权威来源，最终仍按分排序保证权威在前 | ① 纯按分截断（原实现）；② 提高网络权重 | 网络权重 0.5×tavily_score，天然低于内部库（≥0.5）与官方源（0.85），纯按分截断时 top_k 一满网络就**一条都不剩**——Tavily 调用成本花了，用户却完全看不到，「网络未验证」这一验证状态形同虚设。提高权重会违背「内部库优先」原则，让未验证内容挤掉权威内容；配额只保底不抢位，可用 `FUSION_WEB_MIN_SLOTS=0` 一键退回纯按分 |
| D-M3-5 | 非流式 `/api/chat` 与流式路径**共用融合结果**：优先取 `fused_sources`，融合不可用才回退 `retrieved_docs` | 保持非流式用原始 retrieved_docs | 此前非流式直接返回原始检索文档（实测 74 条、无去重、无 verification），与流式的 8 条融合结果行为分叉，前端拿不到验证状态。统一后两条路径口径一致 |
| D-M3-6 | 预算熔断按**逻辑调用次数**计数（埋点于 `LLMBackend` 公开入口），不按 token | token 口径（更贴近账单） | 流式响应拿不到 `usage`，token 只能按字符估算，误差不可控；次数口径跨后端一致、不受重试策略影响，且 Tavily 本就按次计费，两者口径统一。**实测一次复杂查询约 18~20 次 LLM 调用**，据此设默认阈值 5000（≈250 次查询/天） |
| D-M3-7 | 熔断分两级：LLM 超限**整体熔断**（API 前置拦截），Tavily 超限**局部降级**（工具返回 `ok=False`「搜索额度已用尽」，回答照常生成） | 两者都整体熔断 | LLM 是生成回答的必需品，超限后跑下去只会白烧钱；Tavily 仅是线索来源，内部库与官方源仍可作答——按 REQ-UW1「工具失败不阻断」语义降级，服务可用性损失最小 |
| D-M3-8 | 预算存储 **Redis 优先、不可用时退化进程内计数**，统计异常一律告警放行 | Redis 不可用时直接熔断/抛错 | 监控组件故障不应拖垮主链路（与「工具失败不抛异常」同一原则）。内存计数是单进程视角，精度下降但服务不断 |

## M3 决策（2026-08-29）

| # | 决策 | 否决的备选 | 原因 |
| :--- | :--- | :--- | :--- |
| D-M3-9 | （技术分析，仍然成立）F12 有两条路径：**路径 A** 进入图之前的前置确认——不接 `interrupt()`、不加 checkpointer、不改图结构，成本约 1/4；**路径 B** 在 ReAct 循环内用 `interrupt()` + `Command(resume=...)` 逐步骤确认——需 checkpointer、两张图改造、SSE 续流 | — | spike（`scripts/f12_spike_demo.py`，5 项验证全过）证明 interrupt 可行，但同时证明它**不是必需的**：确认点在图之前时图还没开始执行，没有任何状态需要保存恢复，一个「已确认」标记就够了；且恢复时虽是重跑而非续跑，但因确认发生在任何 LLM 调用之前，**零浪费** |
| D-M3-9a | **产品最终决策（2026-08-29）：F12 走路径 A 一次确认**（进入图之前的前置确认） | 路径 B 逐步骤确认（成本 3-4 倍） | 产品一度选逐步骤（希望用户看到中间结果再决策），了解实现代价后改回路径 A。**关键澄清：「一次确认」与「断线重连继续生成」是两个正交的需求**——前者是等人确认，后者是网络断了接着看；选路径 A **不会**丢掉重连能力，重连是 SSE 层的可靠性问题（见 D-M3-12），与 checkpointer / interrupt 无关。路径 B 的分析结论保留在 D-M3-9，作为将来若确实需要「看到中间结果再决策」时的备选 |
| D-M3-13 | 决定**迁移到 LangChain 标准生态**（`BaseChatModel` + `bind_tools`），**推翻 D-M1-1**（不用 bind_tools） | 维持自研 `chat_with_tools()` 抽象 | 战略目标从「最小可控」转为「**生态标准化 + 学习成本低**」（2026-08-29 产品决策）。可行性已验证：依赖解析无冲突（`langchain-openai` 1.6.0 + `langchain-ollama` 1.1.0，需 `langchain-core` ≥1.6.1 提供 `ModelAPIError`）；且项目本就半只脚在生态内——`src/embedding/embedder.py:24` 的 `LawEmbedder(Embeddings)` 已继承 LangChain 抽象。**分三阶段推进**：① LLM 层换 `BaseChatModel` ② 工具层换 LangChain `@tool`（替换 D-M3-3 自研装饰器）③ `create_react_agent` **暂不换**——收益最小且会失去 ReAct 循环控制权（轮数上限强制作答 REQ-UW4、空 tool_call 过滤 D-M1-6 都要重新适配）。> **状态（2026-08-29）：阶段① ② 已完成** —— 638 测试全绿、ruff 干净；③ 按计划不做。
> 依赖安装卡点已绕过：本机 Windows 安全策略会拦截文件删除，导致 `uv add` / `uv sync` / `uv lock`
> 在重建 jieba（该包只有 sdist、无 wheel）时静默失败；解法是 `uv pip install` 装包 + 手动清理
> 残留的旧 dist-info，随后一律用 `uv run --no-sync` 跳过环境校验。**注意 `uv.lock` 尚未更新**，
> 需在能正常 lock 的环境补做（详见 2026-08-29 记忆）。
>
> 迁移覆盖 D-M3-3 的一部分：D-M3-3 的自研 `@tool` **装饰器语法保留不变**，
> 只是内部不再自己推导 schema，改为委托 LangChain 的 `@tool`（D-M3-3 的"不用 LangChain @tool"
> 结论因此失效——它当时成立的前提是"会用倒逼重写 LLM 层"，而 LLM 层本次一并迁移了）。
>
> **迁移后确认的行为差异**（均已实测，测试已同步）：
> 1. `str | None` → `anyOf [string, null]`（原扁平 string），语义等价且更规范
> 2. schema 会带上 `default`（原不写），对 LLM 是有益引导
> 3. dataclass 类型 → 展开为对象 schema（原降级 string）
> 4. ⚠️ 枚举必须用 `Literal` 表达：历史上的 `Param` 类 LangChain **不认识，会静默丢弃**
>    其中的 description 与 enum（不报错，只是发给模型的 schema 少了引导信息，极难发现） |
| D-M3-12 |（**已实现 2026-08-30**）**断线重连继续生成**用 **Redis 事件日志 + seq 游标重放**，**不接 checkpointer**：每个 SSE 事件写入 Redis List（`stream:{request_id}:events`，带 TTL），被动断线后生成任务**继续跑完**并持续写入；重连带 `after_seq` 先重放后订阅新事件。**主动 cancel 仍立即停** | ① 接 checkpointer 保存图状态后从断点续跑；② 断开即停、重连时重新生成 | checkpointer 保存的是**图状态**，解决不了「已流式输出的文本怎么补给重连的用户」——重连要补的是 SSE 事件流，不是图状态。把这两件事混为一谈就会误以为「要重连就得上 checkpointer」，这是本次最大的认知纠偏。Redis 事件日志天然支持重放与游标，且**零新增依赖**（Redis 已在用）。区分主动 cancel（立即停，省 Token）与被动断线（继续跑完，可重连）与现有 `/chat/cancel` 机制天然契合：`/chat/cancel` 走立即停，`http.disconnect` 走宽限期继续跑完 |
| D-M3-10 | F11 场景清单做成**配置数据**：`src/rag/scenes.py` 中 `SCENES` 元组是数据（id/名称/A\|B/关键词/工具），`classify_scene()` 是逻辑；打分用三级权重（正则 3.0 > 强特征词 2.0 > 普通关键词 1.0） | ① 场景判定写死在代码分支里；② 接 LLM 做场景分类 | 配置化让「需产品定场景清单」从阻塞项降级为配置项——产品后续改清单只改 `SCENES`，不动任何函数。三级权重是必需的：中文查询里「合同」这类通用词会同时命中起草/审查/检索多个场景，必须让「起草」「审查」等强特征词和「第X条」正则压过通用词（单测 `test_law_article_query_not_mistaken_for_contract_scene` 守着这条）。LLM 分类要额外一次调用且不可预测，纯字符串匹配零延迟零成本、结果可解释 |
| D-M3-11 | F13 本期**只保留 `sub_agent` state 字段**，不建规划节点扩展位 | 按 PRD 原文建「规划节点扩展位」 | 项目目前没有任何具体多 Agent 需求，此时设计的扩展位大概率在未来被推翻（典型过度设计）。F13 号称工作量最小，但最小是因为价值也最小，省下的工作量给 F12。已在 `state.py` 注释与本文档记录重新立项的触发条件（出现第二个 Agent 角色 / `AGENT_MAX_TOOL_TURNS` 成瓶颈 / 需并行子任务编排） |

## M3 决策（2026-08-30）

| # | 决策 | 否决的备选 | 原因 |
| :--- | :--- | :--- | :--- |
| D-M3-14 | ReAct 决策调用（`agent_node`）**必须经 `LLMBackend.chat_with_tools()` 公开入口**，不得直接 `chat_model.bind_tools().invoke()` | 直接 `chat_model.invoke()`（D-M3-13 迁移时的写法） | 重试（D-M1-3 的 `is_retryable`+`wait_and_log`）与 FailoverLLMBackend 的 4xx 运行期降级**都实现于 `chat_with_tools` 入口链路**——迁移时绕过该入口，两层语义在 ReAct 主路径同时静默失效（瞬时抖动一次即整轮失败、主后端 4xx 不再切 Ollama），测试全绿所以没被发现（FakeToolLLM 两条路径等价）。修复后后端内部仍是 bind_tools+invoke（D-M3-13 成果不变），预算 callback 挂在 ChatModel 上不受影响。**教训：LangChain 标准化迁移时，"标准写法"不能替代项目自建的横切语义层；凡绕过公开入口的写法都要逐项核对该入口承载的横切职责**（本例：重试、降级、预算三件事里预算挂在 model 上幸存，另两个丢了） |

## M4 路线图决策（2026-08-30）

| # | 决策 | 否决的备选 | 原因 |
| :--- | :--- | :--- | :--- |
| D-M4-1 | **多 Agent 演进立项**：D-M3-11 的重启条件（出现第二个 Agent 角色）因产品明确「多 Agent 多场景、意图识别中的规划」方向而正式满足。路线 = **生长式迁移**：新 Agent 用生态标准件长成编译子图，主 Agent 自有循环不重写；**按场景拆出口，不按工具拆内脏**（检索/搜索/核验保持为工具，拆出去的是重流程场景）。阶段 0=M3 收尾 → 1=plan 对象+场景白名单 → 2=审核子图+类案子图 → 3=supervisor 定型（状态作用域/per-agent 预算/checkpointer）。详见 `docs/M4-多Agent路线图.md` | ① 全盘 LangChain 风格化重写；② 按工具拆子 Agent（检索 Agent/搜索 Agent…）；③ 大爆炸重写主 Agent | ① 生态收益已在 D-M3-13 兑现，剩余自有抽象承载产品语义（融合/SSE/预算/降级），重写零新增收益且 D-M3-14 实证迁移最易伤横切语义；② 跨源迭代验证是主 Agent 的核心智能，按工具拆是在 orchestrator 层重建同一个循环还多付每跳路由成本（实测 18~20 次调用/查询，朴素编排砍 15~20% 容量，A 类必须零编排）；③ 同 ①。**前置**：eval_answer_quality 基线先于阶段 2 存在，检索质量线（法名推断）并行推进 |

## 代码审查整改决策（2026-09-01/02）

> 背景：全库静态走查 + 实证验证（`docs/代码审查报告-2026-09-01.md`）。安全与资源管理类问题整改已全部落地（07-01 高危、07-02 中低危），此表只记录**改变或补强既有设计语义**的决策；纯修复项（鉴权补齐/连接池化/日志脱敏/强引用）见 CHANGELOG。

| # | 决策 | 与既有决策的关系 | 原因 |
| :--- | :--- | :--- | :--- |
| D-0902-1 | 重试耗尽（持续 429/5xx/网络）**也触发降级**：`LLMRetryExhaustedError` 哨兵异常携带最后一次失败的状态码（retry.py），failover 把哨兵视为主后端不可用 | 补强 D-M1-3（原只降级 4xx；重试耗尽抛裸 RuntimeError 无状态码被判定「非 4xx」→ 不降级，Ollama 兜底用不上） | 重试耗尽说明主后端在**当前窗口内确实不可用**，有备用就该用上；裸 RuntimeError（编程错误）仍不降级，避免把内部 bug 误判成后端故障 |
| D-0902-2 | 降级**必须可回切**：降级后进入冷却窗口（默认 300s，`recovery_cooldown_seconds`=0 禁用），冷却结束后下一次**真实请求**兼作健康探测——成功回切、失败继续降级并刷新冷却 | 补强 D-M1-3 / D-M1-4 / D-M4-1（原降级单向永久，一次瞬时 401/403 只能重启恢复） | ① 为探测单独发 ping 要付真实 Token + 一次 RTT，复用下一次真实请求零成本，探测失败时请求仍由备用端应答、用户无感；② 自动回切是「降级可观测化」「健康探测自动回切」长期项的第一段，M4 多 Agent 需要稳定的后端健康语义 |
| D-0902-3 | `_react_enabled` 由构造期固化改**动态属性**：能力（AGENT_REACT_ENABLED + 支持工具）构造期定，「当前是否降级」每次访问实时求值；ReAct 图与固定管线图在具备能力时都预构建，运行时按状态切换 | 补强 D-M1-4 / D-M1-5 / AC-7（原：降级状态构造期固化 → 主后端恢复后 ReAct 能力也回不来） | 与 D-0902-2 配套：降级回落固定管线、冷却回切后同一实例自动拿回 ReAct 能力；固定管线图始终保留 = 故障逃生通道不变 |
| D-0902-4 | 断线重连接口做**归属校验**：`/chat/stream` 软鉴权取 user_id 登记流创建者（`StreamEventLog.set_owner`，TTL 与事件日志一致），`/chat/stream/resume` 校验「请求者 == 创建者」，不匹配 403；无归属登记的旧流放行 | 补强 D-M3-12（原 resume 已硬鉴权但未绑定到流创建者，登录用户仍可拿别人 request_id 重放） | request_id 是 uuid4 前 8 位，可枚举性尚可但并非机密；resume 重放的是「提问与回答全文」，登录用户之间也要隔离。归属登记失败只告警不阻断（日志故障不拖垮主链路，D-M3-8 同款） |
| D-0902-5 | 工具执行前必须经 pydantic 校验：`registry.execute()` 对带 `langchain_tool` 的工具先经 `tool_call_schema`（与发模型的同一份约束）`model_validate` 再调 executor | 补强 D-M3-3/D-M3-13 工具层（原 schema 只在发模型时生效，运行时不强制；工具靠内部自发容错） | 工具参数是 LLM 生成的，等同不可信输入。**不走 `langchain_tool.invoke()`**：`BaseTool.run` 会把非字符串返回值 str() 化，executor 返回的 ToolResult 会被拍平——校验用 schema、执行仍走原 executor |
| D-0902-6 | **删除 `src/llm/client.py`**（392 行死代码）：`Message` 迁入 `src/llm/base.py`，`LawLLM`/`LLMConfig`/`create_llm` 全删；engine 构造注解 `LawLLM` 改 `LLMBackend`；评测脚本 judge 层迁 `create_llm_backend` | 在 D-M3-13 后补完（client.py 自建 ollama.Client，与 D-M3-13「LLM 层走 LangChain 生态」重复且**无预算埋点、无 Failover**，误用即同时绕过 F14 与降级链路） | 留着是随时可能被误用的「绕过横切语义」入口（D-M3-14 教训同源）；生产路径真正在用的只有 `Message` 一个数据类，迁走后引用清零 |
| D-0902-7 | docker-compose 数据库密码**必填化 + 移除 5432 宿主映射** | 修正 D-R6（原 `lawrag123` 明文写死在 compose） | 弱口令 + 端口暴露 = 局域网内可直连无防护 PG。compose 用 `${POSTGRES_PASSWORD:?required}` 无默认值（未设置拒绝启动），db 只在 compose 网络内被 app 访问 |

## 代码审查整改决策（2026-09-03，中长期项收尾）

> 背景：`docs/代码审查报告-2026-09-01.md` 中期/长期清单收尾（预算 TOCTOU / 连接池 / 降级可观测 / Token Cookie / 前端性能）。纯实现项（rAF 节流、会话增量 append、markdown-it 渲染器、Vite 升级、CI 前端 job）见 CHANGELOG，此表只记录**改变或补强既有设计语义**的决策。

| # | 决策 | 与既有决策的关系 | 原因 |
| :--- | :--- | :--- | :--- |
| D-0903-1 | 预算计数**原子预占**：`reserve()`/`release()`/`check_and_reserve()` 取代「只读 check + 事后 record」，Redis 走 Lua 脚本（INCRBY→比较→超限回滚），失败归还；`check()` 保留只读语义仅供入口前置拦截 | 补强 D-M3-6/7/8（原 check 与 record 两次往返存在 TOCTOU，并发 N 流把日限额放大到 limit+N-1） | ① 拦截点必须移到**花钱之前**——record 时才发现超限，钱已花出，抛异常截断生成中的回答或默默突破限额两头都不对；② Lua 失败退化**非原子 Redis 路径**而非进程内计数：reserve 写内存、used() 读 Redis 会两边不一致让熔断彻底失效，宁失并发安全不失存储一致性；③ enforce=false 观察期照实计数不拦截 |
| D-0903-2 | DB 连接统一走**进程级连接池**（`src/db/pool.py`）：ThreadedConnectionPool 惰性创建 + fail-open 退化直连；6 个 store 类（pgvector_store/conversation_store/faq_cache/memory.conversation/query_log/retriever）全部改「**每操作借用**」，删光单连接 + threading.Lock + 手写重连 | 推翻 store 层隐含的「常驻单连接 + 锁串行化」模型（D-R6 部署形态下 psycopg2 连接非线程安全，此前用锁把所有 DB 操作串行化） | 锁让并发读写互相排队——检索热路径也被锁住，是审查点名的最大并发瓶颈。借用模型：并发各用各的连接互不阻塞、失效连接由池回收、无需手写重连。**conn 必须是方法内局部变量**——挂实例上会在并发时被互相覆盖（那正是旧锁要防的坑）。建表 DDL 惰性执行一次（加锁幂等） |
| D-0903-3 | `VectorAwarePool` **子类化** ThreadedConnectionPool 覆写 `_connect()`，池内每条连接自动 register_vector | 修正 D-0903-2 前置（组合包装裸池时 register_vector 只覆盖直连兜底路径，池自行创建（minconn + 惰性扩展）的连接不注册） | pgvector 适配是按连接生效的；池连接不注册时向量 store 检索 embedding 以字符串返回——fake 测试静默通过、真实 PG 上类型错乱。psycopg2.pool 的 `_connect(key=None)` 是所有连接的唯一起点 |
| D-0903-4 | `/api/health` 暴露降级态：`degraded`/`degraded_reason`/`active_backend`/`budget_exceeded`；FailoverLLMBackend 记录降级原因（创建期/运行期/回切清空） | 补强 D-0902-2（回切已实现但「是否在降级」无任何对外信号，运维不知道服务跑在 Ollama 上） | 降级可观测是运维前提；取值一律 fail-open——健康检查挂掉会被负载均衡摘掉所有实例，信息缺失远好于 503 |
| D-0903-5 | Token 鉴权从 localStorage **迁移 HttpOnly Cookie**：login/register 同时 Set-Cookie（HttpOnly + SameSite=Strict，30 天，Secure 由 `COOKIE_SECURE` 控制）；`get_current_user` 先 Cookie 后 Bearer（兼容通道保留）；新增无鉴权幂等 `POST /auth/logout` 清 Cookie | 推翻「Token 明文存 localStorage」的前端实现（前端 api/index.js 与 auth store 各读一次，任何 XSS 可直接窃取） | ① HttpOnly = JS 不可读，XSS 拿不到凭据；② SameSite=Strict 跨站不带 Cookie → CSRF 基本失效；③ Bearer 保留供 CLI/curl/第三方（响应体仍回 token）；④ HttpOnly Cookie 无法由 JS 删除，必须有服务端登出端点，且登出不能要求先登录（凭据失效后也要能清残留） |
