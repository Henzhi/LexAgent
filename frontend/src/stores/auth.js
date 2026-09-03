import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

// Token 迁移 HttpOnly Cookie（2026-09-03 审查整改，长期项）：
// 凭据只活在服务端 Set-Cookie（HttpOnly + SameSite=Strict）里，JS 不可读——
// 任何 XSS 都拿不到它。此处只保留**非机密**的显示用用户名，放 localStorage
// 没有安全影响（丢了也只是 UI 显示名；真正的登录态由 Cookie + /auth/me 校验）。
//
// 遗留的旧 lawrag_token 键：一次性清理，避免旧版残留数据误导后续判断。
localStorage.removeItem('lawrag_token')

export const useAuthStore = defineStore('auth', () => {
  const username = ref(localStorage.getItem('lawrag_username') || '')

  // 注意：这只是 UI 侧的"看起来登录过"提示，不是安全边界。
  // 真实校验由后端完成（Cookie 无效时任何 API 都会 401 → 全局拦截器登出）。
  const isAuthenticated = computed(() => !!username.value)

  function setAuth(u) {
    username.value = u || ''
    localStorage.setItem('lawrag_username', u || '')
  }

  function logout() {
    username.value = ''
    localStorage.removeItem('lawrag_username')
    // 通知服务端删除 HttpOnly Cookie（JS 删不掉它，必须有服务端端点）。
    // 该端点无鉴权恒 200；失败不阻塞本地登出——残留 Cookie 会在下一次
    // 请求被 401 拦截器兜底清掉。
    import('../api').then((m) => m.logoutApi?.().catch(() => {})).catch(() => {})
  }

  return { username, isAuthenticated, setAuth, logout }
})
