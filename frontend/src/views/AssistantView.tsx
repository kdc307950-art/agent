import { useEffect, useRef, useState } from 'react'
import { Bot, Send, Wrench } from 'lucide-react'
import { streamChat, streamResume, ChatAbortError } from '../api/chat'
import type { ChatEvent } from '../types'
import ApprovalCard from '../components/ApprovalCard'

const THREAD_KEY = 'assistant_thread_id'

interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  status: 'streaming' | 'done' | 'error' | 'aborted'
  toolActive?: boolean
}

interface PendingApproval {
  interruptId: string
  resumedFrom: string
  question: string
}

function threadIdForSession(): string {
  const existing = sessionStorage.getItem(THREAD_KEY)
  if (existing) return existing
  const next = `assistant_${Date.now().toString(36)}`
  sessionStorage.setItem(THREAD_KEY, next)
  return next
}

function formatError(err: unknown): string {
  if (err instanceof ChatAbortError) return '已中断'
  if (err instanceof Error) return err.message
  return String(err)
}

export default function AssistantView() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [text, setText] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [approvalBusy, setApprovalBusy] = useState(false)
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null)
  const [threadId] = useState(threadIdForSession)

  const abortRef = useRef<AbortController | null>(null)
  const threadRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    return () => abortRef.current?.abort()
  }, [])

  useEffect(() => {
    const el = threadRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, pendingApproval, streaming])

  const patchBy = (id: string, p: Partial<ChatMessage>) =>
    setMessages((items) => items.map((m) => (m.id === id ? { ...m, ...p } : m)))

  /** 统一的 SSE 事件处理：文本/工具/中断/结束/错误。
   *
   * 消息 id 通过闭包绑定（而非 ref），保证批量状态更新延迟执行时仍能命中目标消息。
   */
  const makeEventHandler = (id: string) => {
    const patch = (p: Partial<ChatMessage>) => patchBy(id, p)
    return (event: ChatEvent) => {
      switch (event.type) {
        case 'text':
          // 追加增量，同时关闭工具指示（首个文本块到达说明推理结束）
          setMessages((items) =>
            items.map((m) =>
              m.id === id
                ? { ...m, content: m.content + event.content, toolActive: false }
                : m,
            ),
          )
          break
        case 'tool':
          patch({ toolActive: event.status === 'calling' })
          break
        case 'interrupt':
          patch({ status: 'done', toolActive: false })
          setPendingApproval({
            interruptId: event.interrupt_id,
            resumedFrom: event.run_id,
            question: event.question,
          })
          break
        case 'end':
          patch({ status: 'done', toolActive: false })
          setPendingApproval(null)
          break
        case 'error':
          patch({ status: 'error', content: event.content, toolActive: false })
          break
      }
    }
  }

  const beginStream = async (
    runner: (signal: AbortSignal, onEvent: (event: ChatEvent) => void) => Promise<void>,
  ) => {
    const id = crypto.randomUUID()
    const handleEvent = makeEventHandler(id)
    setMessages((items) => [
      ...items,
      { id, role: 'assistant', content: '', status: 'streaming' },
    ])
    setStreaming(true)
    const controller = new AbortController()
    abortRef.current = controller
    try {
      await runner(controller.signal, handleEvent)
      // 流正常结束但没有收到 end（例如只返回 interrupt），则把 streaming 标为 done
      patchBy(id, { status: 'done' })
    } catch (err) {
      if (controller.signal.aborted || err instanceof ChatAbortError) {
        patchBy(id, { status: 'aborted', content: formatError(err), toolActive: false })
      } else {
        patchBy(id, { status: 'error', content: formatError(err), toolActive: false })
      }
    } finally {
      setStreaming(false)
      abortRef.current = null
    }
  }

  const send = async () => {
    const question = text.trim()
    if (!question || streaming || pendingApproval) return
    setText('')
    setMessages((items) => [
      ...items,
      { id: crypto.randomUUID(), role: 'user', content: question, status: 'done' },
    ])
    await beginStream((signal, onEvent) =>
      streamChat({
        message: question,
        threadId,
        onEvent,
        signal,
      }),
    )
  }

  const decide = async (approved: boolean) => {
    if (!pendingApproval || approvalBusy) return
    setApprovalBusy(true)
    const pending = pendingApproval
    await beginStream((signal, onEvent) =>
      streamResume({
        threadId,
        approved,
        interruptId: pending.interruptId,
        resumedFrom: pending.resumedFrom,
        onEvent,
        signal,
      }),
    )
    setApprovalBusy(false)
  }

  const locked = streaming || Boolean(pendingApproval) || approvalBusy

  return (
    <main className="assistant-view">
      <header>
        <div>
          <span className="eyebrow">内部协作</span>
          <h1>智能助手</h1>
        </div>
      </header>

      <div className="assistant-thread" ref={threadRef} aria-live="polite" aria-atomic="false">
        {messages.length === 0 && (
          <div className="assistant-empty">
            <Bot size={34} />
            <h2>查询知识与处理建议</h2>
          </div>
        )}
        {messages.map((item) => (
          <div
            key={item.id}
            className={`assistant-message ${item.role}${item.status === 'error' || item.status === 'aborted' ? ' error' : ''}`}
          >
            {item.content || (item.status === 'streaming' && item.toolActive ? '' : item.content)}
            {item.toolActive && (
              <div className="tool-indicator">
                <Wrench size={14} />
                正在调用工具…
              </div>
            )}
            {item.status === 'streaming' && !item.toolActive && (
              <span className="stream-caret" aria-hidden="true" />
            )}
          </div>
        ))}
        {pendingApproval && (
          <ApprovalCard
            question={pendingApproval.question}
            busy={approvalBusy}
            onApprove={() => decide(true)}
            onReject={() => decide(false)}
          />
        )}
      </div>

      <div className="assistant-input">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send()
            }
          }}
          placeholder={pendingApproval ? '等待审批，输入框已锁定…' : '输入问题'}
          disabled={locked}
        />
        <button className="icon-button send-button" onClick={send} disabled={locked} aria-label="发送">
          <Send />
        </button>
      </div>
    </main>
  )
}
