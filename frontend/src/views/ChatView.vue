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
import { loadHistory, listConversations, saveSession, streamChat, resumeChat, rewriteQuery, deleteConversation, cancelChat, confirmScene } from '../api'
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
const rewriteState = ref({ open: false, loading: false, original: '', proposed: '', acknowledged: false, changed: false })
watch(rewriteEnabled, v => localStorage.setItem('lawrag_rewrite', v ? '1' : '0'))

// F12 人工确认（B 类场景：合同起草/审查、文书生成等）：
// 后端产出 confirmation_required 事件并结束流，用户确认后重新发起 stream
const confirmState = ref({ open: false, scene: '', sceneName: '', prompt: '', confirmId: '', query: '' })

async function confirmProceed() {
  const st = confirmState.value
  const sid = st.sid || chat.sessionId
  confirmState.value = { ...st, open: false }
  try {
    await confirmScene(sid, st.scene, st.query, true, st.confirmId)
  } catch (e) {
    if (chat.sessionId === sid) chat.messages.push({ role: 'assistant', content: `确认失败: ${e.message}` })
    return
  }
  await runStream(st.query, chat.messages.slice(-20), sid)
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
  await refreshSessions()
  await loadCurrentSession()
})

async function refreshSessions() {
  try {
    const data = await listConversations()
    sessions.value = data || []
  } catch { /* ignore */ }
}

async function loadCurrentSession() {
  const sid = chat.sessionId
  try {
    const data = await loadHistory(sid)
    if (data.history?.length) {
      chat.messages = data.history
      // 增量保存基线：历史已存于服务端，之后只追加新消息
      savedCounts.value[sid] = chat.messages.length
      // 从最后一条 assistant 消息中恢复 thinkingTraces
      const lastMsg = chat.messages[chat.messages.length - 1]
      if (lastMsg?.role === 'assistant' && lastMsg.thinking?.length) {
        // 兼容旧版字符串格式，归一化为 { text, kind } 结构
        thinkingTraces.value = lastMsg.thinking.map(t =>
          typeof t === 'string' ? { text: t, kind: 'thinking' } : t
        )
        thinkingOpen.value = true  // 加载历史时保持思考过程可见
      }
      answered.value = true
      await nextTick()
      scrollBottom()
    } else {
      savedCounts.value[sid] = 0
    }
  } catch {
    // 历史加载失败：基线归零，下轮保存走全量（与旧行为一致）
    savedCounts.value[sid] = 0
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

// 会话增量保存（2026-09-03 修复「切换对话丢消息/串会话」）：
// 此前 savedCount 是全局单值、body 在异步链里才读 chat.sessionId / chat.messages——
// 切到新会话后，旧会话排队中的保存会用新会话的 ID 和消息，造成：
//   ① 旧会话回复没存进去 → 切回后"回复没了"；
//   ② 旧会话内容 append 到新会话 → 串会话。
// 修复要点：
// - 基线按会话独立计数（savedCounts[sid]），互不覆盖；
// - persistSession(sid) 在调用瞬间就快照 body 并固定 sid，链上执行不再读
//   全局 chat.sessionId / chat.messages——切换会话不影响已排队的保存；
// - 显式增量（extraMsgs）：回答完成/中断时把本条 assistant 消息精确追加，
//   即使视图已切走也能写回正确会话；
// - 保存失败不推进基线，下次调用自动重传该段（宁可多传不丢消息）；
// - saveChain 串行化：上一轮保存未完成时下一轮排队，避免并发追加重复。
const savedCounts = ref({})
let saveChain = Promise.resolve()

function savedBaseline(sid) {
  return savedCounts.value[sid] || 0
}

function persistSession(sid, extraMsgs = null) {
  const baseline = savedBaseline(sid)
  let body
  let mode
  if (extraMsgs && extraMsgs.length) {
    // 显式增量：只传本条新增（assistant 消息），视图已切走也能精确追加回原会话
    body = extraMsgs
    mode = 'append'
  } else {
    // 全量/增量快照：调用瞬间切片并固定 sid
    if (chat.sessionId !== sid) return saveChain // 视图已切走且无显式增量 → 无可保存
    const total = chat.messages.length
    if (total === 0 || total <= baseline) return saveChain
    body = baseline === 0 ? [...chat.messages] : chat.messages.slice(baseline)
    mode = baseline === 0 ? 'replace' : 'append'
  }
  const p = saveChain.then(() =>
    saveSession(sid, body, mode).then((res) => {
      const total = res && typeof res.total === 'number' ? res.total : baseline + body.length
      savedCounts.value[sid] = Math.max(baseline, total)
    })
  )
  saveChain = p.catch(() => {})
  return p
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
  }
}

// 智能改写流程：开启开关后，每次发送都先调用 /api/rewrite，弹出确认卡等待用户确认。
// 无论模型是否改动，都给出可见的人机协作步骤（案情分析模式必须确认）。
async function doRewrite(original, recent, sid) {
  rewriteState.value = { open: false, loading: true, original, proposed: original, acknowledged: false, changed: false, sid }
  try {
    const res = await rewriteQuery(original)
    // 改写期间用户切走了会话：放弃弹卡，也不在错误会话继续生成
    if (chat.sessionId !== sid) return
    const proposed = (res.proposed_query || original).trim()
    const changed = proposed !== original.trim() && !res.skipped
    rewriteState.value = { open: true, loading: false, original, proposed, acknowledged: false, changed, sid }
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
  const recent = chat.messages.slice(-20)
  const sid = rewriteState.value.sid || chat.sessionId
  rewriteState.value = { ...rewriteState.value, open: false, loading: false }
  await runStream(finalQuery, recent, sid)
}

// 改用原句 / 未改写时确认检索（保证绝对精确）
async function useOriginal() {
  const original = rewriteState.value.original
  const recent = chat.messages.slice(-20)
  const sid = rewriteState.value.sid || chat.sessionId
  rewriteState.value = { ...rewriteState.value, open: false, loading: false }
  await runStream(original, recent, sid)
}

async function runStream(query, recent, sid) {
  const ctrl = abortController.value
  // 当前视图是否仍停留在本请求所属会话（切换后不再向新会话视图写入状态）
  const isActiveView = () => chat.sessionId === sid
  const localThinking = []  // 本次请求产生的思考轨迹（独立于视图，供中断保存）
  // 跨 try/catch 共享：正常收尾与中断保存都要用到（放函数顶层，勿下沉到 try 块内）
  let answer = ''
  let sources = []
  try {
    let lastSeq = 0   // D-M3-12：已收到的最大 seq，重连时作为续流游标
    let attempt = 0
    while (true) {
      try {
        const iter = attempt === 0
          ? streamChat(query, recent, sid, { signal: ctrl?.signal, requestId: currentRequestId.value })
          : resumeChat(currentRequestId.value, lastSeq, { signal: ctrl?.signal })
        if (attempt > 0) {
          // D-M3-12 断线重连：后端在断线后继续跑完写事件日志，这里按游标补发续流
          if (isActiveView()) {
            thinkingTraces.value.push({ text: `连接中断，正在重连续流…（第 ${attempt} 次）`, kind: 'thinking' })
          }
        }
        for await (const msg of iter) {
          if (typeof msg.seq === 'number') lastSeq = msg.seq
          if (msg.type === 'thinking') {
            localThinking.push({ text: msg.content, kind: 'thinking' })
            if (isActiveView()) thinkingTraces.value.push(localThinking[localThinking.length - 1])
          } else if (msg.type === 'tool_call') {
            // F4 过程透明化：记录工具调用
            localThinking.push({ text: `正在调用 ${msg.tool}`, kind: 'tool_call' })
            if (isActiveView()) thinkingTraces.value.push(localThinking[localThinking.length - 1])
          } else if (msg.type === 'tool_result') {
            // F4 过程透明化：记录工具结果摘要；ok=false 用告警样式区分
            localThinking.push({
              text: msg.summary || '',
              kind: msg.ok === false ? 'tool_result_error' : 'tool_result',
            })
            if (isActiveView()) thinkingTraces.value.push(localThinking[localThinking.length - 1])
          } else if (msg.type === 'clear') {
            // 校验未通过，清掉最后一条 assistant 消息重新生成
            if (isActiveView()) {
              while (chat.messages.length > 0 && chat.messages[chat.messages.length - 1].role === 'assistant') {
                chat.messages.pop()
              }
            }
            answer = ''
          } else if (msg.type === 'meta') {
            if (msg.sources?.length) sources = msg.sources
          } else if (msg.type === 'confirmation_required') {
            // F12：B 类场景需人工确认，本次流到此结束，等待用户决策后重新发起
            confirmState.value = {
              open: true,
              scene: msg.scene || '',
              sceneName: msg.scene_name || '',
              prompt: msg.prompt || '该流程需要您确认后继续。',
              confirmId: msg.confirm_id || '',
              query,
              sid,
            }
          } else if (msg.type === 'token') {
            if (!answered.value && isActiveView()) {
              answered.value = true
              thinkingOpen.value = false  // 思考结束，折叠
            }
            answer += msg.content
            if (isActiveView()) {
              const last = chat.messages[chat.messages.length - 1]
              if (last?.role === 'assistant') {
                last.content = answer
              } else {
                chat.messages.push({ role: 'assistant', content: answer })
              }
              // rAF 节流：同帧内多个 token 只触发一次滚动（不再逐 token 强制布局）
              scheduleScroll()
            }
          }
        }
        // 流结束兜底：把最后一批 token 的滚动补上（此时 scheduleScroll 可能
        // 尚未执行或已取消），再精确滚一次
        await nextTick()
        scrollBottom()
        break
      } catch (e) {
        // D-M3-12：仅"非用户主动取消的网络中断"自动重连续流（最多 2 次）；
        // 用户点停止（AbortError）、无游标可续、无 request_id → 交外层处理
        const cancelled = e?.name === 'AbortError' || ctrl?.signal?.aborted
        if (cancelled || !currentRequestId.value || lastSeq === 0 || attempt >= 2) throw e
        attempt += 1
      }
    }
    if (confirmState.value.open) {
      // F12 等待确认：本次流无回答，不保存会话（确认后重新发起的流正常保存）
    } else if (!answer) {
      if (isActiveView()) chat.messages.push({ role: 'assistant', content: '抱歉，没有生成回答，请重试。' })
    } else {
      const assistantMsg = { role: 'assistant', content: answer, thinking: [...localThinking], sources }
      if (isActiveView()) {
        chat.messages[chat.messages.length - 1] = assistantMsg
      }
      // 写回发起时的会话（sid 固定）：即使期间切走了视图，回答也落回原会话。
      // 保存进全局串行 saveChain 队列即可，无需阻塞本流收尾（失败由链内吞掉
      // 并在下次保存自动重传，宁可多传不丢消息）。
      persistSession(sid, [assistantMsg]).catch(() => {})
      if (isActiveView()) await refreshSessions()
    }
  } catch (e) {
    // 用户主动取消/网络中断：保留已生成的部分，写回原会话，不当作错误提示
    const cancelled = e?.name === 'AbortError' || ctrl?.signal?.aborted
    if (cancelled) {
      if (answer) {
        // 中断但已有部分/完整回答：落库到发起会话，避免"切换对话后回复没了"
        const partialMsg = { role: 'assistant', content: answer, thinking: [...localThinking], sources }
        if (isActiveView()) {
          chat.messages[chat.messages.length - 1] = partialMsg
        }
        persistSession(sid, [partialMsg]).catch(() => {})
      } else if (isActiveView()) {
        chat.messages.push({ role: 'assistant', content: '已停止生成。' })
      }
    } else if (isActiveView()) {
      chat.messages.push({ role: 'assistant', content: `请求失败: ${e.message}` })
    }
  }
  // 收尾守卫：本请求仍是最新请求才清理共享状态（isActiveView 只判视图会话，
  // 不够——切换后新会话可能已发起新请求，旧请求收尾不得把新请求的 controller
  // 清空、也不得把新请求的 sending 置 false）
  const isCurrentRequest = abortController.value === ctrl
  if (isActiveView() && isCurrentRequest) {
    chat.sending = false
    abortController.value = null
  }
}

async function handleNewChat() {
  abortController.value?.abort()  // 中断未完成的生成，避免后端继续消耗
  chat.newSession()
  const sid = chat.sessionId
  chat.sending = false
  chat.messages = []
  savedCounts.value[sid] = 0  // 新会话：增量保存基线归零
  thinkingTraces.value = []
  answered.value = false
  confirmState.value = { open: false, scene: '', sceneName: '', prompt: '', confirmId: '', query: '', sid: '' }
  await refreshSessions()
}

async function handleSelect(sessionId) {
  abortController.value?.abort()  // 中断未完成的生成
  chat.sessionId = sessionId
  sessionStorage.setItem('lawrag_session', sessionId)
  chat.sending = false
  chat.messages = []
  savedCounts.value[sessionId] = 0  // 切换会话：基线归零，由 loadCurrentSession 重新建立
  thinkingTraces.value = []
  answered.value = false
  confirmState.value = { open: false, scene: '', sceneName: '', prompt: '', confirmId: '', query: '', sid: '' }
  await loadCurrentSession()
}

async function handleDelete(sessionId) {
  try {
    await deleteConversation(sessionId)
    await refreshSessions()
    if (chat.sessionId === sessionId) handleNewChat()
  } catch (e) {
    console.error('删除会话失败:', e)
    alert('删除会话失败：' + (e?.message || e))
  }
}

function doLogout() {
  chat.newSession()
  auth.logout()
  router.replace('/login')
}

// 生成中跳路由：中止 SSE fetch，让 consumeSSE 的 finally 释放 reader
// （2026-09-01 审查整改：否则 fetch 与 reader 继续存活，向已卸载组件写状态）
onBeforeUnmount(() => {
  abortController.value?.abort()
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
