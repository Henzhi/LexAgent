<template>
  <div class="chat-layout">
    <!-- Sidebar -->
    <Sidebar
      :sessions="sessions"
      :active-id="chat.sessionId"
      :open="sidebarOpen"
      :username="auth.username"
      @new-chat="handleNewChat"
      @select="handleSelect"
      @toggle="sidebarOpen = !sidebarOpen"
      @delete="handleDelete"
      @logout="doLogout"
    />

    <!-- Main Area -->
    <div class="main-area">
      <!-- 顶部工具栏（精简） -->
      <header class="header">
        <div class="header-left">
          <svg class="logo-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
          <span class="app-name">法智问答</span>
        </div>
        <div class="header-right">
          <router-link to="/crawl" class="nav-link">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15"><path d="M21 12a9 9 0 1 1-9-9"/><path d="M21 3v6h-6"/></svg>
            在线更新
          </router-link>
          <router-link to="/knowledge" class="nav-link">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
            知识库
          </router-link>
          <router-link to="/usage" class="nav-link">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15"><path d="M21 12a9 9 0 1 1-9-9"/><path d="M9 12l2 2 4-4"/><path d="M21 3v6h-6"/></svg>
            用量计费
          </router-link>
        </div>
      </header>

      <main class="messages" ref="messagesEl">
        <!-- 空状态：示例问题卡片 -->
        <div v-if="chat.messages.length === 0 && !chat.sending" class="welcome">
          <div class="welcome-logo">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" width="40" height="40"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
          </div>
          <h2 class="welcome-title">法律智能问答</h2>
          <p class="welcome-sub">基于中国法律法规知识库，为你提供专业、准确的解答</p>

          <div class="suggest-grid">
            <button v-for="(q, i) in suggestions" :key="i" class="suggest-card" @click="handleSend(q)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
              <span>{{ q }}</span>
            </button>
          </div>

          <p class="welcome-disclaimer">回答仅供参考，不构成专业法律意见。涉及具体法律事务，请咨询执业律师。</p>
        </div>

        <template v-for="(m, i) in chat.messages" :key="i">
          <ChatMessage :message="m" :sources="m.sources || []" />
          <!-- 思考过程：跟在最后一个用户消息后面、答案前面 -->
          <div
            v-if="m.role === 'user' && i === lastUserMsgIndex && chat.sending && !rewriteState.open && !rewriteState.loading"
            class="thinking-box"
          >
            <button class="thinking-toggle" @click="thinkingOpen = !thinkingOpen">
              <svg :class="{ rotated: thinkingOpen }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="9 18 15 12 9 6"/></svg>
              <span>{{ thinkingOpen ? '收起思考过程' : (answered ? '已思考' : '思考中...') }}</span>
              <span v-if="!answered && chat.sending" class="spinner"></span>
            </button>
            <div v-if="thinkingOpen" class="thinking-traces">
              <div v-for="(t, i) in thinkingTraces" :key="i" class="trace-item" :class="`trace-${t?.kind || 'thinking'}`" style="white-space:pre-wrap">{{ t?.text ?? t }}</div>
            </div>
          </div>
        </template>
      </main>

      <!-- 智能改写确认（人机协作）：开启改写后每次发送都先展示改写结果，确认后才进入检索 -->
      <div v-if="rewriteState.open || rewriteState.loading" class="rewrite-confirm">
        <div class="rewrite-card">
          <div class="rewrite-head">
            <span class="rewrite-badge">智能改写 · 案情分析</span>
            <span class="rewrite-hint">系统已将您的提问改写为规范法律检索表述，请确认后再检索</span>
          </div>

          <!-- 加载中 -->
          <div v-if="rewriteState.loading" class="rewrite-loading">
            <span class="spinner"></span> 正在分析案情、生成规范检索表述…
          </div>

          <!-- 已改写：可编辑 + 需勾选确认 -->
          <template v-else-if="rewriteState.changed">
            <textarea v-model="rewriteState.proposed" class="rewrite-input" rows="2" @keydown.enter.prevent></textarea>
            <label class="rewrite-check">
              <input type="checkbox" v-model="rewriteState.acknowledged" />
              <span>改写结果符合我的案情</span>
            </label>
            <div class="rewrite-actions">
              <button class="btn-confirm" :disabled="!rewriteState.acknowledged" @click="confirmRewrite">确认并使用改写</button>
              <button class="btn-original" @click="useOriginal">改用原句（保持精确）</button>
            </div>
          </template>

          <!-- 未改写：原句已规范，确认即可检索 -->
          <template v-else>
            <div class="rewrite-note">系统判断原句已足够规范，将直接按原句检索：</div>
            <div class="rewrite-orig">{{ rewriteState.original }}</div>
            <div class="rewrite-actions">
              <button class="btn-confirm" @click="useOriginal">确认检索</button>
            </div>
          </template>
        </div>
      </div>

      <!-- F12 人工确认（B 类场景）：确认后重新发起生成，取消则结束本次请求 -->
      <div v-if="confirmState.open" class="rewrite-confirm">
        <div class="rewrite-card">
          <div class="rewrite-head">
            <span class="rewrite-badge">人工确认 · {{ confirmState.sceneName }}</span>
            <span class="rewrite-hint">该场景会基于您的描述生成正式内容，请确认执行范围后再继续</span>
          </div>
          <div class="rewrite-note">{{ confirmState.prompt }}</div>
          <div class="rewrite-actions">
            <button class="btn-confirm" @click="confirmProceed">确认执行</button>
            <button class="btn-original" @click="confirmCancel">取消</button>
          </div>
        </div>
      </div>

      <!-- 输入区：底部常驻 + 智能改写开关 -->
      <div class="input-wrap">
        <div class="rewrite-switch">
          <label class="switch" :class="{ on: rewriteEnabled }" title="开启后，发送的提问会先被改写为规范法律检索表述并请您确认；关闭则保持原句、绝对精确">
            <input type="checkbox" v-model="rewriteEnabled" />
            <span class="switch-text">智能改写{{ rewriteEnabled ? ' · 开' : ' · 关' }}</span>
          </label>
        </div>
        <ChatInput :disabled="chat.sending" @send="handleSend" @stop="stopGeneration" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useChatStore } from '../stores/chat'
import {
  loadHistory, listConversations, saveSession, streamChat, resumeChat, rewriteQuery,
  deleteConversation, cancelChat, confirmScene, confirmSceneStream,
} from '../api'
import Sidebar from '../components/Sidebar.vue'
import ChatMessage from '../components/ChatMessage.vue'
import ChatInput from '../components/ChatInput.vue'

const router = useRouter()
const auth = useAuthStore()
const chat = useChatStore()
const messagesEl = ref(null)
const sidebarOpen = ref(true)
const sessions = ref([])
const thinkingTraces = ref([])
const thinkingOpen = ref(true)
const answered = ref(false)
// 当前生成请求的取消控制器：点击"停止"时 abort，后端会收到断开并停止消耗 Token
const abortController = ref(null)
// 当前生成请求的唯一 ID：停止时通过 /chat/cancel 主动通知后端中断（覆盖代理场景）
const currentRequestId = ref('')
// 组件是否挂载中（2026-09-03 切页保活）：路由切换不再 abort 掉 SSE，生成在后台
// 继续跑并写回 store；卸载期间只跳过"组件局部状态/DOM"类更新，store 与落库不受影响。
const viewActive = ref(true)

// 示例问题（空状态展示，点击直接提问）
const suggestions = [
  '劳动合同到期不续签，公司需要支付经济补偿金吗？',
  '工伤认定的申请时限是多久？逾期会有什么后果？',
  '离婚时夫妻共同财产一般如何分割？',
  '买到假货如何维权？适用《消费者权益保护法》的哪些规定？',
  '公司违法解除劳动合同，员工可以主张什么赔偿？',
  '民间借贷的利息上限是多少？超过部分受法律保护吗？',
]

// 智能改写（案情分析）开关：开启后复杂查询会被改写为规范法言法语并需用户确认
const rewriteEnabled = ref(localStorage.getItem('lawrag_rewrite') === '1')
const rewriteState = ref({ open: false, loading: false, original: '', recent: [], proposed: '', acknowledged: false, changed: false })
watch(rewriteEnabled, v => localStorage.setItem('lawrag_rewrite', v ? '1' : '0'))

// F12 人工确认（B 类场景：合同起草/审查、文书生成等）：
// 后端产出 confirmation_required 事件并结束流；用户点"确认执行"后在同一
// SSE 连接上直接续跑生成（2026-09-03，无需重新发起 stream）。
// recent 记录本提问之前的轮次，供续跑请求携带（不能把当前问题再当历史）。
const confirmState = ref({ open: false, scene: '', sceneName: '', prompt: '', confirmId: '', query: '', recent: [], sid: '' })

// 确认执行：直接在同一 SSE 连接上续跑生成（2026-09-03 交互优化 —— 不再像 v1 那样
// 确认后还要"重新发送一次请求"）。服务端已写入标记，旧逻辑/其他入口重发 stream 仍兼容。
async function confirmProceed() {
  const st = confirmState.value
  const sid = st.sid || chat.sessionId
  const sceneId = st.scene
  const confirmId = st.confirmId
  confirmState.value = { ...st, open: false }
  // 与正常提问一致的进行中状态：输入区禁用、思考区展开、可点"停止"
  chat.sending = true
  answered.value = false
  thinkingTraces.value = []
  thinkingOpen.value = true
  abortController.value = new AbortController()
  currentRequestId.value = `req_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`
  // 历史取确认卡缓存的本提问之前的轮次（recent），不要带上当前问题本身
  await confirmContinue(st.query, st.recent || [], sid, sceneId, confirmId)
}

async function confirmCancel() {
  const st = confirmState.value
  const sid = st.sid || chat.sessionId
  confirmState.value = { ...st, open: false }
  try {
    await confirmScene(sid, st.scene, st.query, false, st.confirmId)
  } catch { /* 取消标记失败可忽略 */ }
  if (chat.sessionId === sid) chat.messages.push({ role: 'assistant', content: '已取消该操作。需要时请重新发起。' })
}

// 找到最后一个 user 消息的索引，思考过程跟在这个消息后面
const lastUserMsgIndex = computed(() => {
  for (let i = chat.messages.length - 1; i >= 0; i--) {
    if (chat.messages[i].role === 'user') return i
  }
  return -1
})

// Redirect if not authenticated
onMounted(async () => {
  if (!auth.isAuthenticated) {
    router.replace('/login')
    return
  }
  viewActive.value = true
  await refreshSessions()
  await loadCurrentSession()
  // 刷新 / 切页回来：本会话若留有未完成的生成，按服务端事件游标续流，
  // 把断线期间后端已经产出（且仍会继续产出）的内容接回来。
  await resumeIfPending()
})

async function refreshSessions() {
  try {
    const data = await listConversations()
    sessions.value = data || []
  } catch { /* ignore */ }
}

async function loadCurrentSession() {
  const sid = chat.sessionId
  // 切页保活：本会话仍有生成在进行（SSE 未被路由切换中断，store 已含最新流式
  // 内容）→ 不重拉历史覆盖正在增长的答案，只恢复组件局部状态（思考轨迹）。
  if (chat.activeStream && chat.activeStream.sid === sid) {
    // 刷新后消息区是空的（Pinia 重建），先把历史拉回来补上"用户提问"，
    // 否则续流回填的回答会孤零零悬在那里看不到问题。
    if (chat.messages.length === 0) {
      try {
        const data = await loadHistory(sid)
        if (data.history?.length) {
          chat.messages = data.history
          chat.setBaseline(sid, chat.messages.length)
        }
      } catch { /* 历史拉取失败不阻断续流，继续回填进行中的回答 */ }
    }
    // 直接绑定 store 里进行中的同一响应式数组（勿用展开拷贝）：切回页面后，
    // 后台线程仍在向 activeStream.traces 实时追加，绑同一引用才能持续渲染。
    thinkingTraces.value = chat.activeStream.traces || []
    thinkingOpen.value = true
    // 刷新后内存消息已随 Pinia 重建而清空 —— 用 sessionStorage 快照把进行中的
    // 回答先回填出来（否则用户看到的是"只剩提问、回答空白"），随后由
    // resumeIfPending 按游标续上剩余部分。
    const prog = chat.streamProgress()
    if (prog.answer) {
      const last = chat.messages[chat.messages.length - 1]
      const draft = {
        role: 'assistant',
        content: prog.answer,
        sources: prog.sources || [],
        thinking: [...(chat.activeStream.traces || [])],
      }
      if (last?.role === 'assistant') chat.messages[chat.messages.length - 1] = draft
      else chat.messages.push(draft)
      answered.value = true
      thinkingOpen.value = false
    }
    chat.sending = true  // 续流期间保持"生成中"状态（输入禁用 + 可停止）
    await nextTick()
    scrollBottom()
    return
  }
  try {
    const data = await loadHistory(sid)
    if (data.history?.length) {
      // 本地已有更新内容（如刚在后台跑完、服务端持久化还在排队）→ 保留本地，
      // 基线对齐服务端数量，避免误全量覆盖或与排队保存竞争。
      const localLast = chat.messages[chat.messages.length - 1]
      const localAhead =
        chat.messages.length > data.history.length &&
        localLast?.role === 'assistant' &&
        !!localLast?.content
      if (localAhead) {
        chat.setBaseline(sid, data.history.length)
        thinkingOpen.value = false
        answered.value = true
        await nextTick()
        scrollBottom()
        return
      }
      chat.messages = data.history
      // 增量保存基线：历史已存于服务端，之后只追加新消息
      chat.setBaseline(sid, chat.messages.length)
      // 从最后一条 assistant 消息中恢复 thinkingTraces
      const lastMsg = chat.messages[chat.messages.length - 1]
      if (lastMsg?.role === 'assistant' && lastMsg.thinking?.length) {
        // 兼容旧版字符串格式，归一化为 { text, kind } 结构
        thinkingTraces.value = lastMsg.thinking.map((t) =>
          typeof t === 'string' ? { text: t, kind: 'thinking' } : t
        )
        thinkingOpen.value = true  // 加载历史时保持思考过程可见
      }
      answered.value = true
      await nextTick()
      scrollBottom()
    } else {
      chat.resetBaseline(sid)
    }
  } catch {
    // 历史加载失败：基线归零，下轮保存走全量（与旧行为一致）
    chat.resetBaseline(sid)
  }
}

function scrollBottom() {
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}

// 流式渲染节流（2026-09-03 审查整改）：此前每个 token 都 await nextTick() +
// 读 scrollHeight（强制同步布局），长回答下 DOM patch 与重排次数 = token 数。
// 改为 requestAnimationFrame 合并——同一帧内到达的多个 token 只做一次滚动 +
// 一次布局，读 scrollHeight 的频率上限 = 刷新率而非 token 数。
let scrollScheduled = false
function scheduleScroll() {
  if (scrollScheduled) return
  scrollScheduled = true
  requestAnimationFrame(() => {
    scrollScheduled = false
    scrollBottom()
  })
}

// 会话增量保存（2026-09-03 修复「切换对话丢消息/串会话」；基线/串行队列已迁入
// chat store，见 stores/chat.js —— 路由切页卸载组件后仍存活，避免重挂载时基线
// 丢失导致误走全量 replace 或与服务端排队保存竞争重复追加）：
// - 基线按会话独立计数（chat.baselineOf(sid)），互不覆盖；
// - persistSession(sid) 在调用瞬间就快照 body 并固定 sid，链上执行不再读
//   全局 chat.sessionId / chat.messages——切换会话不影响已排队的保存；
// - 显式增量（extraMsgs）：回答完成/中断时把本条 assistant 消息精确追加，
//   即使视图已切走也能写回正确会话；
// - 保存失败不推进基线，下次调用自动重传该段（宁可多传不丢消息）；
// - enqueueSave 串行化：上一轮保存未完成时下一轮排队，避免并发追加重复。
function persistSession(sid, extraMsgs = null) {
  const baseline = chat.baselineOf(sid)
  let body
  let mode
  if (extraMsgs && extraMsgs.length) {
    // 显式增量：只传本条新增（assistant 消息），视图已切走也能精确追加回原会话
    body = extraMsgs
    mode = 'append'
  } else {
    // 全量/增量快照：调用瞬间切片并固定 sid
    if (chat.sessionId !== sid) return Promise.resolve() // 视图已切走且无显式增量 → 无可保存
    const total = chat.messages.length
    if (total === 0 || total <= baseline) return Promise.resolve()
    body = baseline === 0 ? [...chat.messages] : chat.messages.slice(baseline)
    mode = baseline === 0 ? 'replace' : 'append'
  }
  return chat.enqueueSave(() =>
    saveSession(sid, body, mode).then((res) => {
      const total = res && typeof res.total === 'number' ? res.total : baseline + body.length
      chat.setBaseline(sid, total)
    })
  )
}

async function handleSend(query) {
  const sid = chat.sessionId  // 固化本请求所属会话：期间切换/新建不影响收尾写回
  chat.sending = true
  answered.value = false
  thinkingTraces.value = []
  thinkingOpen.value = true
  abortController.value = new AbortController()
  currentRequestId.value = `req_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`

  const recent = chat.messages.slice(-20)  // 不含当前提问
  chat.messages.push({ role: 'user', content: query })
  await nextTick()
  scrollBottom()

  // 问题发出即建立会话：先把已发出的用户提问落库并刷新左侧列表，
  // 让新对话在提问瞬间就出现在侧边栏，而不是等回答回来才刷新出现
  // （满足"提问即建对话"预期；B 类确认场景也会先建空壳，确认后由
  // runStream 结尾的 persistSession 补上答案）。
  await persistSession(sid)
  await refreshSessions()

  if (rewriteEnabled.value) {
    await doRewrite(query, recent, sid)
  } else {
    await runStream(query, recent, sid)
  }
}

// 用户点击"停止"：中止 fetch（浏览器断开连接）+ 主动通知后端中断 LLM 流
function stopGeneration() {
  abortController.value?.abort()
  if (currentRequestId.value) {
    cancelChat(currentRequestId.value)
    // 明确停止即清掉续流快照：这一轮不再需要重连续上
    chat.endStream(chat.sessionId, currentRequestId.value)
  }
}

// 智能改写流程：开启开关后，每次发送都先调用 /api/rewrite，弹出确认卡等待用户确认。
// 无论模型是否改动，都给出可见的人机协作步骤（案情分析模式必须确认）。
async function doRewrite(original, recent, sid) {
  rewriteState.value = { open: false, loading: true, original, recent, proposed: original, acknowledged: false, changed: false, sid }
  try {
    const res = await rewriteQuery(original)
    // 改写期间用户切走了会话：放弃弹卡，也不在错误会话继续生成
    if (chat.sessionId !== sid) return
    const proposed = (res.proposed_query || original).trim()
    const changed = proposed !== original.trim() && !res.skipped
    rewriteState.value = { open: true, loading: false, original, recent, proposed, acknowledged: false, changed, sid }
    // 等待用户确认（使用改写）/ 改用原句
  } catch (e) {
    // 改写失败不阻塞用户，降级为原句并直接检索（仍在原会话才继续）
    if (chat.sessionId !== sid) return
    await runStream(original, recent, sid)
  }
}

// 用户确认改写结果（已勾选"符合案情"）→ 用改写后的查询检索
async function confirmRewrite() {
  const finalQuery = (rewriteState.value.proposed || '').trim() || rewriteState.value.original
  const recent = rewriteState.value.recent || chat.messages.slice(-20)
  const sid = rewriteState.value.sid || chat.sessionId
  rewriteState.value = { ...rewriteState.value, open: false, loading: false }
  await runStream(finalQuery, recent, sid)
}

// 改用原句 / 未改写时确认检索（保证绝对精确）
async function useOriginal() {
  const original = rewriteState.value.original
  const recent = rewriteState.value.recent || chat.messages.slice(-20)
  const sid = rewriteState.value.sid || chat.sessionId
  rewriteState.value = { ...rewriteState.value, open: false, loading: false }
  await runStream(original, recent, sid)
}

// 普通提问流（含智能改写后的检索）：入口与 handleSend 匹配，走统一生成引擎
async function runStream(query, recent, sid) {
  await consumeGeneration(query, recent, sid, null)
}

// F12 确认续跑（2026-09-03）：确认后在同一 SSE 连接直接生成，不再二次发起 stream
async function confirmContinue(query, recent, sid, scene, confirmId) {
  await consumeGeneration(query, recent, sid, { scene, confirmId })
}

// 统一生成引擎（普通提问与"确认后续跑"共用）：
// - 2026-09-03 切页保活：路由切走不再 abort —— token/工具事件持续写回 store（消息、
//   落库、思考轨迹镜像），组件卸载期间只跳过"组件局部/DOM"更新；切回时 onMounted
//   从 store 恢复现场，不会出现"切走被掐断、回来又全部冒出来"。
// - D-M3-12 断线重连逻辑保持不变（attempt 0 首次连接，失败按 seq 游标 resume）。
async function consumeGeneration(query, recent, sid, continuation, opts = {}) {
  // opts.resume：刷新 / 切页回来后按游标续流（不重新发起提问，沿用原 request_id）
  const resuming = !!opts.resume
  const st = chat.activeStream
  if (resuming && (!st || st.sid !== sid)) return
  // 固化本请求 id：切会话后新请求会覆盖全局 currentRequestId，收尾时用它判断
  // "我是否仍是当前请求"，避免旧流收尾误清新请求的状态。
  const myRequestId = resuming ? st.requestId : currentRequestId.value
  let ctrl = abortController.value
  if (!ctrl || ctrl.signal.aborted) {
    ctrl = new AbortController()
    abortController.value = ctrl
  }
  // 当前视图是否仍停留在本请求所属会话（切换会话后不再向新会话视图写入状态）
  const isActiveView = () => chat.sessionId === sid
  // 是否可以更新组件局部状态 / DOM（切页卸载期间为 false，但 store 写入照常）
  const canPaint = () => isActiveView() && viewActive.value
  // 本流的思考轨迹归属判断：切会话后 activeStream 可能已被新请求占用，
  // 只有仍属于自己的流才追加 trace（否则会串到新会话的思考区）
  const myStream = () => {
    const s = chat.activeStream
    return s && s.requestId === myRequestId ? s : null
  }
  // 续流起点：接着快照里已累积的内容继续长，断线期间已生成的部分不丢
  const seed = resuming ? chat.streamProgress() : { answer: '', sources: [], lastSeq: 0 }
  const localThinking = resuming ? [...(st?.traces || [])] : []  // 独立于视图，供中断保存
  // 跨 try/catch 共享：正常收尾与中断保存都要用到（放函数顶层，勿下沉到 try 块内）
  let answer = seed.answer || ''
  let sources = seed.sources || []
  if (resuming) {
    currentRequestId.value = myRequestId  // 续流期间"停止"作用在被续的这条流上
  } else {
    chat.beginStream(sid, myRequestId)
  }
  try {
    let lastSeq = seed.lastSeq || 0   // D-M3-12：已收到的最大 seq，重连时作为续流游标
    let attempt = resuming ? 1 : 0    // 续流直接进入 resume 分支
    while (true) {
      try {
        const iter = attempt === 0
          ? (continuation
            ? confirmSceneStream(
                {
                  sessionId: sid,
                  sceneId: continuation.scene,
                  query,
                  history: recent,
                  requestId: myRequestId,
                  confirmId: continuation.confirmId,
                },
                { signal: ctrl?.signal },
              )
            : streamChat(query, recent, sid, { signal: ctrl?.signal, requestId: myRequestId }))
          : resumeChat(myRequestId, lastSeq, { signal: ctrl?.signal })
        if (attempt > 0) {
          // D-M3-12 断线重连：后端在断线后继续跑完写事件日志，这里按游标补发续流
          if (canPaint()) {
            const tip = resuming && attempt === 1
              ? '正在接续上次的回答…'
              : `连接中断，正在重连续流…（第 ${attempt} 次）`
            thinkingTraces.value.push({ text: tip, kind: 'thinking' })
          }
        }
        for await (const msg of iter) {
          if (typeof msg.seq === 'number') {
            lastSeq = msg.seq
            chat.updateStream({ seq: msg.seq })
          }
          if (msg.type === 'thinking') {
            const t = { text: msg.content, kind: 'thinking' }
            localThinking.push(t)
            const s = myStream()
            if (s) s.traces.push(t)
            if (canPaint()) thinkingTraces.value.push(t)
          } else if (msg.type === 'tool_call') {
            // F4 过程透明化：记录工具调用
            const t = { text: `正在调用 ${msg.tool}`, kind: 'tool_call' }
            localThinking.push(t)
            const s = myStream()
            if (s) s.traces.push(t)
            if (canPaint()) thinkingTraces.value.push(t)
          } else if (msg.type === 'tool_result') {
            // F4 过程透明化：记录工具结果摘要；ok=false 用告警样式区分
            const t = {
              text: msg.summary || '',
              kind: msg.ok === false ? 'tool_result_error' : 'tool_result',
            }
            localThinking.push(t)
            const s = myStream()
            if (s) s.traces.push(t)
            if (canPaint()) thinkingTraces.value.push(t)
          } else if (msg.type === 'clear') {
            // 校验未通过，清掉最后一条 assistant 消息重新生成
            const list = chat.messagesOf(sid)
            while (list.length > 0 && list[list.length - 1].role === 'assistant') {
              list.pop()
            }
            answer = ''
            chat.updateStream({ answer })
          } else if (msg.type === 'meta') {
            if (msg.sources?.length) {
              sources = msg.sources
              chat.updateStream({ sources })
            }
          } else if (msg.type === 'confirmation_required') {
            // F12：B 类场景需人工确认，本次流到此结束，等用户确认后同连接续跑
            confirmState.value = {
              open: true,
              scene: msg.scene || '',
              sceneName: msg.scene_name || '',
              prompt: msg.prompt || '该流程需要您确认后继续。',
              confirmId: msg.confirm_id || '',
              query,
              recent,  // 缓存"本提问之前的轮次"，续跑请求携带（勿带当前问题）
              sid,
            }
          } else if (msg.type === 'token') {
            if (!answered.value && canPaint()) {
              answered.value = true
              thinkingOpen.value = false  // 思考结束，折叠
            }
            answer += msg.content
            chat.updateStream({ answer })
            // 写本请求所属会话的消息数组（后台会话照样累积：切走后跑完落库，
            // 切回即见完整答案；不再受 isActiveView 限制）
            const list = chat.messagesOf(sid)
            const last = list[list.length - 1]
            if (last?.role === 'assistant') {
              last.content = answer
            } else {
              list.push({ role: 'assistant', content: answer })
            }
            // rAF 节流：同帧内多个 token 只触发一次滚动（不再逐 token 强制布局）
            if (canPaint()) scheduleScroll()
          }
        }
        // 流结束兜底：把最后一批 token 的滚动补上（此时 scheduleScroll 可能
        // 尚未执行或已取消），再精确滚一次
        await nextTick()
        if (viewActive.value) scrollBottom()
        break
      } catch (e) {
        // D-M3-12：仅"非用户主动取消的网络中断"自动重连续流（最多 2 次）；
        // 用户点停止（AbortError）、无游标可续、无 request_id → 交外层处理
        const cancelled = e?.name === 'AbortError' || ctrl?.signal?.aborted
        if (cancelled || !myRequestId || lastSeq === 0 || attempt >= 2) throw e
        attempt += 1
      }
    }
    if (confirmState.value.open) {
      // F12 等待确认：本次流无回答，不保存会话（确认后续跑正常保存）
    } else if (!answer) {
      if (canPaint()) chat.messagesOf(sid).push({ role: 'assistant', content: '抱歉，没有生成回答，请重试。' })
    } else {
      const assistantMsg = { role: 'assistant', content: answer, thinking: [...localThinking], sources }
      // 写回本请求所属会话（切走后也能精确落位），不再依赖当前视图
      const list = chat.messagesOf(sid)
      if (list.length && list[list.length - 1]?.role === 'assistant') {
        list[list.length - 1] = assistantMsg
      } else {
        list.push(assistantMsg)
      }
      // 写回发起时的会话（sid 固定）：即使期间切走了视图，回答也落回原会话。
      // 保存进 store 级串行队列即可，无需阻塞本流收尾（失败由链内吞掉并在
      // 下次保存自动重传，宁可多传不丢消息）。
      persistSession(sid, [assistantMsg]).catch(() => {})
      if (canPaint()) await refreshSessions()
    }
  } catch (e) {
    // 用户主动取消/网络中断：保留已生成的部分，写回原会话，不当作错误提示
    const cancelled = e?.name === 'AbortError' || ctrl?.signal?.aborted
    if (cancelled) {
      if (answer) {
        // 中断但已有部分/完整回答：落库到发起会话，避免"切换对话后回复没了"
        const partialMsg = { role: 'assistant', content: answer, thinking: [...localThinking], sources }
        const list = chat.messagesOf(sid)
        if (list.length && list[list.length - 1]?.role === 'assistant') {
          list[list.length - 1] = partialMsg
        } else {
          list.push(partialMsg)
        }
        persistSession(sid, [partialMsg]).catch(() => {})
      } else if (canPaint()) {
        chat.messagesOf(sid).push({ role: 'assistant', content: '已停止生成。' })
      }
    } else if (canPaint()) {
      // 续流失败（事件日志过期等）给出人话提示，而不是抛一句"重连失败: 404"
      const text = /重连|过期/.test(e?.message || '')
        ? '上次的回答已中断（服务端缓存已过期），请重新提问。'
        : `请求失败: ${e.message}`
      chat.messagesOf(sid).push({ role: 'assistant', content: text })
    }
  }
  // 收尾守卫：本请求仍是最新请求才清理共享状态（isActiveView 只判视图会话，
  // 不够——切换后新会话可能已发起新请求，旧请求收尾不得把新请求的 controller
  // 清空、也不得把新请求的 sending 置 false）
  // endStream 内部会校验 sid + requestId：后台会话的流跑完时也要清掉
  // activeStream / 续流快照（否则下次刷新会去续一条早已结束的流）。
  chat.endStream(sid, myRequestId)
  const isCurrentRequest = currentRequestId.value === myRequestId
  if (isCurrentRequest && isActiveView()) {
    chat.sending = false
    abortController.value = null
  }
}

// 刷新 / 切页回来：本会话留有未完成的生成 → 按服务端事件游标续流。
// 后端 D-M3-12 在断线后会继续把这一轮跑完并写事件日志，不续流等于白烧 Token
// 且用户永远看不到内容；这里续上，内容与正常生成完全一致。
async function resumeIfPending() {
  const st = chat.activeStream
  if (!st || st.sid !== chat.sessionId) return
  await consumeGeneration('', [], st.sid, null, { resume: true })
}

async function handleNewChat() {
  // 2026-09-04（用户选定语义）：新建会话不再中断生成 —— 旧会话仍在跑的回答
  // 会在后台跑完并落库，切回去就是完整答案。要立刻停请用"停止"按钮或登出。
  confirmState.value = { open: false, scene: '', sceneName: '', prompt: '', confirmId: '', query: '', recent: [], sid: '' }
  chat.newSession()  // 内部走 switchSession：保活仍有流在跑的旧会话现场
  // 旧流交由后台自行跑完：断开与"停止"能力的绑定，避免误停后台会话
  abortController.value = null
  currentRequestId.value = ''
  chat.resetBaseline(chat.sessionId)  // 新会话：增量保存基线归零
  chat.sending = false
  thinkingTraces.value = []
  answered.value = false
  await refreshSessions()
}

async function handleSelect(targetId) {
  // 2026-09-04（用户选定语义）：切换会话同样不中断生成 —— 原会话的回答后台
  // 跑完并落库，切回即见完整答案（此前 abort 只是断开连接，后端按 D-M3-12
  // 仍会跑完整轮，结果谁也收不到：前端空白 + Token 照扣）。
  confirmState.value = { open: false, scene: '', sceneName: '', prompt: '', confirmId: '', query: '', recent: [], sid: '' }
  chat.switchSession(targetId)
  abortController.value = null
  currentRequestId.value = ''
  const running = chat.activeStream?.sid === targetId
  chat.sending = !!running
  thinkingTraces.value = running ? (chat.activeStream.traces || []) : []
  thinkingOpen.value = true
  answered.value = false
  await loadCurrentSession()
}

async function handleDelete(sessionId) {
  try {
    await deleteConversation(sessionId)
    // 被删会话仍有生成在跑：答案已无处可落，立即停掉（省 Token）
    if (chat.activeStream?.sid === sessionId) {
      abortController.value?.abort()
      cancelChat(currentRequestId.value)
      chat.endStream(sessionId, currentRequestId.value)
      abortController.value = null
      currentRequestId.value = ''
    }
    await refreshSessions()
    if (chat.sessionId === sessionId) handleNewChat()
  } catch (e) {
    console.error('删除会话失败:', e)
    alert('删除会话失败：' + (e?.message || e))
  }
}

function doLogout() {
  // 登出即停：身份已变，续流会被归属校验拒绝（resume 403），留着快照只会
  // 让下次登录误触重连。这里发 /chat/cancel 真正中断后端生成。
  abortController.value?.abort()
  if (currentRequestId.value) cancelChat(currentRequestId.value)
  chat.endStream(chat.sessionId, currentRequestId.value)
  currentRequestId.value = ''
  abortController.value = null
  chat.newSession()
  auth.logout()
  router.replace('/login')
}

// 生成中跳路由（2026-09-03 切页保活）：不再 abort 掉 SSE —— 生成在后台继续跑并
// 持续写回 store / 落库；切回时 onMounted 从 store 恢复现场（对话不被"掐断"、
// 也不会延迟"全部冒出来"）。要显式中断请用输入区的"停止"按钮。
onBeforeUnmount(() => {
  viewActive.value = false
})
</script>

<style scoped>
.chat-layout { height: 100vh; display: flex; background: var(--color-surface); }
.main-area { flex: 1; display: flex; flex-direction: column; min-width: 0; }

/* 顶部工具栏 */
.header {
  background: var(--color-surface);
  padding: 12px 20px;
  display: flex;
  align-items: center;
  flex-shrink: 0;
  border-bottom: 1px solid var(--color-border-light);
}
.header-left { display: flex; align-items: center; gap: 8px; }
.logo-icon { width: 22px; height: 22px; color: var(--color-primary); }
.app-name { font-size: 16px; font-weight: 600; color: var(--color-text); }
.header-right { margin-left: auto; }
.nav-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  padding: 6px 14px;
  border-radius: 8px;
  color: var(--color-text-secondary);
  transition: all var(--transition);
}
.nav-link:hover { background: var(--color-sidebar-hover); color: var(--color-primary); }

/* 消息区 */
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  max-width: 860px;
  margin: 0 auto;
  width: 100%;
}

/* 空状态 */
.welcome { text-align: center; padding: 60px 20px 30px; }
.welcome-logo {
  width: 56px; height: 56px;
  margin: 0 auto 16px;
  border-radius: 14px;
  background: var(--color-primary-light);
  color: var(--color-primary);
  display: flex;
  align-items: center;
  justify-content: center;
}
.welcome-title { font-size: 24px; font-weight: 700; color: var(--color-text); margin-bottom: 8px; }
.welcome-sub { font-size: 14px; color: var(--color-text-muted); margin-bottom: 36px; }

.suggest-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
  max-width: 640px;
  margin: 0 auto 28px;
}
.suggest-card {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 14px 16px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  cursor: pointer;
  font-size: 14px;
  color: var(--color-text-secondary);
  text-align: left;
  line-height: 1.5;
  transition: all var(--transition);
}
.suggest-card svg { color: var(--color-primary); flex-shrink: 0; margin-top: 2px; }
.suggest-card:hover {
  border-color: var(--color-primary-border);
  background: var(--color-primary-light);
  color: var(--color-primary);
  box-shadow: 0 2px 8px rgba(79, 70, 229, 0.08);
}

.welcome-disclaimer {
  font-size: 12px;
  color: var(--color-text-muted);
  max-width: 520px;
  margin: 0 auto;
  padding-top: 12px;
  border-top: 1px solid var(--color-border-light);
}

/* 思考过程 */
.thinking-box { margin: 4px 0 8px; }
.thinking-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  color: var(--color-text-muted);
  font-size: 13px;
  cursor: pointer;
  padding: 4px 0;
  transition: color 150ms ease;
}
.thinking-toggle:hover { color: var(--color-primary); }
.thinking-toggle svg { transition: transform 150ms ease; width: 14px; height: 14px; }
.thinking-toggle svg.rotated { transform: rotate(90deg); }
.thinking-traces {
  margin-top: 4px;
  padding: 10px 14px;
  background: var(--color-sidebar-bg);
  border-left: 3px solid var(--color-primary-border);
  border-radius: 0 8px 8px 0;
  font-size: 13px;
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.trace-item { padding: 2px 0; white-space: pre-wrap; word-break: break-word; }
.trace-item.trace-tool_call { color: var(--color-primary); font-weight: 500; }
.trace-item.trace-tool_result { color: var(--color-text-secondary); }
.trace-item.trace-tool_result_error { color: #dc2626; font-weight: 500; }
.spinner {
  width: 12px; height: 12px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
  flex-shrink: 0;
  display: inline-block;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* 改写确认卡 */
.rewrite-confirm { display: flex; justify-content: center; padding: 8px 20px 0; }
.rewrite-card {
  width: 100%; max-width: 860px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 16px;
  box-shadow: var(--shadow-pop);
}
.rewrite-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
.rewrite-badge { background: var(--color-primary); color: #fff; font-size: 12px; padding: 2px 10px; border-radius: 10px; }
.rewrite-hint { font-size: 13px; color: var(--color-text-muted); }
.rewrite-loading { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--color-text-muted); padding: 4px 0; }
.rewrite-note { font-size: 13px; color: var(--color-text-muted); margin-bottom: 8px; }
.rewrite-orig {
  background: var(--color-primary-light);
  border-left: 3px solid var(--color-primary);
  border-radius: 0 6px 6px 0;
  padding: 8px 12px;
  font-size: 14px;
  color: var(--color-text);
  margin-bottom: 4px;
  white-space: pre-wrap;
  word-break: break-word;
}
.rewrite-input {
  width: 100%;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 14px;
  resize: vertical;
  color: var(--color-text);
  box-sizing: border-box;
}
.rewrite-input:focus { outline: none; border-color: var(--color-primary); }
.rewrite-check { display: flex; align-items: center; gap: 8px; margin: 12px 0; font-size: 13px; color: var(--color-text); cursor: pointer; }
.rewrite-actions { display: flex; gap: 10px; flex-wrap: wrap; }
.btn-confirm { background: var(--color-primary); color: #fff; border: none; padding: 8px 18px; border-radius: 8px; font-size: 13px; cursor: pointer; transition: opacity 150ms; }
.btn-confirm:disabled { opacity: 0.45; cursor: not-allowed; }
.btn-original { background: none; border: 1px solid var(--color-border); color: var(--color-text-muted); padding: 8px 18px; border-radius: 8px; font-size: 13px; cursor: pointer; }
.btn-original:hover { color: var(--color-primary); border-color: var(--color-primary); }

/* 输入区 */
.input-wrap {
  flex-shrink: 0;
  padding-bottom: 14px;
  background: linear-gradient(180deg, transparent 0%, var(--color-surface) 30%);
}
.rewrite-switch { max-width: 860px; margin: 0 auto; padding: 6px 24px 0; }
.switch { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; user-select: none; }
.switch input {
  width: 30px; height: 17px;
  appearance: none;
  background: var(--color-border);
  border-radius: 10px;
  position: relative;
  cursor: pointer;
  transition: background 150ms;
  outline: none;
  flex-shrink: 0;
}
.switch input::after {
  content: '';
  position: absolute;
  top: 2px; left: 2px;
  width: 13px; height: 13px;
  border-radius: 50%;
  background: var(--color-surface);
  transition: transform 150ms;
  box-shadow: 0 1px 2px rgba(0,0,0,0.2);
}
.switch input:checked { background: var(--color-primary); }
.switch input:checked::after { transform: translateX(13px); }
.switch-text { font-size: 12px; color: var(--color-text-muted); }
.switch.on .switch-text { color: var(--color-primary); font-weight: 500; }
</style>
