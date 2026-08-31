/**
 * 智能助手视图（AssistantView.tsx）。
 *
 * 职责：
 * - 提供与 AI 助手（LangGraph Agent）的流式对话界面：用户提问 → SSE 流式接收回答/工具调用/中断/结束。
 * - 通过 sessionStorage 保持「当前浏览器标签页」内的会话线程 id（thread_id），刷新页面后对话仍能续接。
 * - 当 Agent 需要人工审批时，渲染 ApprovalCard 并支持同意/拒绝（通过 streamResume 恢复执行）。
 *
 * 与后端 API 的对应关系（src/api/chat）：
 * - streamChat：发起新对话，经 SSE（ChatEvent）推送文本增量、工具调用、interrupt 与结束事件。
 * - streamResume：对 interrupt 做出审批决定后恢复 Agent 执行。
 * - ChatAbortError：用户主动中断（AbortController）时的标记错误，用于区分真实失败。
 *
 * 关键交互逻辑：
 * - 消息 id 在发起时生成（crypto.randomUUID()），SSE 事件处理器通过闭包绑定该 id，保证批量状态更新仍能命中目标消息。
 * - 流式期间锁定输入框（streaming / pendingApproval / approvalBusy），防止并发提问破坏单线程会话。
 */
import { useEffect, useRef, useState } from 'react'
import { Bot, Send, Wrench } from 'lucide-react'
import { streamChat, streamResume, ChatAbortError } from '../api/chat'
import type { ChatEvent } from '../types'
import ApprovalCard from '../components/ApprovalCard'

// sessionStorage 中保存线程 id 的键名：同一标签页内对话共享一个线程
const THREAD_KEY = 'assistant_thread_id'

// 一条对话消息：status 记录流式生命周期（streaming 流式中 / done 完成 / error 出错 / aborted 被中断）
interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  status: 'streaming' | 'done' | 'error' | 'aborted'
  // 是否正在调用工具（用于展示「正在调用工具…」指示器）
  toolActive?: boolean
}

// 待审批的 interrupt：记录中断 id、需要从哪个 run 恢复以及审批问题文案
interface PendingApproval {
  interruptId: string
  resumedFrom: string
  question: string
}

// 获取（或首次生成）当前标签页的线程 id：存 sessionStorage，刷新后保持同一对话
function threadIdForSession(): string {
  const existing = sessionStorage.getItem(THREAD_KEY)
  if (existing) return existing
  const next = `assistant_${Date.now().toString(36)}`
  sessionStorage.setItem(THREAD_KEY, next)
  return next
}

// 统一错误格式化：ChatAbortError（用户主动中断）显示为「已中断」
function formatError(err: unknown): string {
  if (err instanceof ChatAbortError) return '已中断'
  if (err instanceof Error) return err.message
  return String(err)
}

export default function AssistantView() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  // 输入框文本
  const [text, setText] = useState('')
  // 是否有流在途（决定输入框/发送按钮是否锁定）
  const [streaming, setStreaming] = useState(false)
  // 审批请求提交中（同意/拒绝按钮禁用）
  const [approvalBusy, setApprovalBusy] = useState(false)
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null)
  // 线程 id：useState 初始化函数只执行一次，会话期间保持不变
  const [threadId] = useState(threadIdForSession)

  // 当前流式请求的取消控制器：切换请求前先取消旧的，卸载时统一取消
  const abortRef = useRef<AbortController | null>(null)
  // SSE 终态由服务端事件决定；流关闭本身不等于成功（error 事件后也会正常 close）。
  const terminalEventRef = useRef<'end' | 'error' | 'interrupt' | null>(null)
  // 对话滚动容器引用：新消息/工具状态变化后自动滚到底部
  const threadRef = useRef<HTMLDivElement | null>(null)

  // 组件卸载时取消在途的流式请求，避免组件卸载后仍回调 setState
  useEffect(() => {
    return () => abortRef.current?.abort()
  }, [])

  // 消息、审批卡片或流式状态变化时，将对话区自动滚动到底部
  useEffect(() => {
    const el = threadRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, pendingApproval, streaming])

  // 按 id 局部更新某条消息的字段（保留其余字段）
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
          // 工具事件：calling 时点亮工具指示器，其余状态熄灭
          patch({ toolActive: event.status === 'calling' })
          break
        case 'interrupt':
          // 中断事件：Agent 挂起等待审批，消息标记完成并弹出审批卡片
          patch({ status: 'done', toolActive: false })
          terminalEventRef.current = 'interrupt'
          setPendingApproval({
            interruptId: event.interrupt_id,
            resumedFrom: event.run_id,
            question: event.question,
          })
          break
        case 'end':
          // 结束事件：对话完成，清除待审批状态
          patch({ status: 'done', toolActive: false })
          terminalEventRef.current = 'end'
          setPendingApproval(null)
          break
        case 'error':
          // 错误事件：后端流式报错，写入错误内容并标记消息为 error
          patch({ status: 'error', content: event.content, toolActive: false })
          terminalEventRef.current = 'error'
          break
      }
    }
  }

  // 统一发起一次流式对话：新建 assistant 消息（streaming 状态）→ 绑定事件处理器 → 执行 runner。
  // runner 由调用方提供（新对话用 streamChat，审批后恢复用 streamResume）
  const beginStream = async (
    runner: (signal: AbortSignal, onEvent: (event: ChatEvent) => void) => Promise<void>,
  ) => {
    // 每条消息用 uuid 作为稳定标识，事件回调通过闭包绑定它
    const id = crypto.randomUUID()
    terminalEventRef.current = null
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
      // 只有未收到服务端 error 才把无终态的正常关闭视为完成。
      if (terminalEventRef.current !== 'error') {
        patchBy(id, { status: 'done' })
      }
    } catch (err) {
      // 主动中断（用户取消）与 ChatAbortError 都标记为 aborted；其余为真实错误
      if (controller.signal.aborted || err instanceof ChatAbortError) {
        patchBy(id, { status: 'aborted', content: formatError(err), toolActive: false })
      } else if (terminalEventRef.current !== 'error') {
        patchBy(id, { status: 'error', content: formatError(err), toolActive: false })
      }
    } finally {
      setStreaming(false)
      abortRef.current = null
    }
  }

  // 发送用户消息：空内容/流式中/有待审批时忽略；用户消息立即上屏，随后启动 assistant 流
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

  // 审批决定（同意/拒绝）：通过 streamResume 恢复被 interrupt 挂起的 Agent 执行
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

  // 锁定条件：流式中 / 等待审批 / 审批提交中，三者任一成立则禁止输入与发送
  const locked = streaming || Boolean(pendingApproval) || approvalBusy

  return (
    <main className="assistant-view">
      <header>
        <div>
          <span className="eyebrow">内部协作</span>
          <h1>智能助手</h1>
        </div>
      </header>

      {/* 对话滚动区：无消息时显示空状态引导；消息流 + 审批卡片按顺序渲染 */}
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
            {/* 工具调用指示器：Agent 正在调用工具时展示 */}
            {item.toolActive && (
              <div className="tool-indicator">
                <Wrench size={14} />
                正在调用工具…
              </div>
            )}
            {/* 流式输入光标：无工具调用时显示闪烁光标示意正在输出 */}
            {item.status === 'streaming' && !item.toolActive && (
              <span className="stream-caret" aria-hidden="true" />
            )}
          </div>
        ))}
        {/* 待审批卡片：Agent 在 interrupt 挂起时展示，等待人工同意/拒绝 */}
        {pendingApproval && (
          <ApprovalCard
            question={pendingApproval.question}
            busy={approvalBusy}
            onApprove={() => decide(true)}
            onReject={() => decide(false)}
          />
        )}
      </div>

      {/* 底部输入区：Enter 发送、Shift+Enter 换行；锁定期间禁用并更换占位文案 */}
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
