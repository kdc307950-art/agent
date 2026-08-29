/** 智能助手 SSE 客户端：基于 eventsource-parser 的健壮事件解析。
 *
 * 协议（backend/app.py _execute_run）：
 *  - data: {"type":"text","content":"..."}        文本增量
 *  - data: {"type":"tool","status":"calling|done"} 工具活动
 *  - data: {"type":"interrupt","interrupt_id":...} 等待审批，且本轮不发 end
 *  - data: {"type":"end","run_id":...}             一轮运行完成
 *  - data: {"type":"error","code":...,"content":...} 运行失败
 */

import { createParser, type EventSourceMessage } from 'eventsource-parser'
import { sseFetch } from './client'
import type { ChatEvent } from '../types'

export class ChatAbortError extends Error {
  constructor() {
    super('请求已取消')
    this.name = 'ChatAbortError'
  }
}

function parseEvent(raw: string): ChatEvent | null {
  try {
    return JSON.parse(raw) as ChatEvent
  } catch {
    return null
  }
}

/** 读取 SSE 响应体并逐事件回调。 */
async function readSse(
  response: Response,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  if (!response.ok || !response.body) {
    let detail = `请求失败 (${response.status})`
    try {
      const data = await response.json()
      if (data && typeof data === 'object' && 'detail' in data) {
        detail = String((data as { detail: unknown }).detail)
      }
    } catch {
      /* 非 JSON 错误体，保留默认文案 */
    }
    throw new Error(detail)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()

  const parser = createParser({
    onEvent: (event: EventSourceMessage) => {
      if (event.data) {
        const chatEvent = parseEvent(event.data)
        if (chatEvent) onEvent(chatEvent)
      }
    },
  })

  let aborted = false
  const onAbort = () => {
    aborted = true
    // 主动 cancel reader，让挂起的 read() 立即返回
    reader.cancel().catch(() => {})
  }
  signal?.addEventListener('abort', onAbort)

  try {
    while (!aborted) {
      const { done, value } = await reader.read()
      if (done) break
      parser.feed(decoder.decode(value, { stream: true }))
    }
    // 若是因 cancel 导致的 done，应按取消处理
    if (aborted) {
      throw new ChatAbortError()
    }
    // 流正常结束时也 flush 可能剩余的缓冲区
    parser.feed(decoder.decode())
  } catch (err) {
    if (aborted || signal?.aborted) {
      throw new ChatAbortError()
    }
    throw err
  } finally {
    signal?.removeEventListener('abort', onAbort)
    parser.reset()
    try {
      reader.releaseLock()
    } catch {
      // 已取消或已关闭时忽略
    }
  }
}

export interface StreamChatOptions {
  message: string
  threadId: string
  onEvent: (event: ChatEvent) => void
  signal?: AbortSignal
}

/** 发送一条消息并流式消费回复。 */
export async function streamChat(options: StreamChatOptions): Promise<void> {
  const { message, threadId, onEvent, signal } = options
  const response = await sseFetch('/chat/stream', { message, thread_id: threadId }, signal)
  await readSse(response, onEvent, signal)
}

export interface StreamResumeOptions {
  threadId: string
  approved: boolean
  interruptId?: string
  /** 挂起那一轮的 run_id，服务端仅用于审计串联。 */
  resumedFrom?: string
  onEvent: (event: ChatEvent) => void
  signal?: AbortSignal
}

/** 恢复一次被 interrupt 挂起的运行（审批通过/拒绝），同样以 SSE 流返回结果。 */
export async function streamResume(options: StreamResumeOptions): Promise<void> {
  const { threadId, approved, interruptId, resumedFrom, onEvent, signal } = options
  const response = await sseFetch(
    '/chat/resume',
    {
      thread_id: threadId,
      approved,
      interrupt_id: interruptId,
      resumed_from: resumedFrom,
    },
    signal,
  )
  await readSse(response, onEvent, signal)
}
