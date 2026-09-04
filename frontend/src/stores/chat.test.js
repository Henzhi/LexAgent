import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useChatStore } from './chat'

// store 依赖浏览器 API：sessionStorage（node 环境没有）与 crypto.randomUUID
// （node 22 全局可用）。这里用内存 Map 桩掉 sessionStorage，行为对齐规范。
class FakeStorage {
  constructor() {
    this.map = new Map()
  }

  getItem(k) {
    return this.map.has(k) ? this.map.get(k) : null
  }

  setItem(k, v) {
    this.map.set(k, String(v))
  }

  removeItem(k) {
    this.map.delete(k)
  }
}

const PENDING_KEY = 'lawrag_pending_stream'
const SESSION_KEY = 'lawrag_session'

beforeEach(() => {
  globalThis.sessionStorage = new FakeStorage()
  setActivePinia(createPinia())
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

function flushPersist() {
  vi.advanceTimersByTime(600) // 快照写入节流 500ms
}

function writePending(snapshot) {
  sessionStorage.setItem(PENDING_KEY, JSON.stringify({ ...snapshot, updatedAt: Date.now() }))
}

describe('beginStream / 快照持久化', () => {
  it('beginStream 建立带活连接标记的流，500ms 后快照落盘', () => {
    const store = useChatStore()
    store.beginStream('s1', 'req_1')
    expect(store.activeStream).toEqual({ sid: 's1', requestId: 'req_1', traces: [], connected: true })
    flushPersist()
    const raw = JSON.parse(sessionStorage.getItem(PENDING_KEY))
    expect(raw).toMatchObject({ sid: 's1', requestId: 'req_1', lastSeq: 0, answer: '' })
  })

  it('updateStream 推进游标与答案（旧 seq 不回退），节流后落盘', () => {
    const store = useChatStore()
    store.beginStream('s1', 'req_1')
    store.updateStream({ seq: 3, answer: 'abc' })
    store.updateStream({ seq: 2 }) // 乱序/重复事件不得回退游标
    store.updateStream({ answer: 'abcd', sources: [{ law_name: '民法典' }] })
    flushPersist()
    const raw = JSON.parse(sessionStorage.getItem(PENDING_KEY))
    expect(raw).toMatchObject({ lastSeq: 3, answer: 'abcd', sources: [{ law_name: '民法典' }] })
  })

  it('traces 只保留最近 80 条，防止撑爆配额', () => {
    const store = useChatStore()
    store.beginStream('s1', 'req_1')
    for (let i = 0; i < 100; i++) store.activeStream.traces.push({ text: String(i), kind: 'thinking' })
    flushPersist()
    const raw = JSON.parse(sessionStorage.getItem(PENDING_KEY))
    expect(raw.traces).toHaveLength(80)
    expect(raw.traces[0].text).toBe('20')
  })
})

describe('endStream 清理', () => {
  it('匹配 sid + requestId 才清理：清 activeStream、删快照、取消待写入的 timer', () => {
    const store = useChatStore()
    store.beginStream('s1', 'req_1')
    flushPersist()
    store.endStream('s1', 'req_1')
    expect(store.activeStream).toBeNull()
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
    // 再推进时间也不应把快照写回（timer 已取消）
    flushPersist()
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
  })

  it('sid 或 requestId 不匹配时不清理（后台旧流收尾不得清掉新请求的状态）', () => {
    const store = useChatStore()
    store.beginStream('s1', 'req_1')
    store.endStream('s2', 'req_1') // sid 不匹配
    expect(store.activeStream).not.toBeNull()
    store.endStream('s1', 'req_x') // requestId 不匹配
    expect(store.activeStream).not.toBeNull()
    store.endStream('s1', 'req_1') // 匹配 → 清理
    expect(store.activeStream).toBeNull()
  })
})

describe('messagesOf 分桶', () => {
  it('当前会话返回渲染中的 messages（同引用）；后台会话返回独立草稿桶', () => {
    const store = useChatStore()
    store.messages.push({ role: 'user', content: 'hi' })
    expect(store.messagesOf(store.sessionId)).toBe(store.messages)
    const arr = store.messagesOf('other')
    arr.push({ role: 'assistant', content: 'a' })
    expect(store.messagesOf('other')).toHaveLength(1)
    expect(store.messages).toHaveLength(1) // 不串桶
  })
})

describe('switchSession 会话语义（D-0904-1）', () => {
  it('有流在跑的旧会话被保活：切走后 SSE 继续写草稿桶，切回即见现场', () => {
    const store = useChatStore()
    store.sessionId = 'A'
    sessionStorage.setItem(SESSION_KEY, 'A')
    store.messages.push({ role: 'user', content: 'q' })
    store.beginStream('A', 'req_A')

    store.switchSession('B')
    expect(store.sessionId).toBe('B')
    expect(store.messages).toEqual([]) // 新会话空
    expect(sessionStorage.getItem(SESSION_KEY)).toBe('B')

    // 模拟后台流继续写 A
    store.messagesOf('A').push({ role: 'assistant', content: 'partial' })

    store.switchSession('A')
    expect(store.messages).toHaveLength(2) // 切回即见完整现场
    expect(store.messages[1].content).toBe('partial')
  })

  it('无流在跑的旧会话不缓存（切回时从服务端历史重载，避免陈旧数据）', () => {
    const store = useChatStore()
    store.sessionId = 'A'
    store.messages.push({ role: 'user', content: 'q' })
    store.switchSession('B')
    // drafts 里没有 A 的现场 → messagesOf('A') 是全新空数组
    expect(store.messagesOf('A')).toEqual([])
    expect(store.activeStream).toBeNull()
  })

  it('newSession 同样保活旧会话（流继续在后台跑）', () => {
    const store = useChatStore()
    const old = store.sessionId
    store.beginStream(old, 'req_1')
    store.newSession()
    expect(store.sessionId).not.toBe(old)
    expect(store.activeStream).toMatchObject({ sid: old, requestId: 'req_1' })
  })
})

describe('pending 快照恢复（D-0904-2）', () => {
  it('有效快照 → activeStream 重建且 connected=false（允许续流），进度可读', () => {
    writePending({ sid: 's1', requestId: 'req_9', lastSeq: 5, answer: '部分', sources: [], traces: [{ text: 't', kind: 'thinking' }] })
    const store = useChatStore()
    expect(store.activeStream).toMatchObject({ sid: 's1', requestId: 'req_9', connected: false })
    expect(store.streamProgress()).toMatchObject({ lastSeq: 5, answer: '部分' })
    expect(store.activeStream.traces).toHaveLength(1)
  })

  it('过期快照（>9 分钟，事件日志 TTL 已到）→ 丢弃并清掉存储', () => {
    sessionStorage.setItem(PENDING_KEY, JSON.stringify({ sid: 's1', requestId: 'req_9', lastSeq: 5, updatedAt: Date.now() - 10 * 60 * 1000 }))
    const store = useChatStore()
    expect(store.activeStream).toBeNull()
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
  })

  it('损坏的快照不抛错，视为无进行中流', () => {
    sessionStorage.setItem(PENDING_KEY, '{bad json')
    expect(() => useChatStore()).not.toThrow()
    expect(useChatStore().activeStream).toBeNull()
  })

  it('markConnected 翻转活连接标记（resumeIfPending 据此防重复续流）', () => {
    writePending({ sid: 's1', requestId: 'req_9', lastSeq: 1 })
    const store = useChatStore()
    expect(store.activeStream.connected).toBe(false)
    store.markConnected(true)
    expect(store.activeStream.connected).toBe(true)
    store.markConnected(false)
    expect(store.activeStream.connected).toBe(false)
  })
})
