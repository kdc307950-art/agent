/**
 * Resolution Copilot 面板（CopilotPanel.tsx）。
 *
 * 职责：
 * - 展示"生成 AI 处理建议"按钮与生成状态（loading / 失败 / 结果）
 * - 渲染处理步骤、AI 回复草稿、知识引用、置信度与风险提示
 * - 提供"采用草稿"（填充回复编辑框）、"复制到回复框"、"重新生成"
 *
 * 安全边界：
 * - 所有生成结果标注"AI 草稿，发送前必须由客服确认"
 * - "采用草稿"只填充回复编辑框，绝不直接发送（发送仍走工单消息流程）
 * - 切换工单时父组件负责重置本组件状态（或通过 key 强制重建）
 */
import { useState } from 'react'
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
import { approveCopilotDraft, generateCopilot } from '../api/copilot'
import { describeApiError } from '../api/client'

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
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)

  // 生成建议：operation_id 用随机 UUID 保证每次生成独立可审计；
  // 若后端返回 idempotent_replay（相同 operation），仍会返回已有草稿
  const generate = async () => {
    setLoading(true)
    setError('')
    try {
      const result = await generateCopilot(ticketId, {
        operation_id: crypto.randomUUID(),
        expected_version: expectedVersion,
      })
      setDraft(result.draft)
    } catch (err) {
      setError(describeApiError(err))
    } finally {
      setLoading(false)
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

  const canGenerate = enabled && !loading

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

      {draft && (
        <div className="copilot-result">
          {/* 风险提示：AI 草稿必须客服确认后才可发送 */}
          <div className="copilot-warning">
            <AlertTriangle size={14} />
            <span>AI 草稿，发送前必须由客服确认</span>
            <span className="copilot-confidence">
              置信度 {(draft.confidence * 100).toFixed(0)}%
            </span>
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
