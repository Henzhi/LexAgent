const BASE = '/api'

function authHeaders() {
  const t = localStorage.getItem('lawrag_token')
  return t ? { Authorization: `Bearer ${t}`, 'Content-Type': 'application/json' } : { 'Content-Type': 'application/json' }
}

function handleError(r) {
  if (!r.ok) throw new Error(r.status === 401 ? '认证失败，请重新登录' : '请求失败')
  return r.json()
}

// 主动取消生成：点击"停止"后除断开连接外，再通知后端立即中断 LLM 流
// （覆盖经反向代理时断开信号传不到后端的场景）
export function cancelChat(requestId) {
  if (!requestId) return
  fetch(`${BASE}/chat/cancel`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({ request_id: requestId }),
  }).catch(() => {})
}

// Auth
export const login = (username, password) =>
  fetch(`${BASE}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }) }).then(handleError)

export const register = (username, password) =>
  fetch(`${BASE}/auth/register`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }) }).then(handleError)

export const getMe = () =>
  fetch(`${BASE}/auth/me`, { headers: authHeaders() }).then(handleError)

// Conversations
export const listConversations = () =>
  fetch(`${BASE}/conversations`, { headers: authHeaders() }).then(handleError)

export const loadHistory = (sessionId) =>
  fetch(`${BASE}/conversations/${sessionId}`, { headers: authHeaders() }).then(handleError)

export const saveSession = (sessionId, messages) =>
  fetch(`${BASE}/conversations/${sessionId}`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ messages }) }).then(r => { if (!r.ok) throw new Error('会话保存失败') })

export const deleteConversation = (sessionId) =>
  fetch(`${BASE}/conversations/${sessionId}`, { method: 'DELETE', headers: authHeaders() }).then(r => { if (!r.ok) throw new Error('删除失败') })

// Knowledge
export const uploadDocument = async (file, docType, source, effectiveDate, status = 'active') => {
  const fd = new FormData()
  fd.append('file', file)
  fd.append('doc_type', docType)
  fd.append('source', source)
  fd.append('effective_date', effectiveDate)
  fd.append('status', status)
  const resp = await fetch(`${BASE}/knowledge/upload`, { method: 'POST', headers: { Authorization: authHeaders().Authorization }, body: fd })
  if (!resp.ok) throw new Error('上传失败')
  return resp.json()
}

export const getIngestionStatus = (taskId) =>
  fetch(`${BASE}/knowledge/status/${taskId}`, { headers: authHeaders() }).then(handleError)

export const listDocuments = (opts = {}) => {
  const params = new URLSearchParams()
  if (opts.docType) params.set('doc_type', opts.docType)
  if (opts.status) params.set('status', opts.status)
  if (opts.q) params.set('q', opts.q)
  if (opts.sort) params.set('sort', opts.sort)
  if (opts.order) params.set('order', opts.order)
  if (opts.limit != null) params.set('limit', opts.limit)
  if (opts.offset != null) params.set('offset', opts.offset)
  const qs = params.toString()
  return fetch(`${BASE}/knowledge/documents${qs ? `?${qs}` : ''}`, { headers: authHeaders() }).then(handleError)
}

export const deleteDocument = (docId) =>
  fetch(`${BASE}/knowledge/documents/${docId}`, { method: 'DELETE', headers: authHeaders() }).then(r => { if (!r.ok) throw new Error('删除失败'); return r.json() })

export const getDocumentChunks = (docId, limit = 50, offset = 0) =>
  fetch(`${BASE}/knowledge/documents/${docId}/chunks?limit=${limit}&offset=${offset}`, { headers: authHeaders() }).then(handleError)

// Chat Stream
export async function* streamChat(query, history, sessionId, { signal, requestId } = {}) {
  const resp = await fetch(`${BASE}/chat/stream`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({ query, history, session_id: sessionId, request_id: requestId || '' }),
    signal,
  })
  if (!resp.ok) throw new Error(`请求失败: ${resp.status}`)

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // 按 SSE 事件边界（\n\n）切分：半条事件留在 buffer 中与下一数据包拼接，
    // 避免 TCP 分包把一条 data: 事件截断导致 JSON.parse 失败。
    let sep
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const raw = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      for (const line of raw.split('\n')) {
        if (!line.startsWith('data: ')) continue
        const data = line.slice(6)
        if (data === '[DONE]') return
        try {
          const msg = JSON.parse(data)
          yield msg
        } catch (e) {
          // 单个事件 JSON 损坏（供应商截断 / 网关错误）：不再静默丢弃，
          // 明确报错让上层提示用户，避免"内容凭空丢失"。
          console.warn('[stream] malformed SSE event:', data)
          throw new Error('服务端返回了不完整的数据，请重试')
        }
      }
    }
  }
}

// Query rewrite（智能改写 / 案情分析模式）
export async function rewriteQuery(query) {
  const resp = await fetch(`${BASE}/rewrite`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({ query }),
  })
  if (!resp.ok) throw new Error(`改写请求失败: ${resp.status}`)
  return resp.json()
}

// Crawl（在线更新法律：国家法律法规数据库增量爬取）
export const listCrawlTypes = () =>
  fetch(`${BASE}/crawl/types`, { headers: authHeaders() }).then(handleError)

export const startCrawl = (params) => {
  const body = {
    source: 'npc',
    doc_type: params.doc_type,
    keyword: params.keyword || '',
    limit: params.limit,
    force: !!params.force,
    subdir: params.subdir || '',
    store: params.store || 'both',
    rebuild: !!params.rebuild,
  }
  return fetch(`${BASE}/crawl`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(body),
  }).then(handleError)
}

export const getCrawlStatus = (taskId) =>
  fetch(`${BASE}/crawl/status/${taskId}`, { headers: authHeaders() }).then(handleError)
