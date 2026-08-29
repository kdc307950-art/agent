/**
 * Resolution Copilot 面板（CopilotPanel.tsx）。
 *
 * 职责：
 * - 展示"生成 AI 处理建议"按钮与生成状态（loading / 202 轮询 / 失败 / 结果）
 * - 渲染处理步骤、AI 回复草稿、知识引用、置信度与风险提示
 * - 提供"采用草稿"（填充回复编辑框）、"复制到回复框"、"重新生成"
 *
 * 安全边界（阶段八完善）：
 * - 所有生成结果标注"AI 草稿，发送前必须由客服确认"
 * - "采用草稿"只填充回复编辑框，绝不直接发送（发送仍走工单消息流程）
 * - 503 -> 显示"Copilot 未配置"；202 -> 显示"正在生成"并轮询；
 *   409 -> 提示刷新当前工单
 * - 生成期间禁用按钮（防重复点击）；切换工单（ticketId 变化）时取消旧请求，
 *   旧工单结果不能覆盖新工单（ticketId 守卫 + AbortController）
 */
import { useEffect, useRef, useState } from 'react'
import {
  AlertTriangle,
  BookOpen,
  CheckCircle2,
  Copy,
  LoaderCircle,
  Sparkles,
  Wand2,
} from 'lucide-react'
import type { CopilotDraft } from '../types'
import { approveCopilotDraft, generateCopilot, getCopilotRunStatus } from '../api/copilot'
import { ApiError, describeApiError } from '../api/client'
import { copilotPollConfig } from './copilotPoll'

interface CopilotPanelProps {
  ticketId: string
  expectedVersion: number
  /** 是否允许生成（状态为 assigned/in_progress 时 true） */
  enabled: boolean
  /** 采用草稿：把 draft_answer 填充到回复编辑框（由父组件实现） */
  onAdopt: (text: string) => void
  /** 切换工单时父组件通过 key 重建本组件，因此无需手动重置 */
}

export default function CopilotPanel({
  ticketId,
  expectedVersion,
  enabled,
  onAdopt,
}: CopilotPanelProps) {
  const [draft, setDraft] = useState<CopilotDraft | null>(null)
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(false) // 202：后端仍在生成
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  // 请求取消与竞态守卫：ticketId 变化 / 组件卸载时 abort；响应回来时校验是否仍属当前工单
  const abortRef = useRef<AbortController | null>(null)
  const mountedRef = useRef(true)
  // 最新工单 id 的 ref：generate 闭包捕获的是发起时的值，切换工单后必须用 ref 判断
  const ticketIdRef = useRef(ticketId)
  ticketIdRef.current = ticketId

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      abortRef.current?.abort()
    }
  }, [])

  // 切换工单：取消旧请求、清空旧结果（父组件用 key 重建，这里兜底防旧响应覆盖）
  useEffect(() => {
    abortRef.current?.abort()
    setDraft(null)
    setRunning(false)
    setError('')
    setLoading(false)
  }, [ticketId])

  // 状态轮询：POST 入队后每 2s 查 run 状态，直到 completed/failed/dead
  const pollRunStatus = async (runId: string, signal: AbortSignal) => {
    const deadline = Date.now() + 60_000
    while (Date.now() < deadline) {
      if (signal.aborted || !mountedRef.current) return
      await new Promise((resolve) => setTimeout(resolve, copilotPollConfig.intervalMs))
      if (signal.aborted || !mountedRef.current) return
      try {
        const status = await getCopilotRunStatus(ticketIdRef.current, runId, signal)
        if (!mountedRef.current || signal.aborted) return
        if (status.status === 'completed') {
          setDraft(status.draft)
          setRunning(false)
          setLoading(false)
          return
        }
        if (status.status === 'failed') {
          setRunning(false)
          setLoading(false)
          setError('生成失败，请重新生成重试')
          return
        }
        if (status.status === 'dead') {
          setRunning(false)
          setLoading(false)
          setError('生成进入死信，请转人工处理')
          return
        }
        if (status.status === 'expired') {
          setRunning(false)
          setLoading(false)
          setError('生成任务已过期，请重新生成')
          return
        }
        // queued/processing：继续轮询
      } catch {
        if (signal.aborted) return
      }
    }
    setRunning(false)
    setLoading(false)
    setError('生成超时，请稍后重试')
  }

  // 生成建议：operation_id 用随机 UUID 保证每次生成独立可审计；
  // POST 只入队（202），模型执行由后端 Worker 异步完成，前端轮询 run 状态
  const generate = async () => {
    setLoading(true)
    setRunning(false)
    setError('')
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    // 发起时的工单 id 快照：响应回来时若用户已切走（ref 已更新），丢弃结果
    const requestTicketId = ticketIdRef.current
    try {
      const result = await generateCopilot(requestTicketId, {
        operation_id: crypto.randomUUID(),
        expected_version: expectedVersion,
      }, controller.signal)
      // 竞态守卫：响应返回时若已切走工单则丢弃
      if (!mountedRef.current || ticketIdRef.current !== requestTicketId) return
      // 202 入队成功：显示"正在生成"并轮询 run 状态
      setRunning(true)
      setLoading(false)
      void pollRunStatus(result.run_id, controller.signal)
    } catch (err) {
      if (controller.signal.aborted || !mountedRef.current) return
      if (err instanceof ApiError && err.status === 503) {
        setError('Copilot 服务未配置（未接入模型服务）')
      } else if (err instanceof ApiError && err.status === 409) {
        setError('工单已变更，请刷新当前工单后重试')
      } else {
        setError(describeApiError(err))
      }
    } finally {
      if (mountedRef.current) {
        setLoading(false)
      }
    }
  }

  // 重新生成：清空结果后再发起一次（每次都是新的 operation_id）
  const regenerate = async () => {
    setDraft(null)
    await generate()
  }

  // 复制草稿到剪贴板
  const copyToClipboard = async () => {
    if (!draft?.draft_answer) return
    await navigator.clipboard.writeText(draft.draft_answer)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  // 审批草稿：只做状态迁移（approved），不发送消息
  const approve = async () => {
    if (!draft) return
    try {
      await approveCopilotDraft(ticketId, draft.draft_id)
      setDraft({ ...draft, status: 'approved' })
    } catch (err) {
      setError(describeApiError(err))
    }
  }

  // 防重复点击：生成中 / 后端运行中 / 未启用时均禁用按钮
  const canGenerate = enabled && !loading && !running

  return (
    <section className="detail-section copilot-panel">
      <div className="copilot-header">
        <Sparkles size={16} />
        <h3>Resolution Copilot</h3>
        <button
          className="secondary-action"
          onClick={draft ? regenerate : generate}
          disabled={!canGenerate}
        >
          {loading ? (
            <LoaderCircle className="spin" size={15} />
          ) : (
            <Wand2 size={15} />
          )}
          {draft ? '重新生成' : '生成 AI 处理建议'}
        </button>
      </div>

      {error && (
        <div className="error-banner" style={{ borderLeft: '3px solid #b52f35' }}>
          <AlertTriangle size={15} />
          <span>{error}</span>
        </div>
      )}

      {loading && (
        <p className="copilot-hint">
          <LoaderCircle className="spin" size={14} />
          正在分析知识库、资产与历史工单…
        </p>
      )}

      {/* 202：后端仍在生成（running），显示"正在生成"并轮询 */}
      {running && !loading && (
        <p className="copilot-hint">
          <LoaderCircle className="spin" size={14} />
          正在生成，请稍候…
        </p>
      )}

      {draft && (
        <div className="copilot-result">
          {/* 风险提示：AI 草稿必须客服确认后才可发送 */}
          <div className="copilot-warning">
            <AlertTriangle size={14} />
            <span>AI 草稿，发送前必须由客服确认</span>
            <span className="copilot-confidence">
              置信度 {(draft.confidence * 100).toFixed(0)}%
            </span>
            {/* 检索模式（阶段二）：hybrid 显示双路；lexical-only 显示关键词 */}
            {draft.retrieval_mode && (
              <span className="copilot-badge">
                {draft.retrieval_mode === 'hybrid'
                  ? '混合检索'
                  : draft.degraded
                    ? '关键词检索（向量降级）'
                    : '关键词检索'}
              </span>
            )}
            {draft.needs_human_review && (
              <span className="copilot-badge">需人工复核</span>
            )}
          </div>

          {/* 处理步骤 */}
          {draft.steps.length > 0 && (
            <div className="copilot-block">
              <strong>处理步骤</strong>
              <ol>
                {draft.steps.map((step, index) => (
                  <li key={index}>{step}</li>
                ))}
              </ol>
            </div>
          )}

          {/* AI 回复草稿 */}
          {draft.draft_answer && (
            <div className="copilot-block">
              <strong>AI 回复草稿</strong>
              <p className="description">{draft.draft_answer}</p>
            </div>
          )}

          {/* 知识引用 */}
          {draft.citations.length > 0 && (
            <div className="copilot-block">
              <strong>知识引用</strong>
              {draft.citations.map((citation) => (
                <div className="requester" key={`${citation.document_id}-${citation.chunk_id}`}>
                  <BookOpen size={14} />
                  <div>
                    <strong>{citation.title || citation.document_id}</strong>
                    <span>
                      {citation.document_id} v{citation.document_version} ·{' '}
                      {citation.chunk_id}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* 操作区：采用草稿（填充回复框）/ 复制 / 审批 */}
          <div className="copilot-actions">
            {draft.draft_answer && (
              <button className="primary-action" onClick={() => onAdopt(draft.draft_answer!)}>
                <CheckCircle2 size={15} />
                采用草稿
              </button>
            )}
            {draft.draft_answer && (
              <button className="secondary-action" onClick={copyToClipboard}>
                <Copy size={15} />
                {copied ? '已复制' : '复制'}
              </button>
            )}
            {draft.status === 'generated' && (
              <button className="secondary-action" onClick={approve}>
                <CheckCircle2 size={15} />
                标记已核对
              </button>
            )}
          </div>
        </div>
      )}
    </section>
  )
}
