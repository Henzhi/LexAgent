<template>
  <div :class="['msg', message.role]">
    <!-- 思考过程折叠块 -->
    <div v-if="message.role === 'assistant' && message.thinking?.length" class="thinking-box">
      <button class="thinking-toggle" @click="thinkingCollapsed = !thinkingCollapsed">
        <svg :class="{ rotated: !thinkingCollapsed }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="9 18 15 12 9 6"/></svg>
        <span>已思考</span>
      </button>
      <div v-if="!thinkingCollapsed" class="thinking-traces">
        <div v-for="(t, i) in message.thinking" :key="i" class="trace-item">{{ t }}</div>
      </div>
    </div>

    <div class="bubble" v-html="renderedContent"></div>

    <!-- 引用条文：可折叠 -->
    <div v-if="sources.length" class="sources">
      <button class="src-toggle" @click="srcOpen = !srcOpen">
        <svg :class="{ rotated: srcOpen }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><polyline points="9 18 15 12 9 6"/></svg>
        <span>引用条文 · {{ sources.length }} 条</span>
      </button>
      <ul v-if="srcOpen" class="src-list">
        <li v-for="(s, i) in sources" :key="i" class="src-item">
          <button
            class="src-head"
            :class="{ expandable: !!s.content }"
            @click="toggleSrc(i)"
            :aria-expanded="expandedSources.includes(i)"
          >
            <svg v-if="s.content" :class="{ rotated: expandedSources.includes(i) }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="11" height="11"><polyline points="9 18 15 12 9 6"/></svg>
            <span class="src-name">{{ s.law_name }}</span>
            <span class="src-citation">{{ s.citation }}</span>
            <span v-if="s.content" class="src-hint">{{ expandedSources.includes(i) ? '收起' : '查看原文' }}</span>
          </button>
          <div v-if="s.content && expandedSources.includes(i)" class="src-content">{{ s.content }}</div>
        </li>
      </ul>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'

const props = defineProps({
  message: { type: Object, required: true },
  thinking: { type: Boolean, default: false },
  sources: { type: Array, default: () => [] },
})

const thinkingCollapsed = ref(true)
const srcOpen = ref(true)
// 已展开原文的条文索引（点击具体条文后才展示，避免直接堆出全部原文）
const expandedSources = ref([])

function toggleSrc(i) {
  const idx = expandedSources.value.indexOf(i)
  if (idx >= 0) expandedSources.value.splice(idx, 1)
  else expandedSources.value.push(i)
}

// 先转义 HTML 特殊字符，再应用轻量 markdown 渲染，避免 LLM 输出注入 <script>/事件属性等（存储型 XSS）
function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

const renderedContent = computed(() => {
  const text = props.message.content || ''
  // 先转义：所有用户/LLM 可控内容均变为纯文本
  const escaped = escapeHtml(text)
  // 再应用白名单标记：**加粗** 与 换行
  return escaped
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\n/g, '<br>')
})
</script>

<style scoped>
.msg {
  margin-bottom: 28px;
  animation: fadeIn 0.3s ease;
}
@keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

/* 用户消息：右对齐气泡 */
.msg.user { text-align: right; }
.msg.user .bubble {
  background: var(--color-primary);
  color: #fff;
  display: inline-block;
  max-width: 72%;
  padding: 10px 16px;
  border-radius: 14px 14px 4px 14px;
  text-align: left;
  line-height: 1.6;
  font-size: 15px;
}

/* AI 消息：左侧全宽无气泡（DeepSeek 风格） */
.msg.assistant { text-align: left; }
.msg.assistant .bubble {
  color: var(--color-text);
  max-width: 100%;
  padding: 2px 0;
  line-height: 1.8;
  font-size: 15px;
  word-break: break-word;
}
.msg.assistant .bubble :deep(strong) { color: var(--color-text); font-weight: 600; }
.msg.assistant .bubble :deep(br) + :deep(br) { display: block; margin-top: 4px; }

/* 引用条文卡片 */
.sources {
  margin-top: 10px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: var(--color-sidebar-bg);
  overflow: hidden;
}
.src-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  width: 100%;
  padding: 10px 14px;
  font-size: 13px;
  color: var(--color-primary);
  font-weight: 500;
  cursor: pointer;
  transition: background 150ms ease;
  text-align: left;
}
.src-toggle:hover { background: var(--color-sidebar-hover); }
.src-toggle svg {
  transition: transform 150ms ease;
  width: 13px; height: 13px;
  flex-shrink: 0;
}
.src-toggle svg.rotated { transform: rotate(90deg); }
.src-list {
  padding: 4px 14px 14px;
  list-style: none;
  font-size: 13px;
  color: var(--color-text-secondary);
}
.src-item {
  padding: 8px 0;
  border-top: 1px solid var(--color-border);
  line-height: 1.6;
}
.src-item:first-child { border-top: none; }
.src-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 4px 0;
  background: none;
  border: none;
  cursor: pointer;
  text-align: left;
  border-radius: 6px;
}
.src-head.expandable:hover { background: rgba(79, 70, 229, 0.06); }
.src-head.expandable:hover .src-name { text-decoration: underline; }
.src-head svg {
  transition: transform 150ms ease;
  flex-shrink: 0;
  color: var(--color-text-muted);
}
.src-head svg.rotated { transform: rotate(90deg); }
.src-name {
  color: var(--color-text);
  font-weight: 600;
  white-space: nowrap;
}
.src-citation { color: var(--color-text-muted); }
.src-hint {
  margin-left: auto;
  font-size: 12px;
  color: var(--color-primary);
  flex-shrink: 0;
}
.src-content {
  margin-top: 6px;
  padding: 10px 12px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  color: var(--color-text);
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;   /* 保留法条换行 */
  word-break: break-word;
  max-height: 240px;
  overflow-y: auto;        /* 长法条可滚动 */
}

/* 思考过程折叠块 */
.thinking-box { margin-bottom: 8px; }
.thinking-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  color: var(--color-text-muted);
  font-size: 13px;
  cursor: pointer;
  padding: 4px 0;
  transition: color 150ms ease;
}
.thinking-toggle:hover { color: var(--color-primary); }
.thinking-toggle svg {
  transition: transform 150ms ease;
  width: 14px; height: 14px;
}
.thinking-toggle svg.rotated { transform: rotate(90deg); }
.thinking-traces {
  margin-top: 4px;
  padding: 10px 14px;
  background: var(--color-sidebar-bg);
  border-left: 3px solid var(--color-primary-border);
  border-radius: 0 8px 8px 0;
  font-size: 13px;
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.trace-item {
  padding: 2px 0;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
