import { createRouter, createWebHashHistory } from 'vue-router'
import LoginView from '../views/LoginView.vue'
import ChatView from '../views/ChatView.vue'
import KnowledgeView from '../views/KnowledgeView.vue'
import CrawlView from '../views/CrawlView.vue'

const routes = [
  { path: '/', name: 'chat', component: ChatView, meta: { requiresAuth: true } },
  { path: '/login', name: 'login', component: LoginView },
  { path: '/knowledge', name: 'knowledge', component: KnowledgeView, meta: { requiresAuth: true } },
  { path: '/crawl', name: 'crawl', component: CrawlView, meta: { requiresAuth: true } },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

router.beforeEach((to, from, next) => {
  // Token 迁移后凭据在 HttpOnly Cookie（JS 不可读），路由守卫退而检查非机密的
  // 用户名标记；Cookie 若已失效，首个 API 的 401 会由全局拦截器登出并跳回登录页。
  const username = localStorage.getItem('lawrag_username')
  if (to.meta.requiresAuth && !username) {
    next('/login')
  } else {
    next()
  }
})

export default router
