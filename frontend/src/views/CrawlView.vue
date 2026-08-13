<template>
  <div class="crawl-page">
    <header class="page-header">
      <button class="btn-back" @click="$router.push('/')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
        返回问答
      </button>
      <h2>在线更新法律</h2>
      <div class="header-actions">
        <router-link to="/knowledge" class="btn-knowledge">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
          知识库管理
        </router-link>
      </div>
    </header>

    <p class="page-hint">
      数据源：全国人大「国家法律法规数据库」（flk.npc.gov.cn），请控制请求频率、仅用于学习/研究。
      增量去重：LawData 按官方文档 id（.crawl_manifest.json）、pgvector 按「标题 + 效力状态」判定，
      已存在的文档自动跳过，不会重复下载；副本自动保存到 LawData/ 目录。
    </p>

    <!-- 更新设置 -->
    <div class="panel">
      <h3>更新设置</h3>
      <div class="form-row">
        <label>
          文档类型
          <select v-model="crawlForm.doc_type">
            <option v-for="(label, key) in crawlTypes" :key="key" :value="key">{{ label }}</option>
          </select>
        </label>
        <label>
          输出目标
          <select v-model="crawlForm.store">
            <option value="both">pgvector + LawData 副本（推荐）</option>
            <option value="pg">仅 pgvector</option>
            <option value="txt">仅 LawData 文本副本</option>
          </select>
        </label>
        <label>
          最多条数
          <input v-model.number="crawlForm.limit" type="number" min="0" max="1000" />
          <span class="field-hint">0 = 不限</span>
        </label>
        <label>
          标题关键词
          <input v-model="crawlForm.keyword" placeholder="空 = 该类型全部，如：数据安全法" />
        </label>
      </div>
      <div class="crawl-options">
        <label class="checkbox-label">
          <input type="checkbox" v-model="crawlForm.force" /> 强制重爬（覆盖已存在文档）
        </label>
        <label class="checkbox-label">
          <input type="checkbox" v-model="crawlForm.rebuild" /> 爬完后重建向量索引
        </label>
      </div>
      <div class="crawl-actions">
        <button class="btn-crawl" @click="handleCrawl" :disabled="crawlRunning || !Object.keys(crawlTypes).length">
          {{ crawlRunning ? '爬取中...' : '开始增量更新' }}
        </button>
        <span v-if="crawlMsg" class="success">{{ crawlMsg }}</span>
        <span v-if="crawlErr" class="error">{{ crawlErr }}</span>
      </div>
    </div>

    <!-- 当前任务进度 -->
    <div v-if="currentTask" class="panel">
      <h3>当前任务进度</h3>
      <div class="crawl-task">
        <div class="task-row">
          <span class="task-id">任务 {{ currentTask.task_id }}</span>
          <span class="task-status" :class="currentTask.status">{{ crawlStatusText(currentTask.status) }}</span>
        </div>
        <div class="crawl-progress">
          <span>命中 {{ currentTask.progress.total }}</span>
          <span class="crawl-add">新增 {{ currentTask.progress.added }}</span>
          <span class="crawl-upd">更新 {{ currentTask.progress.updated }}</span>
          <span>跳过 {{ currentTask.progress.skipped }}</span>
          <span class="crawl-fail">失败 {{ currentTask.progress.failed }}</span>
        </div>
        <div v-if="currentTask.rebuild" class="crawl-rebuild">
          HNSW 索引重建：{{ currentTask.rebuild === 'done' ? '完成' : currentTask.rebuild }}
        </div>
        <div v-if="currentTask.errors && currentTask.errors.length" class="task-errors">
          <div v-for="(e, i) in currentTask.errors.slice(0, 10)" :key="i" class="task-error">- {{ e }}</div>
          <div v-if="currentTask.errors.length > 10" class="task-error">… 共 {{ currentTask.errors.length }} 条错误</div>
        </div>
      </div>
    </div>

    <!-- 本次会话的任务记录 -->
    <div v-if="tasks.length" class="panel">
      <h3>本次会话任务记录</h3>
      <div v-for="t in tasks" :key="t.task_id" class="history-item">
        <div class="task-row">
          <div class="history-meta">
            <span class="task-id">#{{ t.task_id }}</span>
            <span class="history-type">{{ typeLabel(t.doc_type) }}</span>
            <span v-if="t.keyword" class="history-kw">「{{ t.keyword }}」</span>
            <span class="history-store">{{ storeLabel(t.store) }}</span>
          </div>
          <span class="task-status" :class="t.status">{{ crawlStatusText(t.status) }}</span>
        </div>
        <div v-if="t.progress" class="crawl-progress">
          <span>命中 {{ t.progress.total }}</span>
          <span class="crawl-add">新增 {{ t.progress.added }}</span>
          <span class="crawl-upd">更新 {{ t.progress.updated }}</span>
          <span>跳过 {{ t.progress.skipped }}</span>
          <span class="crawl-fail">失败 {{ t.progress.failed }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { listCrawlTypes, startCrawl, getCrawlStatus } from '../api'

const crawlTypes = ref({})
const crawlForm = ref({ doc_type: 'all', store: 'both', limit: 50, keyword: '', force: false, rebuild: false })
const crawlRunning = ref(false)
const crawlMsg = ref('')
const crawlErr = ref('')
const tasks = ref([])          // 本次会话提交的所有任务
const currentTask = ref(null)  // 最近一个仍在进行/刚结束的任务
const timers = new Map()

async function loadCrawlTypes() {
  try {
    const res = await listCrawlTypes()
    crawlTypes.value = res.types || {}
  } catch (e) {
    crawlErr.value = '加载类型列表失败: ' + e.message
  }
}

async function handleCrawl() {
  crawlRunning.value = true
  crawlMsg.value = ''
  crawlErr.value = ''
  try {
    const res = await startCrawl(crawlForm.value)
    const task = {
      task_id: res.task_id,
      status: res.status || 'pending',
      doc_type: crawlForm.value.doc_type,
      keyword: crawlForm.value.keyword,
      store: crawlForm.value.store,
      progress: { total: 0, added: 0, updated: 0, skipped: 0, failed: 0 },
      errors: [],
      rebuild: null,
    }
    tasks.value.unshift(task)
    currentTask.value = task
    crawlMsg.value = '任务已提交，正在增量更新...'
    pollTask(task)
  } catch (e) {
    crawlErr.value = e.message
  } finally {
    crawlRunning.value = false
  }
}

function pollTask(task) {
  if (timers.has(task.task_id)) return
  const timer = setInterval(async () => {
    try {
      const s = await getCrawlStatus(task.task_id)
      task.status = s.status
      task.progress = s.progress || task.progress
      task.errors = s.errors || []
      task.rebuild = s.rebuild
      if (s.status === 'done' || s.status === 'error' || s.status === 'failed') {
        clearInterval(timer)
        timers.delete(task.task_id)
        crawlRunning.value = false
        crawlMsg.value = s.status === 'done' ? '更新完成，可前往知识库查看' : '更新失败'
      }
    } catch { /* 瞬时错误下轮重试 */ }
  }, 3000)
}

function crawlStatusText(s) {
  return { pending: '等待中', running: '爬取中', done: '已完成', error: '失败' }[s] || s
}

function typeLabel(t) {
  return {
    law: '法律', regulation: '行政法规',
    judicial_interpretation: '司法解释', local_regulation: '地方性法规',
    constitution: '宪法', supervision: '监察法规',
    auto: '自动分类', all: '全部类型',
  }[t] || t
}

function storeLabel(s) {
  return { both: 'pgvector+副本', pg: 'pgvector', txt: 'LawData 副本' }[s] || s
}

onMounted(() => {
  loadCrawlTypes()
})

onBeforeUnmount(() => {
  timers.forEach((t) => clearInterval(t))
  timers.clear()
})
</script>

<style scoped>
.crawl-page { max-width: 1440px; margin: 0 auto; padding: 24px; }

.page-header { display: flex; align-items: center; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
.page-header h2 { font-size: 22px; flex: 1; }
.header-actions { display: flex; gap: 8px; }
.btn-back { background: none; border: 1px solid var(--color-border); border-radius: var(--radius); padding: 7px 14px; cursor: pointer; color: var(--color-text-secondary); font-size: 14px; display: inline-flex; align-items: center; gap: 6px; transition: all var(--transition); }
.btn-back:hover { background: var(--color-sidebar-hover); color: var(--color-primary); }
.btn-knowledge { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; background: var(--color-surface-soft); border: 1px solid var(--color-border); border-radius: var(--radius); color: var(--color-text-secondary); font-size: 14px; text-decoration: none; transition: all var(--transition); }
.btn-knowledge:hover { color: var(--color-primary); border-color: var(--color-primary-border); background: var(--color-primary-light); }

.page-hint { color: var(--color-text-muted); font-size: 13px; line-height: 1.8; background: var(--color-surface-soft); border: 1px solid var(--color-border); border-radius: var(--radius); padding: 12px 16px; margin-bottom: 20px; }

.panel { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-lg); padding: 24px; margin-bottom: 20px; }
.panel h3 { font-size: 17px; margin-bottom: 16px; }

.form-row { display: flex; gap: 16px; flex-wrap: wrap; }
.form-row label { flex: 1; min-width: 180px; display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--color-text-muted); }
.form-row input, .form-row select { padding: 8px 12px; border: 1px solid var(--color-border); border-radius: var(--radius); font-size: 14px; background: var(--color-surface-soft); color: var(--color-text); }
.field-hint { color: var(--color-text-muted); font-size: 12px; }

.crawl-options { display: flex; gap: 20px; flex-wrap: wrap; font-size: 13px; color: var(--color-text-secondary); margin: 16px 0; }
.checkbox-label { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
.checkbox-label input { accent-color: var(--color-primary); }
.crawl-actions { display: flex; align-items: center; gap: 12px; }
.btn-crawl { padding: 10px 28px; background: var(--color-primary); color: #fff; border: none; border-radius: var(--radius); font-size: 15px; cursor: pointer; }
.btn-crawl:disabled { opacity: 0.5; cursor: not-allowed; }

.success { color: var(--color-success); font-size: 14px; }
.error { color: var(--color-error); font-size: 14px; }

.crawl-task { display: flex; flex-direction: column; gap: 10px; }
.task-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.task-id { font-family: monospace; font-size: 13px; color: var(--color-text-secondary); }
.task-status { font-size: 12px; padding: 3px 10px; border-radius: 10px; background: var(--color-primary-light); color: var(--color-primary); white-space: nowrap; }
.task-status.done { background: #D1FAE5; color: #065F46; }
.task-status.error, .task-status.failed { background: #FEE2E2; color: #991B1B; }
.crawl-progress { display: flex; gap: 16px; flex-wrap: wrap; font-size: 13px; color: var(--color-text-secondary); }
.crawl-add { color: #059669; }
.crawl-upd { color: var(--color-primary); }
.crawl-fail { color: var(--color-error); }
.crawl-rebuild { font-size: 13px; color: var(--color-text-muted); }
.task-errors { display: flex; flex-direction: column; gap: 2px; }
.task-error { font-size: 12px; color: var(--color-error); }

.history-item { padding: 12px 0; border-bottom: 1px dashed var(--color-border); display: flex; flex-direction: column; gap: 8px; }
.history-item:last-child { border-bottom: none; }
.history-meta { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.history-type { font-size: 12px; padding: 2px 10px; border-radius: 10px; background: var(--color-primary-light); color: var(--color-primary); }
.history-kw { font-size: 13px; color: var(--color-text-secondary); }
.history-store { font-size: 12px; color: var(--color-text-muted); }

/* 暗色模式下拉列表可读性 */
:root[data-theme='dark'] .form-row select option {
  background: var(--color-surface-soft);
  color: var(--color-text);
}
</style>
