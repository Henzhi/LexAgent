import { defineStore } from 'pinia'
import { ref } from 'vue'

// sessionId 存 sessionStorage（窗口级，2026-09-03 修复）：
// 此前用 localStorage，多标签页/窗口共享同一个 sessionId——A 窗口写合同、
// B 窗口新开提问时 B 会复用 A 的会话，回复互相串号、切回后"消息消失"。
// sessionStorage 语义：刷新同标签页保留；新开标签页/窗口各自独立新会话。
const SESSION_KEY = 'lawrag_session'

// 进行中生成的持久化快照（2026-09-04 修复「刷新/切页后输出变空白」）：
// 刷新页面会销毁内存态（Pinia 重建），但服务端的 SSE 流仍在跑（后端 D-M3-12
// 语义：断线后继续跑完并写事件日志等重连）。若不留下游标，这一轮的答案就
// 永远回不来——用户看到"只剩提问、回答空白"，Token 却照扣。
// 这里把 { sid, requestId, lastSeq, answer, sources, traces } 存进
// sessionStorage（标签页级：新开标签页是独立会话，不会串号），刷新后据此
// 调 /chat/stream/resume 按游标续流。
const PENDING_KEY = 'lawrag_pending_stream'
// 超过该时长视为过期：服务端事件日志 TTL 默认 600s，留 60s 余量
const PENDING_MAX_AGE_MS = 9 * 60 * 1000

function loadPending() {
  try {
    const raw = sessionStorage.getItem(PENDING_KEY)
    if (!raw) return null
    const p = JSON.parse(raw)
    if (!p?.sid || !p?.requestId) return null
    if (!p.updatedAt || Date.now() - p.updatedAt > PENDING_MAX_AGE_MS) {
      sessionStorage.removeItem(PENDING_KEY) // 事件日志已过期，续流必然 404
      return null
    }
    return p
  } catch {
    return null
  }
}

export const useChatStore = defineStore('chat', () => {
  const sessionId = ref(sessionStorage.getItem(SESSION_KEY) || crypto.randomUUID())
  const messages = ref([])
  const sending = ref(false)

  // 进行中的生成请求（2026-09-03 切页保活）：路由切换不再 abort 掉 SSE，
  // 生成在后台跑完并持续写回 store。此对象在组件卸载后依然存活：
  // - 切走再回来时据此恢复"思考过程"与进行中状态（不误清空、不重复加载历史）；
  // - 仅当前这一个请求（sid + requestId 定位），结束时清空。
  // 2026-09-04：刷新后由 sessionStorage 的快照重建（traces 用于恢复思考区，
  // lastSeq/answer/sources 用于按游标续流）。
  const pending = loadPending()
  // connected：当前流是否有一条"活着的 SSE 连接"（2026-09-04 防重复续流）。
  // 从 sessionStorage 快照重建（刷新后）必然没有活连接 → false，此时才允许
  // resume；切页保活的流连接仍在（beginStream → true），回到页面绝不能再发
  // resume，否则同一流被两个消费者读取：内容互相覆盖、收尾各自落库 → 回答
  // 在服务端出现两条。
  const activeStream = ref(
    pending
      ? { sid: pending.sid, requestId: pending.requestId, traces: pending.traces || [], connected: false }
      : null,
  )

  // 续流进度（非响应式：token 高频更新，无需驱动渲染，只在持久化与收尾时用）
  let progress = pending
    ? { lastSeq: pending.lastSeq || 0, answer: pending.answer || '', sources: pending.sources || [] }
    : { lastSeq: 0, answer: '', sources: [] }

  // 后台会话的消息分桶（2026-09-04 切会话后台续跑）：切走会话时若该会话仍有
  // 生成在进行，把消息数组暂存此处，SSE 继续往里写；切回时原样取回，
  // 看到的是持续增长的完整答案，而不是被掐断的空白。
  const drafts = ref({})

  // 快照写入节流：token 每几十毫秒一个，写 sessionStorage 500ms 一次足够
  // （刷新最多丢失最后半秒内容，续流会按 seq 游标补发，不会真的缺失）。
  let persistTimer = null
  function schedulePersist() {
    if (persistTimer) return
    persistTimer = setTimeout(() => {
      persistTimer = null
      const s = activeStream.value
      if (!s) {
        sessionStorage.removeItem(PENDING_KEY)
        return
      }
      try {
        sessionStorage.setItem(PENDING_KEY, JSON.stringify({
          sid: s.sid,
          requestId: s.requestId,
          lastSeq: progress.lastSeq || 0,
          answer: progress.answer || '',
          sources: progress.sources || [],
          traces: (s.traces || []).slice(-80), // 只留最近 80 条，避免撑爆配额
          updatedAt: Date.now(),
        }))
      } catch { /* 配额超限 / 隐私模式：放弃快照，续流降级为重新提问 */ }
    }, 500)
  }

  function beginStream(sid, requestId, seed = null) {
    activeStream.value = { sid, requestId, traces: seed?.traces || [], connected: true }
    progress = seed
      ? { lastSeq: seed.lastSeq || 0, answer: seed.answer || '', sources: seed.sources || [] }
      : { lastSeq: 0, answer: '', sources: [] }
    schedulePersist()
  }

  // 标记当前流是否拥有活连接（resumeIfPending 发起续流前调用，见上方说明）
  function markConnected(v) {
    if (activeStream.value) activeStream.value.connected = !!v
  }

  // 更新续流游标 / 已生成内容（非渲染路径，节流落盘）
  function updateStream(patch = {}) {
    if (patch.seq != null && patch.seq > (progress.lastSeq || 0)) progress.lastSeq = patch.seq
    if (patch.answer != null) progress.answer = patch.answer
    if (patch.sources != null) progress.sources = patch.sources
    schedulePersist()
  }

  function streamProgress() {
    return progress
  }

  function endStream(sid, requestId) {
    if (
      activeStream.value &&
      activeStream.value.sid === sid &&
      (!requestId || activeStream.value.requestId === requestId)
    ) {
      activeStream.value = null
      progress = { lastSeq: 0, answer: '', sources: [] }
      sessionStorage.removeItem(PENDING_KEY)
      if (persistTimer) {
        clearTimeout(persistTimer)
        persistTimer = null
      }
    }
  }

  // 取某会话的消息数组：当前会话返回渲染中的 messages；后台会话返回草稿桶
  // （SSE 照样往里写，切回时即为最新内容）
  function messagesOf(sid) {
    if (sid === sessionId.value) return messages.value
    if (!drafts.value[sid]) drafts.value[sid] = []
    return drafts.value[sid]
  }

  // 切换会话：保活仍有生成在跑的旧会话现场，恢复目标会话的现场
  function switchSession(newSid) {
    const old = sessionId.value
    if (old === newSid) return
    const keeping = activeStream.value && activeStream.value.sid === old
    if (keeping) {
      drafts.value[old] = messages.value
    } else {
      delete drafts.value[old] // 无进行中的流：不缓存，切回时从服务端历史重载
    }
    sessionId.value = newSid
    sessionStorage.setItem(SESSION_KEY, newSid)
    messages.value = drafts.value[newSid] || []
    delete drafts.value[newSid]
  }

  // ---- 会话增量保存基线（2026-09-03 迁入 store）----
  // 原先在 ChatView 组件内：路由切换卸载组件后基线/串行队列随组件销毁，
  // 切回重挂载时基线丢失 → 下次保存误走全量 replace，可能与排队中的旧保存
  // 竞争造成服务端重复追加。挪到 store（模块级单例）后跨路由存活。
  const savedCounts = ref({})
  let saveChain = Promise.resolve()

  function baselineOf(sid) {
    return savedCounts.value[sid] || 0
  }

  function setBaseline(sid, total) {
    const cur = baselineOf(sid)
    if (typeof total === 'number' && total > cur) savedCounts.value[sid] = total
  }

  // 串行化保存：上一轮未完成时下一轮排队，避免并发追加重复
  function enqueueSave(fn) {
    const p = saveChain.then(fn)
    saveChain = p.catch(() => {})
    return p
  }

  function resetBaseline(sid) {
    savedCounts.value[sid] = 0
  }

  function newSession() {
    // 新建会话同样走保活路径：旧会话若仍有生成在跑，现场存草稿桶继续跑完
    switchSession(crypto.randomUUID())
  }

  return {
    sessionId,
    messages,
    sending,
    activeStream,
    beginStream,
    endStream,
    markConnected,
    updateStream,
    streamProgress,
    messagesOf,
    switchSession,
    baselineOf,
    setBaseline,
    resetBaseline,
    enqueueSave,
    newSession,
  }
})
