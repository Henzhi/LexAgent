import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('lawrag_token') || '')
  const username = ref(localStorage.getItem('lawrag_username') || '')

  const isAuthenticated = computed(() => !!token.value)

  function setAuth(t, u) {
    token.value = t
    username.value = u
    localStorage.setItem('lawrag_token', t)
    localStorage.setItem('lawrag_username', u)
  }

  function logout() {
    token.value = ''
    username.value = ''
    localStorage.removeItem('lawrag_token')
    localStorage.removeItem('lawrag_username')
  }

  return { token, username, isAuthenticated, setAuth, logout }
})
