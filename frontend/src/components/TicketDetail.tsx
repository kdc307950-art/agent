import { useState } from 'react'
import {
  AlertCircle,
  BookOpen,
  Boxes,
  ChevronLeft,
  CircleUserRound,
  ClipboardList,
  LoaderCircle,
  Menu,
  MessageSquareText,
  RefreshCw,
  Send,
  Star,
} from 'lucide-react'
import type { PendingInterrupt, Ticket, TicketOverview, TicketStatus } from '../types'
import type { ResumeIntakeInput } from '../api/tickets'
import StatusBadge from './StatusBadge'
import { categoryLabel, formatTime, priorityLabel, statusLabel } from '../lib/labels'

export interface TransitionAction {
  action: string
  actor_type: string
  label: string
}

const actionByStatus: Partial<Record<TicketStatus, TransitionAction>> = {
  new: { action: 'start_intake', actor_type: 'agent', label: '开始受理' },
  queued: { action: 'assign', actor_type: 'agent', label: '接单' },
  assigned: { action: 'start_work', actor_type: 'agent', label: '开始处理' },
  in_progress: { action: 'resolve', actor_type: 'agent', label: '标记解决' },
  resolved: { action: 'close', actor_type: 'agent', label: '关闭工单' },
}

interface TicketDetailProps {
  ticket: Ticket | null
  overview: TicketOverview | null
  busy: boolean
  detailLoading: boolean
  detailError: string
  pendingClarification: PendingInterrupt | null
  onTransition: (action: TransitionAction) => void
  onSurvey: () => void
  onResume: (payload: ResumeIntakeInput) => void
  onBack: () => void
  onRetry: () => void
}

export default function TicketDetail({
  ticket,
  overview,
  busy,
  detailLoading,
  detailError,
  pendingClarification,
  onTransition,
  onSurvey,
  onResume,
  onBack,
  onRetry,
}: TicketDetailProps) {
  const [clarificationFields, setClarificationFields] = useState<Record<string, string>>({})

  if (detailLoading || (!ticket && !detailError)) {
    return (
      <section className="detail detail-empty">
        <LoaderCircle className="spin" size={32} />
        <h2>正在加载工单</h2>
        <p>请稍候…</p>
      </section>
    )
  }

  if (detailError && !ticket) {
    return (
      <section className="detail detail-empty">
        <AlertCircle size={32} />
        <h2>工单加载失败</h2>
        <p>{detailError}</p>
        <button className="primary-action" onClick={onRetry} style={{ marginTop: 12 }}>
          <RefreshCw size={16} />
          重新加载
        </button>
      </section>
    )
  }

  if (!ticket) {
    return (
      <section className="detail detail-empty">
        <ClipboardList size={32} />
        <h2>选择一张工单</h2>
        <p>从队列中选择工单查看处理上下文。</p>
      </section>
    )
  }

  const nextAction = actionByStatus[ticket.status]
  const missingFields = pendingClarification?.missing_fields ?? []

  const updateClarificationField = (name: string, value: string) =>
    setClarificationFields((fields) => ({ ...fields, [name]: value }))

  const allFilled = missingFields.every((name) => String(clarificationFields[name] ?? '').trim())

  const submitClarification = async () => {
    if (!allFilled) return
    await onResume({
      operation_id: crypto.randomUUID(),
      interrupt_id: pendingClarification?.interrupt_id,
      ticket_id: ticket.ticket_id,
      actor_type: 'customer',
      actor_id: 'current-user',
      action: 'provide_information',
      expected_version: ticket.version,
      payload: { fields: clarificationFields },
    })
    setClarificationFields({})
  }

  const sla = overview?.sla
  const citations = overview?.citations ?? []
  const survey = overview?.survey
  const messages = overview?.messages ?? []

  return (
    <section className="detail detail-visible">
      <header className="detail-header">
        <button className="icon-button mobile-only" onClick={onBack} aria-label="返回列表">
          <ChevronLeft />
        </button>
        <div>
          <div className="eyebrow">工单 #{ticket.ticket_id.slice(0, 8)}</div>
          <h2>{ticket.title}</h2>
        </div>
        <button className="icon-button" aria-label="更多操作">
          <Menu />
        </button>
      </header>

      {detailError && (
        <div className="error-banner" style={{ borderLeft: '3px solid #b52f35' }}>
          <AlertCircle size={16} />
          <span>{detailError}</span>
          <button onClick={onRetry} aria-label="重新加载">
            <RefreshCw size={15} />
          </button>
        </div>
      )}

      <div className="detail-actions">
        <StatusBadge status={ticket.status} />
        {nextAction && (
          <button
            className="primary-action"
            onClick={() => onTransition(nextAction)}
            disabled={busy || detailLoading}
          >
            {busy ? <LoaderCircle className="spin" size={16} /> : null}
            {nextAction.label}
          </button>
        )}
        {ticket.status === 'resolved' && (
          <button className="secondary-action" onClick={onSurvey} disabled={busy || detailLoading}>
            <Star size={16} />
            发起回访
          </button>
        )}
      </div>

      {pendingClarification && (
        <section className="clarification-panel">
          <div className="clarification-title">
            <MessageSquareText size={16} />
            <strong>{pendingClarification.question || '请补充工单信息'}</strong>
          </div>
          {missingFields.map((name) => (
            <label key={name}>
              {name}
              <input
                value={clarificationFields[name] ?? ''}
                onChange={(event) => updateClarificationField(name, event.target.value)}
                disabled={busy}
              />
            </label>
          ))}
          <button
            className="primary-action"
            onClick={submitClarification}
            disabled={busy || !allFilled}
          >
            <Send size={15} />
            提交补充
          </button>
        </section>
      )}

      <div className="detail-scroll">
        <section className="summary-band">
          <dl>
            <div>
              <dt>优先级</dt>
              <dd>{priorityLabel[ticket.priority]}</dd>
            </div>
            <div>
              <dt>类别</dt>
              <dd>{categoryLabel[ticket.category ?? ''] || '未分类'}</dd>
            </div>
            <div>
              <dt>处理团队</dt>
              <dd>{ticket.assigned_team_id || '待分派'}</dd>
            </div>
            <div>
              <dt>版本</dt>
              <dd>v{ticket.version}</dd>
            </div>
          </dl>
        </section>

        <section className="detail-section">
          <h3>问题描述</h3>
          <p className="description">{ticket.description || '暂无问题描述'}</p>
        </section>

        <section className="detail-section">
          <h3>请求人</h3>
          <div className="requester">
            <CircleUserRound />
            <div>
              <strong>{ticket.requester_id}</strong>
              <span>
                {ticket.channel} · 创建于 {formatTime(ticket.created_at)}
              </span>
            </div>
          </div>
        </section>

        {ticket.asset_id && (
          <section className="detail-section">
            <h3>关联资产</h3>
            <div className="requester">
              <Boxes />
              <div>
                <strong>{ticket.asset_id}</strong>
                <span>资产已绑定到本工单</span>
              </div>
            </div>
          </section>
        )}

        {sla && (
          <section className="detail-section">
            <h3>SLA</h3>
            <div className="ticket-meta">
              <span>首次响应 {formatTime(sla.first_response_due_at)}</span>
              <span>解决时限 {formatTime(sla.resolution_due_at)}</span>
              <span>{sla.paused_at ? '已暂停' : '计时中'}</span>
              <span>{sla.first_responded_at ? '已首次响应' : '尚未首响'}</span>
            </div>
          </section>
        )}

        {citations.length > 0 && (
          <section className="detail-section">
            <h3>知识引用</h3>
            {citations.map((citation, index) => (
              <div className="requester" key={`${citation.document_id}-${index}`}>
                <BookOpen />
                <div>
                  <strong>{citation.title || citation.document_id}</strong>
                  <span>
                    {citation.document_id} v{citation.document_version} · {citation.chunk_id}
                  </span>
                </div>
              </div>
            ))}
          </section>
        )}

        {survey && (
          <section className="detail-section">
            <h3>回访结果</h3>
            <p className="description">
              {survey.status === 'responded'
                ? `${survey.score} 分 · ${survey.feedback || '无文字反馈'}`
                : statusLabel[survey.status] ?? survey.status}
            </p>
          </section>
        )}

        {messages.length > 0 && (
          <section className="detail-section">
            <h3>消息流</h3>
            {messages.map((message) => (
              <div className="requester" key={message.message_id}>
                <MessageSquareText />
                <div>
                  <strong>{message.actor_id}</strong>
                  <span>{message.content}</span>
                </div>
              </div>
            ))}
          </section>
        )}

        <section className="detail-section">
          <h3>处理记录</h3>
          <div className="timeline">
            <div className="timeline-item">
              <span />
              <div>
                <strong>工单已创建</strong>
                <p>{ticket.channel} 渠道进入服务台</p>
                <time>{formatTime(ticket.created_at)}</time>
              </div>
            </div>
            {ticket.resolved_at && (
              <div className="timeline-item">
                <span />
                <div>
                  <strong>问题已解决</strong>
                  <p>等待关闭或回访</p>
                  <time>{formatTime(ticket.resolved_at)}</time>
                </div>
              </div>
            )}
          </div>
        </section>
      </div>
    </section>
  )
}
