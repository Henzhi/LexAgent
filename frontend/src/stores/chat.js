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

  function newSession() {
    sessionId.value = crypto.randomUUID()
    sessionStorage.setItem(SESSION_KEY, sessionId.value)
    messages.value = []
  }

  return { sessionId, messages, sending, newSession }
})
