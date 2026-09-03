<template>
  <div class="usage-page">
    <header class="page-header">
      <button class="btn-back" @click="$router.push('/')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
        返回问答
      </button>
      <h2>用量与计费</h2>
      <div class="header-actions">
        <button class="btn-refresh" @click="loadAll(true)" :disabled="loading">刷新</button>
        <button class="btn-price" @click="priceOpen = !priceOpen">价格设置</button>
      </div>
    </header>

    <!-- 顶部 KPI -->
    <section class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">今日 LLM tokens</div>
        <div class="kpi-value">{{ fmtTokens(today.tokens_in) }}</div>
        <div class="kpi-sub">输入 {{ fmtTokens(today.tokens_in) }} / 输出 {{ fmtTokens(today.tokens_out) }}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">今日估算费用</div>
        <div class="kpi-value">{{ fmtYuan(today.cost_cny) }}</div>
        <div class="kpi-sub" :class="{ warn: today.est_cost > 0 }">
          其中估算 {{ fmtYuan(today.est_cost) }}{{ today.est_cost > 0 ? '（部分未取到 usage）' : '' }}
        </div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">调用次数</div>
        <div class="kpi-value">{{ today.llm_calls }}<span class="kpi-unit"> LLM</span></div>
        <div class="kpi-sub">
          Tavily {{ today.tavily_calls }} 次 · 法宝 {{ today.pkulaw_calls }} 次
        </div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">熔断状态</div>
        <div class="kpi-value" :class="budget.exceeded ? 'text-danger' : 'text-ok'">
          {{ budget.exceeded ? '已熔断' : '正常' }}
        </div>
        <div class="kpi-sub">
          LLM {{ budget.detail?.llm?.used ?? 0 }}/{{ budget.detail?.llm?.limit ?? '∞' }} 次 ·
          {{ budget.detail?.llm?.exceeded ? '超限' : '未超' }}
        </div>
      </div>
    </section>

    <!-- 7 日趋势 + 构成 -->
    <section class="two-col">
      <div class="card">
        <h3 class="card-title">近 {{ days }} 日费用（元）</h3>
        <div class="bar-chart">
          <div v-for="row in summary" :key="row.day" class="bar-col">
            <div class="bar-track">
              <div class="bar-fill" :style="{ height: barHeight(row) }" :title="`${row.day} ¥${fmtYuan(row.cost_cny)}`"></div>
            </div>
            <div class="bar-label">{{ row.day.slice(5) }}</div>
            <div class="bar-cost">{{ fmtYuan(row.cost_cny) }}</div>
          </div>
        </div>
      </div>
      <div class="card">
        <h3 class="card-title">近 {{ days }} 日构成（按来源）</h3>
        <div v-if="!breakdown.length" class="empty-hint">暂无用量数据</div>
        <div v-else class="breakdown-list">
          <div v-for="b in breakdown" :key="b.key" class="break-row">
            <div class="break-head">
              <span class="break-key">{{ sourceLabel(b.key) }}</span>
              <span class="break-amt">{{ fmtYuan(b.cost_cny) }} · {{ b.calls }} 次</span>
            </div>
            <div class="break-track">
              <div class="break-fill" :style="{ width: shareOf(b) }"></div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- 明细 -->
    <section class="card">
      <div class="card-head-row">
        <h3 class="card-title">最近调用明细</h3>
        <div class="head-right">
          <label class="day-pick">
            日期
            <input v-model="detailDay" type="date" @change="loadDetail()" />
          </label>
          <button class="btn-mini" @click="loadDetail()">查询</button>
        </div>
      </div>
      <div v-if="!detail.length" class="empty-hint">该日期暂无调用记录</div>
      <div v-else class="detail-table-wrap">
        <table class="detail-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>来源</th>
              <th>模型 / 工具</th>
              <th>tokens</th>
              <th>credits</th>
              <th>费用</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in detail" :key="i">
              <td class="mono">{{ fmtTs(row.ts) }}</td>
              <td>{{ sourceLabel(row.source) }}</td>
              <td>
                {{ row.model }}
                <span v-if="row.tool" class="chip">{{ row.tool }}</span>
                <span v-if="row.est" class="chip chip-warn" title="未取到真实 usage，token 为估算">est</span>
              </td>
              <td class="mono">{{ row.total_tokens ? fmtTokens(row.total_tokens) : '—' }}</td>
              <td class="mono">{{ row.credits ? row.credits : '—' }}</td>
              <td class="mono">{{ fmtYuan(row.cost_cny) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 价格设置抽屉 -->
    <div v-if="priceOpen" class="drawer-mask" @click.self="priceOpen = false">
      <div class="drawer">
        <div class="drawer-head">
          <h3>价格设置</h3>
          <div class="drawer-actions">
            <button class="btn-mini" @click="resetPrices" :disabled="savingPrice">恢复默认</button>
            <button class="btn-close" @click="priceOpen = false">×</button>
          </div>
        </div>
        <p class="page-hint">金额按「当时价格」算好快照落库，改价只影响之后的调用；历史费用不漂移。</p>
        <div class="price-table-wrap">
          <table class="price-table">
            <thead>
              <tr><th>键</th><th>单价</th><th>单位</th><th>来源</th></tr>
            </thead>
            <tbody>
              <tr v-for="p in pricingList" :key="p.key">
                <td class="mono price-key" :title="p.key">{{ p.key }}</td>
                <td>
                  <input v-model.number="p.value" type="number" step="0.000001" min="0" class="price-input" />
                </td>
                <td>{{ p.unit }}</td>
                <td>
                  <span class="chip" :class="p.source === 'db' ? 'chip-db' : ''">{{ p.source }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="drawer-foot">
          <span class="save-msg">{{ saveMsg }}</span>
          <button class="btn-primary" @click="savePrices" :disabled="savingPrice">保存修改</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import {
  getBudgetStatus,
  getUsageBreakdown,
  getUsageDetail,
  getUsagePricing,
  getUsageSummary,
  updateUsagePricing,
} from '../api'

const days = ref(7)
const loading = ref(false)
const summary = ref([])
const today = reactive({ tokens_in: 0, tokens_out: 0, cost_cny: 0, est_cost: 0, llm_calls: 0, tavily_calls: 0, pkulaw_calls: 0 })
const breakdown = ref([])
const budget = reactive({ exceeded: false, detail: {} })
const detail = ref([])
const detailDay = ref(new Date().toISOString().slice(0, 10))
const priceOpen = ref(false)
const pricingList = ref([])
const savingPrice = ref(false)
const saveMsg = ref('')

function fmtTokens(n) {
  n = Number(n) || 0
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K'
  return String(n)
}
function fmtYuan(n) {
  n = Number(n) || 0
  if (n < 0.01 && n > 0) return '¥' + n.toFixed(4)
  return '¥' + n.toFixed(2)
}
function fmtTs(ts) {
  if (!ts) return '—'
  return String(ts).replace('T', ' ').slice(0, 19)
}
function sourceLabel(key) {
  return { llm: 'LLM', tavily: 'Tavily', pkulaw: '北大法宝' }[key] || key
}

async function loadSummary() {
  try {
    const r = await getUsageSummary(days.value)
    summary.value = (r.items || []).slice(-days.value)
    const last = summary.value[summary.value.length - 1]
    if (last) Object.assign(today, {
      tokens_in: last.tokens_in,
      tokens_out: last.tokens_out,
      cost_cny: last.cost_cny,
      est_cost: last.est_cost,
      llm_calls: last.llm_calls,
      tavily_calls: last.tavily_calls,
      pkulaw_calls: last.pkulaw_calls,
    })
  } catch (e) { /* 401 已由拦截器处理 */ }
}

async function loadBreakdown() {
  try {
    const r = await getUsageBreakdown(days.value, 'source')
    breakdown.value = r.items || []
  } catch (e) { /* ignore */ }
}

async function loadBudget() {
  try {
    const r = await getBudgetStatus()
    Object.assign(budget, { exceeded: !!r.exceeded, detail: r.detail || {} })
  } catch (e) { /* ignore */ }
}

async function loadDetail() {
  try {
    const r = await getUsageDetail({ day: detailDay.value || undefined, limit: 50 })
    detail.value = r.items || []
  } catch (e) { /* ignore */ }
}

async function loadAll(showSpinner = false) {
  if (showSpinner) loading.value = true
  try {
    await Promise.all([loadSummary(), loadBreakdown(), loadBudget()])
    await loadDetail()
  } finally {
    loading.value = false
  }
}

function barHeight(row) {
  const max = Math.max(...summary.value.map((r) => Number(r.cost_cny) || 0), 0.0001)
  const h = max > 0 ? (Number(row.cost_cny) || 0) / max : 0
  return Math.max(h * 100, 1) + '%'
}
function shareOf(b) {
  const total = breakdown.value.reduce((s, x) => s + (Number(x.cost_cny) || 0), 0) || 1
  return ((Number(b.cost_cny) || 0) / total) * 100 + '%'
}

async function openPricing() {
  priceOpen.value = true
  try {
    const r = await getUsagePricing()
    pricingList.value = (r.items || []).map((p) => ({ ...p }))
  } catch (e) { /* ignore */ }
}

async function savePrices() {
  savingPrice.value = true
  saveMsg.value = ''
  try {
    const items = pricingList.value
      .filter((p) => p.source === 'db' || true) // 全量提交，后端只认已知键
      .map((p) => ({ key: p.key, value: p.value }))
    const r = await updateUsagePricing(items)
    saveMsg.value = `已保存 ${r.updated} 项，之后的新调用按新价计`
    await Promise.all([loadSummary(), loadBreakdown()])
  } catch (e) {
    saveMsg.value = '保存失败'
  } finally {
    savingPrice.value = false
  }
}

async function resetPrices() {
  if (!window.confirm('恢复默认价格？会清除所有价格覆盖。')) return
  savingPrice.value = true
  try {
    const items = pricingList.value
      .filter((p) => p.source === 'db')
      .map((p) => ({ key: p.key, value: p.value }))
    // 恢复默认 = 把 db 覆盖值删掉 → 后端无 DELETE 单键，用设置成默认值实现；
    // 简单方案：重新拉默认（接口返回 source=default 的值），把 db 项改回默认值。
    await Promise.all(
      pricingList.value
        .filter((p) => p.source === 'db')
        .map(async (p) => {
          const fresh = await getUsagePricing()
          const def = (fresh.items || []).find((x) => x.key === p.key)
          if (def) await updateUsagePricing([{ key: p.key, value: def.value }])
        })
    )
    saveMsg.value = '已恢复默认价格'
    await loadAll()
  } finally {
    savingPrice.value = false
  }
}

onMounted(() => loadAll(true))
</script>

<style scoped>
.usage-page { max-width: 1440px; margin: 0 auto; padding: 24px; }

.page-header { display: flex; align-items: center; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
.page-header h2 { font-size: 22px; flex: 1; }
.header-actions { display: flex; gap: 8px; }
.btn-back { background: none; border: 1px solid var(--color-border); border-radius: var(--radius); padding: 7px 14px; cursor: pointer; color: var(--color-text-secondary); font-size: 14px; display: inline-flex; align-items: center; gap: 6px; transition: all var(--transition); }
.btn-back:hover { background: var(--color-sidebar-hover); color: var(--color-primary); }
.btn-refresh, .btn-price, .btn-mini { padding: 8px 14px; border: 1px solid var(--color-border); border-radius: var(--radius); background: var(--color-surface-soft); color: var(--color-text-secondary); font-size: 14px; cursor: pointer; transition: all var(--transition); }
.btn-refresh:hover, .btn-price:hover, .btn-mini:hover { color: var(--color-primary); border-color: var(--color-primary-border); }
.btn-primary { padding: 9px 20px; border: none; border-radius: var(--radius); background: var(--color-primary); color: #fff; font-size: 14px; cursor: pointer; }
.btn-primary:hover { background: var(--color-primary-hover); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }

.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 20px; }
.kpi-card { background: var(--color-surface-soft); border: 1px solid var(--color-border); border-radius: var(--radius-lg); padding: 16px 18px; }
.kpi-label { font-size: 13px; color: var(--color-text-muted); }
.kpi-value { font-size: 26px; font-weight: 600; margin: 6px 0 2px; font-variant-numeric: tabular-nums; }
.kpi-unit { font-size: 14px; color: var(--color-text-muted); font-weight: 400; }
.kpi-sub { font-size: 12px; color: var(--color-text-muted); }
.text-ok { color: var(--color-success); }
.text-danger { color: var(--color-danger); }
.warn { color: var(--color-warning); }

.two-col { display: grid; grid-template-columns: 1.5fr 1fr; gap: 14px; margin-bottom: 20px; }
@media (max-width: 900px) { .two-col { grid-template-columns: 1fr; } }
.card { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-lg); padding: 18px 20px; margin-bottom: 20px; }
.card-title { font-size: 15px; font-weight: 600; margin: 0 0 14px; }
.card-head-row { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
.head-right { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--color-text-secondary); }
.day-pick input { padding: 5px 8px; border: 1px solid var(--color-border); border-radius: var(--radius); background: var(--color-surface); color: var(--color-text); font-size: 13px; margin-left: 6px; }

.bar-chart { display: flex; align-items: flex-end; gap: 10px; height: 170px; padding-top: 8px; }
.bar-col { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; min-width: 0; }
.bar-track { width: 60%; max-width: 42px; height: 120px; background: var(--color-surface-soft); border-radius: 4px; display: flex; align-items: flex-end; overflow: hidden; }
.bar-fill { width: 100%; background: var(--color-primary); border-radius: 4px 4px 0 0; min-height: 1px; transition: height 0.3s ease; }
.bar-label { font-size: 11px; color: var(--color-text-muted); margin-top: 4px; }
.bar-cost { font-size: 11px; color: var(--color-text-secondary); font-variant-numeric: tabular-nums; }

.empty-hint { color: var(--color-text-muted); font-size: 13px; padding: 18px 0; text-align: center; }
.breakdown-list { display: flex; flex-direction: column; gap: 12px; }
.break-row { display: flex; flex-direction: column; gap: 4px; }
.break-head { display: flex; justify-content: space-between; font-size: 13px; }
.break-key { font-weight: 500; }
.break-amt { color: var(--color-text-secondary); font-variant-numeric: tabular-nums; }
.break-track { height: 8px; background: var(--color-surface-soft); border-radius: 4px; overflow: hidden; }
.break-fill { height: 100%; background: var(--color-primary); border-radius: 4px; }

.detail-table-wrap, .price-table-wrap { overflow-x: auto; }
.detail-table, .price-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.detail-table th, .price-table th { text-align: left; color: var(--color-text-muted); font-weight: 500; padding: 8px 10px; border-bottom: 1px solid var(--color-border); white-space: nowrap; }
.detail-table td, .price-table td { padding: 8px 10px; border-bottom: 1px solid var(--color-border-light); white-space: nowrap; }
.mono { font-variant-numeric: tabular-nums; }
.price-key { max-width: 260px; overflow: hidden; text-overflow: ellipsis; }
.chip { display: inline-block; background: var(--color-surface-soft); border: 1px solid var(--color-border); border-radius: 10px; padding: 1px 8px; font-size: 11px; color: var(--color-text-secondary); margin-left: 4px; }
.chip-warn { color: var(--color-warning); border-color: var(--color-warning); }
.chip-db { color: var(--color-primary); border-color: var(--color-primary-border); }

.drawer-mask { position: fixed; inset: 0; background: rgba(0,0,0,0.35); z-index: 100; }
.drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(560px, 92vw); background: var(--color-surface); padding: 20px; overflow-y: auto; box-shadow: -8px 0 24px rgba(0,0,0,0.12); }
.drawer-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.drawer-actions { display: flex; gap: 8px; align-items: center; }
.btn-close { border: none; background: none; font-size: 22px; cursor: pointer; color: var(--color-text-muted); line-height: 1; }
.price-input { width: 110px; padding: 5px 8px; border: 1px solid var(--color-border); border-radius: var(--radius); background: var(--color-surface); color: var(--color-text); font-size: 13px; }
.drawer-foot { display: flex; justify-content: flex-end; align-items: center; gap: 12px; margin-top: 16px; }
.save-msg { font-size: 13px; color: var(--color-success); }
.page-hint { color: var(--color-text-muted); font-size: 12px; line-height: 1.6; margin-bottom: 12px; }
</style>
