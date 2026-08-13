<template>
  <div class="input-area">
    <div :class="['input-row', { disabled: disabled }]">
      <textarea
        v-model="text"
        :disabled="disabled"
        rows="1"
        :placeholder="disabled ? 'AI 正在回复中，请稍候...' : '输入法律问题，Enter 发送 / Shift+Enter 换行'"
        @keydown="onKeydown"
        @input="autoResize"
        ref="textareaRef"
      ></textarea>
      <button v-if="disabled" class="btn-stop" title="停止生成" @click="emit('stop')">
        <svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
      </button>
      <button v-else class="btn-send" @click="doSend" :disabled="!text.trim()" title="发送">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'

const props = defineProps({ disabled: Boolean })
const emit = defineEmits(['send', 'stop'])

const text = ref('')
const textareaRef = ref(null)

function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    doSend()
  }
}

function autoResize() {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 160) + 'px'
}

function doSend() {
  const q = text.value.trim()
  if (!q || props.disabled) return
  emit('send', q)
  text.value = ''
  if (textareaRef.value) {
    textareaRef.value.style.height = 'auto'
  }
}
</script>

<style scoped>
.input-area {
  max-width: 860px;
  margin: 0 auto;
  width: 100%;
  padding: 8px 20px 4px;
  flex-shrink: 0;
}

.input-row {
  display: flex;
  gap: 8px;
  align-items: flex-end;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 14px;
  padding: 10px 10px 10px 18px;
  box-shadow: var(--shadow-card);
  transition: border-color 150ms ease, box-shadow 150ms ease, opacity 200ms ease;
}
.input-row:focus-within {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.08), 0 2px 8px rgba(0, 0, 0, 0.04);
}
.input-row.disabled {
  cursor: not-allowed;
}
.input-row.disabled textarea {
  opacity: 0.6;
}

textarea {
  flex: 1;
  border: none;
  outline: none;
  background: transparent;
  font-size: 15px;
  line-height: 1.5;
  resize: none;
  padding: 6px 0;
  max-height: 160px;
  color: var(--color-text);
  cursor: text;
}
textarea:disabled {
  cursor: not-allowed;
  opacity: 0.7;
}
textarea::placeholder { color: var(--color-text-muted); }

.btn-send {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  border: none;
  background: var(--color-primary);
  color: #fff;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 150ms ease;
}
.btn-send:hover:not(:disabled) { background: var(--color-primary-hover); transform: scale(1.05); }
.btn-send:disabled { background: var(--color-border); color: var(--color-text-muted); cursor: not-allowed; }

.btn-stop {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  border: none;
  background: var(--color-danger, #ef4444);
  color: #fff;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 150ms ease;
}
.btn-stop:hover { background: var(--color-danger-hover, #dc2626); transform: scale(1.05); }
</style>
