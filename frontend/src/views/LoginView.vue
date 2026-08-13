<template>
  <div class="auth-page">
    <div class="auth-card">
      <div class="logo">
        <div class="logo-box">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
        </div>
        <h1>法智问答</h1>
      </div>
      <p class="subtitle">法律法规智能问答系统</p>

      <form @submit.prevent="submit">
        <label for="username">用户名</label>
        <input id="username" v-model="username" type="text" placeholder="请输入用户名" autocomplete="username" @keydown.enter="passwordRef?.focus()" />

        <label for="password">密码</label>
        <div class="pw-wrap">
          <input id="password" ref="passwordRef" v-model="password" :type="showPw ? 'text' : 'password'" placeholder="请输入密码" autocomplete="current-password" />
          <button type="button" class="pw-toggle" @click="showPw = !showPw" :aria-label="showPw ? '隐藏密码' : '显示密码'">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
              <path v-if="!showPw" d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
              <template v-else>
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                <circle cx="12" cy="12" r="3" />
              </template>
            </svg>
          </button>
        </div>

        <p class="error" v-if="error">{{ error }}</p>

        <button type="submit" class="btn-primary" :disabled="loading">
          {{ loading ? '处理中...' : (isLogin ? '登 录' : '注 册') }}
        </button>
      </form>

      <p class="switch">
        {{ isLogin ? '没有账号？' : '已有账号？' }}
        <a @click="toggleMode">{{ isLogin ? '立即注册' : '返回登录' }}</a>
      </p>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { login, register } from '../api'

const router = useRouter()
const auth = useAuthStore()

const isLogin = ref(true)
const username = ref('')
const password = ref('')
const passwordRef = ref(null)
const showPw = ref(false)
const loading = ref(false)
const error = ref('')

onMounted(() => {
  if (auth.isAuthenticated) router.replace('/')
})

function toggleMode() {
  isLogin.value = !isLogin.value
  error.value = ''
}

async function submit() {
  if (!username.value.trim() || !password.value) {
    error.value = '请填写用户名和密码'
    return
  }
  if (password.value.length < 6) {
    error.value = '密码至少 6 位'
    return
  }
  loading.value = true
  error.value = ''
  try {
    const data = await (isLogin.value ? login(username.value, password.value) : register(username.value, password.value))
    auth.setAuth(data.token, data.username)
    router.replace('/')
  } catch (e) {
    error.value = e.message
  }
  loading.value = false
}
</script>

<style scoped>
.auth-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(160deg, var(--color-surface) 0%, var(--color-primary-light) 100%);
}
.auth-card {
  background: var(--color-surface);
  padding: 40px 36px;
  border-radius: 16px;
  box-shadow: 0 8px 40px rgba(79, 70, 229, 0.08);
  width: 380px;
  max-width: 90vw;
  border: 1px solid var(--color-border);
}
.logo {
  text-align: center;
  margin-bottom: 8px;
}
.logo-box {
  width: 52px;
  height: 52px;
  margin: 0 auto 12px;
  border-radius: 14px;
  background: var(--color-primary-light);
  color: var(--color-primary);
  display: flex;
  align-items: center;
  justify-content: center;
}
.logo-box svg { width: 28px; height: 28px; }
.logo h1 { font-size: 22px; color: var(--color-text); font-weight: 700; }
.subtitle { text-align: center; color: var(--color-text-muted); font-size: 13px; margin-bottom: 28px; }
label { display: block; font-size: 13px; color: var(--color-text-secondary); margin: 14px 0 6px; }
input[type="text"], input[type="password"] {
  width: 100%;
  padding: 11px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  font-size: 14px;
  outline: none;
  transition: border-color var(--transition), box-shadow var(--transition);
  background: var(--color-sidebar-bg);
  color: var(--color-text);
}
input:focus {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.1);
  background: var(--color-surface);
}
.pw-wrap { position: relative; }
.pw-wrap input { padding-right: 40px; }
.pw-toggle {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  background: none;
  border: none;
  cursor: pointer;
  color: var(--color-text-muted);
  padding: 4px;
}
.pw-toggle:hover { color: var(--color-primary); }
.error { color: var(--color-error); font-size: 13px; margin-top: 10px; text-align: center; min-height: 20px; }
.btn-primary {
  width: 100%;
  padding: 12px;
  background: var(--color-primary);
  color: #fff;
  border: none;
  border-radius: 8px;
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  margin-top: 20px;
  transition: background var(--transition);
}
.btn-primary:hover:not(:disabled) { background: var(--color-primary-hover); }
.btn-primary:disabled { opacity: 0.6; cursor: not-allowed; }
.switch { text-align: center; margin-top: 16px; font-size: 13px; color: var(--color-text-muted); }
.switch a { color: var(--color-primary); cursor: pointer; text-decoration: none; font-weight: 500; }
.switch a:hover { text-decoration: underline; }
</style>
