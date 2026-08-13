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
              <div v-for="(t, i) in thinkingTraces" :key="i" class="trace-item" style="white-space:pre-wrap">{{ t }}</div>
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
import { ref, computed, onMounted, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useChatStore } from '../stores/chat'
import { loadHistory, listConversations, saveSession, streamChat, rewriteQuery, deleteConversation, cancelChat } from '../api'
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
  try {
    const data = await loadHistory(chat.sessionId)
    if (data.history?.length) {
      chat.messages = data.history
      // 从最后一条 assistant 消息中恢复 thinkingTraces
      const lastMsg = chat.messages[chat.messages.length - 1]
      if (lastMsg?.role === 'assistant' && lastMsg.thinking?.length) {
        thinkingTraces.value = [...lastMsg.thinking]
        thinkingOpen.value = true  // 加载历史时保持思考过程可见
      }
      answered.value = true
      await nextTick()
      scrollBottom()
    }
  } catch { /* no history */ }
}

function scrollBottom() {
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}

async function handleSend(query) {
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

  if (rewriteEnabled.value) {
    await doRewrite(query, recent)
  } else {
    await runStream(query, recent)
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
async function doRewrite(original, recent) {
  rewriteState.value = { open: false, loading: true, original, proposed: original, acknowledged: false, changed: false }
  try {
    const res = await rewriteQuery(original)
    const proposed = (res.proposed_query || original).trim()
    const changed = proposed !== original.trim() && !res.skipped
    rewriteState.value = { open: true, loading: false, original, proposed, acknowledged: false, changed }
    // 等待用户确认（使用改写）/ 改用原句
  } catch (e) {
    // 改写失败不阻塞用户，降级为原句并直接检索
    await runStream(original, recent)
  }
}

// 用户确认改写结果（已勾选"符合案情"）→ 用改写后的查询检索
async function confirmRewrite() {
  const finalQuery = (rewriteState.value.proposed || '').trim() || rewriteState.value.original
  const recent = chat.messages.slice(-20)
  rewriteState.value = { ...rewriteState.value, open: false, loading: false }
  await runStream(finalQuery, recent)
}

// 改用原句 / 未改写时确认检索（保证绝对精确）
async function useOriginal() {
  const original = rewriteState.value.original
  const recent = chat.messages.slice(-20)
  rewriteState.value = { ...rewriteState.value, open: false, loading: false }
  await runStream(original, recent)
}

async function runStream(query, recent) {
  const ctrl = abortController.value
  try {
    let answer = ''
    let sources = []
    for await (const msg of streamChat(query, recent, chat.sessionId, { signal: ctrl?.signal, requestId: currentRequestId.value })) {
      if (msg.type === 'thinking') {
        thinkingTraces.value.push(msg.content)
      } else if (msg.type === 'clear') {
        // 校验未通过，清掉最后一条 assistant 消息重新生成
        while (chat.messages.length > 0 && chat.messages[chat.messages.length - 1].role === 'assistant') {
          chat.messages.pop()
        }
        answer = ''
      } else if (msg.type === 'meta') {
        if (msg.sources?.length) sources = msg.sources
      } else if (msg.type === 'token') {
        if (!answered.value) {
          answered.value = true
          thinkingOpen.value = false  // 思考结束，折叠
        }
        answer += msg.content
        const last = chat.messages[chat.messages.length - 1]
        if (last?.role === 'assistant') {
          last.content = answer
        } else {
          chat.messages.push({ role: 'assistant', content: answer })
        }
        await nextTick()
        scrollBottom()
      }
    }
    if (!answer) {
      chat.messages.push({ role: 'assistant', content: '抱歉，没有生成回答，请重试。' })
    } else {
      chat.messages[chat.messages.length - 1] = { role: 'assistant', content: answer, thinking: [...thinkingTraces.value], sources }
      saveSession(chat.sessionId, chat.messages).catch(() => {})
      await refreshSessions()
    }
  } catch (e) {
    // 用户主动取消：保留已生成的部分，不当作错误提示
    const cancelled = e?.name === 'AbortError' || ctrl?.signal?.aborted
    const hasContent = chat.messages.some(m => m.role === 'assistant' && m.content)
    if (cancelled && !hasContent) {
      chat.messages.push({ role: 'assistant', content: '已停止生成。' })
    } else if (!cancelled) {
      chat.messages.push({ role: 'assistant', content: `请求失败: ${e.message}` })
    }
  }
  chat.sending = false
  abortController.value = null
}

async function handleNewChat() {
  abortController.value?.abort()  // 中断未完成的生成，避免后端继续消耗
  chat.newSession()
  chat.sending = false
  chat.messages = []
  thinkingTraces.value = []
  answered.value = false
  await refreshSessions()
}

async function handleSelect(sessionId) {
  abortController.value?.abort()  // 中断未完成的生成
  chat.sessionId = sessionId
  localStorage.setItem('lawrag_session', sessionId)
  chat.sending = false
  chat.messages = []
  thinkingTraces.value = []
  answered.value = false
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
