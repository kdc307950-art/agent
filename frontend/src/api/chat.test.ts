import { describe, it, expect, vi } from 'vitest'
import { streamChat, ChatAbortError } from './chat'

function buildResponse(chunks: string[]): Response {
  const encoder = new TextEncoder()
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(encoder.encode(chunk))
        }
        controller.close()
      },
    }),
    { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
  )
}

describe('streamChat', () => {
  it('在任意分片位置切断仍能正确拼接文本', async () => {
    const events = [
      'data: {"type":"tool","status":"calling"}\n\n',
      'data: {"type":"text","content":"你好"}\n\ndata: {"type":"text","content":"，世界"}\n\n',
      'data: {"type":"end","run_id":"r1"}\n\n',
    ]
    // 故意把第二个事件切成两段，模拟网络分片
    const body = events.join('')
    const chunks = [body.slice(0, 35), body.slice(35)]

    globalThis.fetch = vi.fn().mockResolvedValue(buildResponse(chunks))

    const received: unknown[] = []
    await streamChat({
      message: 'hi',
      threadId: 't1',
      onEvent: (event) => received.push(event),
    })

    expect(received).toEqual([
      { type: 'tool', status: 'calling' },
      { type: 'text', content: '你好' },
      { type: 'text', content: '，世界' },
      { type: 'end', run_id: 'r1' },
    ])
  })

  it('手动取消后抛出 ChatAbortError 且不继续回调', async () => {
    const encoder = new TextEncoder()
    const response = new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode('data: {"type":"text","content":"a"}\n\n'))
        },
      }),
      { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
    )

    globalThis.fetch = vi.fn().mockResolvedValue(response)

    const abortController = new AbortController()
    const received: unknown[] = []
    const promise = streamChat({
      message: 'hi',
      threadId: 't1',
      onEvent: (event) => received.push(event),
      signal: abortController.signal,
    })

    // 确保第一个事件已回调
    await new Promise((resolve) => setTimeout(resolve, 10))
    abortController.abort()

    await expect(promise).rejects.toBeInstanceOf(ChatAbortError)
    expect(received.length).toBeLessThanOrEqual(1)
  })

  it('CRLF 与单事件跨多 data: 行都能正确解析', async () => {
    // 同一事件的 JSON 拆成两行（拆分点选在 JSON 允许空白处，合并后仍合法）
    const body =
      'data: {"type":"tool","status":"calling"}\r\n\r\n' +
      'data: {"type":"text",\r\n' +
      'data: "content":"跨行内容"}\r\n\r\n' +
      'data: {"type":"end","run_id":"r1"}\r\n\r\n'
    globalThis.fetch = vi.fn().mockResolvedValue(buildResponse([body]))

    const received: unknown[] = []
    await streamChat({
      message: 'hi',
      threadId: 't1',
      onEvent: (event) => received.push(event),
    })

    // eventsource-parser 把多 data: 行合并为一行后再回调
    expect(received).toEqual([
      { type: 'tool', status: 'calling' },
      { type: 'text', content: '跨行内容' },
      { type: 'end', run_id: 'r1' },
    ])
  })

  it('服务端 error 事件被正常回调', async () => {
    const body = 'data: {"type":"error","code":"llm_failed","content":"模型超时"}\n\n'
    globalThis.fetch = vi.fn().mockResolvedValue(buildResponse([body]))

    const received: unknown[] = []
    await streamChat({
      message: 'hi',
      threadId: 't1',
      onEvent: (event) => received.push(event),
    })

    expect(received).toEqual([{ type: 'error', code: 'llm_failed', content: '模型超时' }])
  })

  it('网络断开（非 2xx）抛出带状态码的错误而非无限等待', async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ detail: '网关不可达' }), {
          status: 502,
          headers: { 'Content-Type': 'application/json' },
        }),
      )

    await expect(
      streamChat({ message: 'hi', threadId: 't1', onEvent: () => {} }),
    ).rejects.toThrow('网关不可达')
  })

  it('流在任意位置断开（缺 end 事件）不重复回调且正常结束', async () => {
    const body = 'data: {"type":"text","content":"部分文本"}\n\n'
    globalThis.fetch = vi.fn().mockResolvedValue(buildResponse([body]))

    const received: unknown[] = []
    await streamChat({
      message: 'hi',
      threadId: 't1',
      onEvent: (event) => received.push(event),
    })

    expect(received).toEqual([{ type: 'text', content: '部分文本' }])
  })
})
