<template>
  <aside :class="['sidebar', { collapsed: !open }]">
    <!-- New Chat -->
    <button class="btn-new-chat" @click="$emit('new-chat')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
      <span v-if="open">新建对话</span>
    </button>

    <!-- Session list -->
    <div class="session-list">
      <div v-if="open && sessions.length === 0" class="empty">暂无对话记录</div>
      <button
        v-for="s in sessions"
        :key="s.session_id"
        :class="['session-item', { active: s.session_id === activeId }]"
        :title="open ? s.first_msg || '新对话' : ''"
        @click="$emit('select', s.session_id)"
        @contextmenu.prevent="onContextMenu($event, s)"
      >
        <svg class="session-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="15" height="15"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
        <span v-if="open" class="session-label">{{ s.first_msg || '新对话' }}</span>
      </button>
    </div>

    <!-- Bottom: user area -->
    <div v-if="open" class="user-area">
      <div class="user-info">
        <div class="avatar">{{ avatarChar }}</div>
        <span class="username" :title="username">{{ username || '用户' }}</span>
      </div>
      <button class="btn-theme" @click="toggleTheme" :title="isDark ? '切换到浅色模式' : '切换到深色模式'">
        <!-- 月亮（切到深色） / 太阳（切到浅色） -->
        <svg v-if="!isDark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
        <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="6.34" y2="6.34"/><line x1="17.66" y1="17.66" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="6.34" y2="17.66"/><line x1="17.66" y1="6.34" x2="19.07" y2="4.93"/></svg>
      </button>
      <button class="btn-logout" @click="$emit('logout')" title="退出登录">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
      </button>
    </div>

    <!-- Collapse toggle -->
    <button class="toggle-btn" @click="$emit('toggle')" :title="open ? '收起侧栏' : '展开侧栏'">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
        <polyline v-if="open" points="15 18 9 12 15 6" />
        <polyline v-else points="9 18 15 12 9 6" />
      </svg>
    </button>

    <!-- Context menu -->
    <div v-if="ctxMenu.visible" class="ctx-menu" :style="{ top: ctxMenu.y + 'px', left: ctxMenu.x + 'px' }">
      <button @click="askDelete">删除对话</button>
    </div>

    <!-- Delete confirm modal -->
    <div v-if="confirmOpen" class="modal-mask" @click.self="cancelDelete">
      <div class="modal">
        <p class="modal-title">确定删除此对话？</p>
        <p class="modal-desc">删除后该对话的历史记录将无法恢复。</p>
        <div class="modal-actions">
          <button class="btn-cancel" @click="cancelDelete">取消</button>
          <button class="btn-danger" @click="confirmDelete">删除</button>
        </div>
      </div>
    </div>
  </aside>
</template>

<script setup>
import { reactive, ref, computed, onMounted, onUnmounted } from 'vue'

// 主题切换：写 documentElement[data-theme] + localStorage，与 index.html 防闪烁脚本保持一致
const isDark = ref((document.documentElement.getAttribute('data-theme') || 'light') === 'dark')
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme)
  isDark.value = theme === 'dark'
}
function toggleTheme() {
  const next = isDark.value ? 'light' : 'dark'
  try { localStorage.setItem('lawrag_theme', next) } catch { /* ignore */ }
  applyTheme(next)
}

const props = defineProps({
  sessions: { type: Array, default: () => [] },
  activeId: { type: String, default: '' },
  open: { type: Boolean, default: true },
  username: { type: String, default: '' },
})
const emit = defineEmits(['new-chat', 'select', 'toggle', 'delete', 'logout'])

const avatarChar = computed(() => (props.username || 'U').charAt(0).toUpperCase())

const ctxMenu = reactive({ visible: false, x: 0, y: 0, session: null })
const confirmOpen = ref(false)

function onContextMenu(e, session) {
  ctxMenu.visible = true
  ctxMenu.x = e.clientX
  ctxMenu.y = e.clientY
  ctxMenu.session = session
}
function closeMenu() { ctxMenu.visible = false }
function askDelete() {
  closeMenu()
  confirmOpen.value = true
}
function cancelDelete() {
  confirmOpen.value = false
}
function confirmDelete() {
  confirmOpen.value = false
  if (ctxMenu.session) {
    emit('delete', ctxMenu.session.session_id)
  }
}
onMounted(() => document.addEventListener('click', closeMenu))
onUnmounted(() => document.removeEventListener('click', closeMenu))
</script>

<style scoped>
.sidebar {
  position: relative;
  background: var(--color-sidebar-bg);
  border-right: 1px solid var(--color-border);
  color: var(--color-text-secondary);
  display: flex;
  flex-direction: column;
  transition: width 200ms ease;
  width: 260px;
  flex-shrink: 0;
  overflow: hidden;
}
.sidebar.collapsed { width: 56px; }

.btn-new-chat {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 11px 14px;
  margin: 12px 12px 4px;
  width: calc(100% - 24px);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  color: var(--color-text);
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: all var(--transition);
  white-space: nowrap;
  box-shadow: 0 1px 2px rgba(0,0,0,0.03);
}
.btn-new-chat:hover { background: var(--color-surface); border-color: var(--color-text-muted); box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
.btn-new-chat svg { color: var(--color-primary); flex-shrink: 0; }
.collapsed .btn-new-chat { justify-content: center; padding: 11px; }

.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 8px;
}
.empty { padding: 24px 8px; font-size: 13px; color: var(--color-text-muted); text-align: center; }

.session-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  background: none;
  border: none;
  border-radius: 8px;
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 14px;
  text-align: left;
  transition: all var(--transition);
  margin-bottom: 2px;
  white-space: nowrap;
}
.session-item:hover { background: var(--color-sidebar-hover); color: var(--color-text); }
.session-item.active { background: var(--color-primary-light); color: var(--color-primary); font-weight: 500; }
.session-icon { flex-shrink: 0; color: var(--color-text-muted); }
.session-item.active .session-icon { color: var(--color-primary); }
.session-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}

/* Bottom user area */
.user-area {
  border-top: 1px solid var(--color-border);
  padding: 10px 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.user-info { flex: 1; display: flex; align-items: center; gap: 10px; min-width: 0; }
.avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: var(--color-primary);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  font-weight: 600;
  flex-shrink: 0;
}
.username {
  font-size: 14px;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.btn-logout {
  flex-shrink: 0;
  background: none;
  border: none;
  color: var(--color-text-muted);
  cursor: pointer;
  padding: 6px;
  border-radius: 6px;
  transition: all var(--transition);
  display: flex;
}
.btn-logout:hover { color: var(--color-error); background: var(--color-error-light); }
.btn-theme {
  flex-shrink: 0;
  background: none;
  border: none;
  color: var(--color-text-muted);
  cursor: pointer;
  padding: 6px;
  border-radius: 6px;
  transition: all var(--transition);
  display: flex;
}
.btn-theme:hover { color: var(--color-primary); background: var(--color-sidebar-hover); }

.toggle-btn {
  position: absolute;
  bottom: 72px;
  right: -12px;
  width: 24px;
  height: 40px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-left: none;
  border-radius: 0 8px 8px 0;
  color: var(--color-text-muted);
  cursor: pointer;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--transition);
  box-shadow: 1px 0 3px rgba(0,0,0,0.04);
}
.toggle-btn:hover { color: var(--color-primary); }

.ctx-menu {
  position: fixed;
  z-index: 100;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  box-shadow: var(--shadow-pop);
  padding: 4px;
  min-width: 120px;
}
.ctx-menu button {
  width: 100%;
  padding: 8px 14px;
  background: none;
  border: none;
  border-radius: 6px;
  color: var(--color-error);
  cursor: pointer;
  font-size: 13px;
  text-align: left;
}
.ctx-menu button:hover { background: var(--color-error-light); }

.modal-mask {
  position: fixed;
  inset: 0;
  z-index: 200;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
}
.modal {
  width: 320px;
  background: var(--color-surface);
  border-radius: 12px;
  padding: 24px;
  box-shadow: var(--shadow-pop);
}
.modal-title { margin: 0 0 8px; font-size: 16px; color: var(--color-text); font-weight: 600; }
.modal-desc { margin: 0 0 20px; font-size: 13px; color: var(--color-text-muted); line-height: 1.5; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; }
.modal-actions button {
  padding: 8px 18px;
  border-radius: 8px;
  font-size: 14px;
  cursor: pointer;
  border: 1px solid transparent;
}
.btn-cancel { background: var(--color-sidebar-bg); color: var(--color-text-secondary); }
.btn-cancel:hover { background: var(--color-sidebar-hover); }
.btn-danger { background: var(--color-error); color: #fff; }
.btn-danger:hover { background: #B91C1C; }
</style>
