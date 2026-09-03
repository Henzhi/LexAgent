<template>
  <div :class="['msg', message.role]">
    <!-- 思考过程折叠块 -->
    <div v-if="message.role === 'assistant' && message.thinking?.length" class="thinking-box">
      <button class="thinking-toggle" @click="thinkingCollapsed = !thinkingCollapsed">
        <svg :class="{ rotated: !thinkingCollapsed }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="9 18 15 12 9 6"/></svg>
        <span>已思考</span>
      </button>
      <div v-if="!thinkingCollapsed" class="thinking-traces">
        <div v-for="(t, i) in message.thinking" :key="i" class="trace-item" :class="`trace-${t?.kind || 'thinking'}`">{{ t?.text ?? t }}</div>
      </div>
    </div>

    <div class="bubble" v-html="renderedContent"></div>

    <!-- 引用条文：可折叠 -->
    <div v-if="sources.length" class="sources">
      <button class="src-toggle" @click="srcOpen = !srcOpen">
        <svg :class="{ rotated: srcOpen }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><polyline points="9 18 15 12 9 6"/></svg>
        <span>引用条文 · {{ sources.length }} 条</span>
        <!-- 来源构成汇总：一眼看清依据的可信度分布 -->
        <span v-if="sourceSummary" class="src-summary">{{ sourceSummary }}</span>
      </button>
      <ul v-if="srcOpen" class="src-list">
        <!-- 未验证线索警示：法律场景下须明确区分"可引用"与"仅供参考" -->
        <li v-if="unverifiedCount" class="src-caution">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
          <span>含 {{ unverifiedCount }} 条未经官方源验证的线索，仅供参考，不宜直接作为法律依据</span>
        </li>
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
            <!-- 来源与验证状态徽章（M2 / F10 引用溯源） -->
            <span
              v-if="veriMeta(s).label"
              class="src-badge"
              :class="`badge-${veriMeta(s).tone}`"
              :title="veriMeta(s).hint"
            >{{ veriMeta(s).label }}</span>
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
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'

const props = defineProps({
  message: { type: Object, required: true },
  thinking: { type: Boolean, default: false },
  sources: { type: Array, default: () => [] },
})

// 来源验证状态 → 展示配置（M2 / F10 引用溯源，字段由后端 fusion.py 产出）
// verification 缺失（旧会话/固定管线路径）时回退为空白，不显示徽章。
const VERIFICATION_META = {
  verified_internal: {
    label: '内部库', tone: 'internal',
    hint: '已收录于内部法律知识库，可直接作为法律依据',
  },
  verified_official: {
    label: '官方源', tone: 'official',
    hint: '来自国家法律法规数据库等官方源，已验证现行有效',
  },
  third_party: {
    label: '第三方', tone: 'third',
    hint: '来自第三方数据源，建议回源官方库二次核验',
  },
  web_unverified: {
    label: '网络未验证', tone: 'web',
    hint: '来自网络搜索，未经官方源验证，仅供参考',
  },
}

function veriMeta(s) {
  return VERIFICATION_META[s?.verification] || { label: '', tone: '', hint: '' }
}

// 标题栏来源构成汇总，例："6 官方源 · 2 内部库 · 1 网络未验证"
const sourceSummary = computed(() => {
  const counts = new Map()
  for (const s of props.sources) {
    const label = veriMeta(s).label
    if (!label) continue
    counts.set(label, (counts.get(label) || 0) + 1)
  }
  if (!counts.size) return ''
  // 按可信度排序：内部库 → 官方源 → 第三方 → 网络未验证
  const order = ['内部库', '官方源', '第三方', '网络未验证']
  return [...counts.entries()]
    .sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]))
    .map(([label, n]) => `${n} ${label}`)
    .join(' · ')
})

// 未验证线索条数（第三方 + 网络未验证），用于顶部警示
const unverifiedCount = computed(
  () => props.sources.filter((s) => ['third_party', 'web_unverified'].includes(s?.verification)).length,
)

const thinkingCollapsed = ref(true)
const srcOpen = ref(true)
// 已展开原文的条文索引（点击具体条文后才展示，避免直接堆出全部原文）
const expandedSources = ref([])

function toggleSrc(i) {
  const idx = expandedSources.value.indexOf(i)
  if (idx >= 0) expandedSources.value.splice(idx, 1)
  else expandedSources.value.push(i)
}

// Markdown 渲染：markdown-it + DOMPurify（2026-09-03 审查整改，替换自研渲染器）。
// 自研渲染器虽当前无注入点，但每加一种语法都要重新证明安全——09-01 之后加
// 标题/引用/列表时已经逼近这条线。换成成熟方案后：
// - markdown-it html:false：源文本里的原始 HTML 直接转义，不进解析树；
// - DOMPurify 再对输出做白名单消毒（双层防御，解析器漏洞也不至于变成 XSS）；
// - 链接统一 target=_blank + rel=noopener（防 reverse tabnabbing）。
const md = new MarkdownIt({
  html: false,      // 原始 HTML 一律转义
  linkify: true,    // 裸 URL 自动成链（引用来源常用）
  breaks: true,     // 单换行 → <br>（对话场景贴近原自研渲染器的逐行段落行为）
})

// 所有渲染出的链接：新窗口打开并断开 opener（DOMPurify 全局钩子，模块级注册一次）
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

const renderedContent = computed(() => {
  const raw = md.render(props.message.content || '')
  return DOMPurify.sanitize(raw, {
    // 白名单够用即可：法律问答不需要 img/iframe/视频等富媒体
    ALLOWED_TAGS: [
      'p', 'br', 'strong', 'em', 'del', 's', 'code', 'pre',
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
      'blockquote', 'ul', 'ol', 'li',
      'hr', 'a', 'span', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
    ],
    ALLOWED_ATTR: ['href', 'title', 'target', 'rel', 'class'],
  })
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

/* ---- Markdown 渲染元素（v-html 注入，须经 :deep 穿透 scoped）----
   markdown-it 输出标准标签（h3/h4/blockquote/ul/ol/hr），直接按标签选择器适配；
   标题从 h3 起映射由 CSS 缩放承担（h1/h2 同样式），避免与页面主标题层级冲突。 */
.msg .bubble :deep(p) { margin: 6px 0; }
.msg .bubble :deep(h1),
.msg .bubble :deep(h2),
.msg .bubble :deep(h3),
.msg .bubble :deep(h4),
.msg .bubble :deep(h5) {
  margin: 16px 0 8px;
  font-size: 16px;
  font-weight: 600;
  line-height: 1.5;
  color: var(--color-text);
}
.msg .bubble :deep(h1:first-child),
.msg .bubble :deep(h2:first-child),
.msg .bubble :deep(h3:first-child),
.msg .bubble :deep(h4:first-child),
.msg .bubble :deep(h5:first-child) { margin-top: 0; }
.msg .bubble :deep(blockquote) {
  margin: 8px 0;
  padding: 8px 14px;
  border-left: 3px solid var(--color-primary);
  background: var(--color-surface-soft, #f7f8fa);
  border-radius: 0 8px 8px 0;
  color: var(--color-text-secondary);
}
.msg .bubble :deep(blockquote p) { margin: 4px 0; }
.msg .bubble :deep(ul),
.msg .bubble :deep(ol) { margin: 6px 0; padding-left: 24px; }
.msg .bubble :deep(ul li),
.msg .bubble :deep(ol li) { margin: 4px 0; line-height: 1.7; }
.msg .bubble :deep(hr) {
  border: none;
  border-top: 1px solid var(--color-border);
  margin: 14px 0;
}
.msg .bubble :deep(a) { color: var(--color-primary); }
.msg .bubble :deep(code) {
  background: var(--color-surface-soft, #f2f3f5);
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 13px;
}
/* 用户消息气泡内不留段间距（保持紧凑气泡外观） */
.msg.user .bubble :deep(p) { margin: 0; }
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
/* 来源构成汇总：次要信息，弱化但可读 */
.src-summary {
  margin-left: auto;
  font-size: 12px;
  font-weight: 400;
  color: var(--color-text-muted);
  flex-shrink: 0;
  padding-right: 2px;
}
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

/* ---- 来源验证状态徽章（M2 / F10）----
   四态配色：内部库绿（已验证可引用）、官方源蓝（权威已验证）、
   第三方橙、网络未验证灰橙。双主题靠 CSS 变量自动适配。 */
.src-badge {
  flex-shrink: 0;
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  line-height: 1.6;
  white-space: nowrap;
  border: 1px solid transparent;
  cursor: help;
}
.badge-internal {
  color: var(--color-success);
  background: var(--color-success-light);
  border-color: color-mix(in srgb, var(--color-success) 30%, transparent);
}
.badge-official {
  color: var(--color-primary);
  background: var(--color-primary-light);
  border-color: var(--color-primary-border);
}
.badge-third {
  color: var(--color-warning);
  background: var(--color-warning-light);
  border-color: color-mix(in srgb, var(--color-warning) 35%, transparent);
}
.badge-web {
  color: var(--color-text-secondary);
  background: var(--color-surface-hover);
  border-color: var(--color-border);
}

/* 未验证线索警示条 */
.src-caution {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin-bottom: 4px;
  padding: 7px 10px;
  border-radius: 8px;
  background: var(--color-warning-light);
  border: 1px solid color-mix(in srgb, var(--color-warning) 30%, transparent);
  color: var(--color-warning);
  font-size: 12px;
  line-height: 1.5;
}
.src-caution svg { flex-shrink: 0; margin-top: 1px; }
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
.trace-item.trace-tool_call { color: var(--color-primary); font-weight: 500; }
.trace-item.trace-tool_result { color: var(--color-text-secondary); }
.trace-item.trace-tool_result_error { color: #dc2626; font-weight: 500; }
</style>
