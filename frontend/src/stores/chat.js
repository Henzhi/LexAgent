import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useChatStore = defineStore('chat', () => {
  const sessionId = ref(localStorage.getItem('lawrag_session') || crypto.randomUUID())
  const messages = ref([])
  const sending = ref(false)

  function newSession() {
    sessionId.value = crypto.randomUUID()
    localStorage.setItem('lawrag_session', sessionId.value)
    messages.value = []
  }

  return { sessionId, messages, sending, newSession }
})
