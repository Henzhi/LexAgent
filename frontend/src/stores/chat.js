import { defineStore } from 'pinia'
import { ref } from 'vue'

// sessionId 存 sessionStorage（窗口级，2026-09-03 修复）：
// 此前用 localStorage，多标签页/窗口共享同一个 sessionId——A 窗口写合同、
// B 窗口新开提问时 B 会复用 A 的会话，回复互相串号、切回后"消息消失"。
// sessionStorage 语义：刷新同标签页保留；新开标签页/窗口各自独立新会话。
const SESSION_KEY = 'lawrag_session'

export const useChatStore = defineStore('chat', () => {
  const sessionId = ref(sessionStorage.getItem(SESSION_KEY) || crypto.randomUUID())
  const messages = ref([])
  const sending = ref(false)

  // 进行中的生成请求（2026-09-03 切页保活）：路由切换不再 abort 掉 SSE，
  // 生成在后台跑完并持续写回 store。此对象在组件卸载后依然存活：
  // - 切走再回来时据此恢复"思考过程"与进行中状态（不误清空、不重复加载历史）；
  // - 仅当前这一个请求（sid + requestId 定位），结束时清空。
  const activeStream = ref(null)

  function beginStream(sid, requestId) {
    activeStream.value = { sid, requestId, traces: [] }
  }

  function endStream(sid, requestId) {
    if (
      activeStream.value &&
      activeStream.value.sid === sid &&
      (!requestId || activeStream.value.requestId === requestId)
    ) {
      activeStream.value = null
    }
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
    sessionId.value = crypto.randomUUID()
    sessionStorage.setItem(SESSION_KEY, sessionId.value)
    messages.value = []
  }

  return {
    sessionId,
    messages,
    sending,
    activeStream,
    beginStream,
    endStream,
    baselineOf,
    setBaseline,
    resetBaseline,
    enqueueSave,
    newSession,
  }
})
