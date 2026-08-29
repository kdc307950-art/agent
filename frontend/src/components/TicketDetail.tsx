/**
 * 工单详情面板（TicketDetail.tsx）。
 *
 * 职责：
 * - 展示选中工单的完整处理上下文：状态徽章与流转按钮、SLA、描述、请求人、关联资产、
 *   知识引用、回访结果、消息流与处理记录时间线。
 * - 根据工单状态提供下一步操作（状态机流转）、对已解决工单发起回访、
 *   在等待客户补全信息时渲染补充表单。
 *
 * 与后端 API 的对应关系：本组件为受控展示组件，不直接调用 API；
 * 数据由父组件 QueueView 传入（ticket / overview / pendingClarification），
 * 操作通过回调上抛：onTransition → transitionTicket、onSurvey → createSurvey、
 * onResume → resumeIntake（携带 expected_version 做并发控制）。
 *
 * 关键交互逻辑：
 * - actionByStatus 定义状态机：new→start_intake、queued→assign、assigned→start_work、
 *   in_progress→resolve、resolved→close，状态未知时无按钮。
 * - 三种占位渲染：加载中 / 加载失败（可重试）/ 未选中（引导选择）。
 * - 信息补全表单：必须所有 missing_fields 都非空才允许提交（allFilled 校验）。
 */
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

// 一次状态流转动作：action 为后端状态机动作名，actor_type 为执行者类型，label 为按钮文案
export interface TransitionAction {
  action: string
  actor_type: string
  label: string
}

// 状态 → 下一步动作映射表：驱动「开始受理/接单/开始处理/标记解决/关闭」按钮
const actionByStatus: Partial<Record<TicketStatus, TransitionAction>> = {
  new: { action: 'start_intake', actor_type: 'agent', label: '开始受理' },
  queued: { action: 'assign', actor_type: 'agent', label: '接单' },
  assigned: { action: 'start_work', actor_type: 'agent', label: '开始处理' },
  in_progress: { action: 'resolve', actor_type: 'agent', label: '标记解决' },
  resolved: { action: 'close', actor_type: 'agent', label: '关闭工单' },
}

// 组件 props：全部数据与回调均由父组件 QueueView 注入
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
  // 信息补全表单值：字段名 → 用户输入（提交成功后清空）
  const [clarificationFields, setClarificationFields] = useState<Record<string, string>>({})

  // 加载中（或既无工单也无错误）：展示加载占位
  if (detailLoading || (!ticket && !detailError)) {
    return (
      <section className="detail detail-empty">
        <LoaderCircle className="spin" size={32} />
        <h2>正在加载工单</h2>
        <p>请稍候…</p>
      </section>
    )
  }

  // 加载失败且无工单数据：展示错误与「重新加载」按钮（走 onRetry 重新请求详情）
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

  // 未选中任何工单：引导提示
  if (!ticket) {
    return (
      <section className="detail detail-empty">
        <ClipboardList size={32} />
        <h2>选择一张工单</h2>
        <p>从队列中选择工单查看处理上下文。</p>
      </section>
    )
  }

  // 当前状态下可执行的下一步动作（无映射则 undefined，不显示按钮）
  const nextAction = actionByStatus[ticket.status]
  // 后端要求补充的缺失字段列表
  const missingFields = pendingClarification?.missing_fields ?? []

  // 更新某个补全字段的值
  const updateClarificationField = (name: string, value: string) =>
    setClarificationFields((fields) => ({ ...fields, [name]: value }))

  // 校验：所有缺失字段都已填写非空内容，否则禁用「提交补充」按钮
  const allFilled = missingFields.every((name) => String(clarificationFields[name] ?? '').trim())

  // 提交补充信息：通过 onResume（resumeIntake）恢复受理流程，成功后清空表单
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

  // 从 overview 中解构可选展示数据（SLA / 知识引用 / 回访 / 消息流）
  const sla = overview?.sla
  const citations = overview?.citations ?? []
  const survey = overview?.survey
  const messages = overview?.messages ?? []

  return (
    <section className="detail detail-visible">
      <header className="detail-header">
        {/* 移动端返回按钮：回到列表页 */}
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

      {/* 详情级错误横幅：加载/操作失败时显示，可重试 */}
      {detailError && (
        <div className="error-banner" style={{ borderLeft: '3px solid #b52f35' }}>
          <AlertCircle size={16} />
          <span>{detailError}</span>
          <button onClick={onRetry} aria-label="重新加载">
            <RefreshCw size={15} />
          </button>
        </div>
      )}

      {/* 操作区：状态徽章 + 状态流转按钮（按 actionByStatus 映射）+ 已解决时发起回访 */}
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

      {/* 信息补全面板：等待客户补充时渲染缺失字段表单，全部填写后才能提交 */}
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
          {/* 提交补充：全部必填字段非空才可点击 */}
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
        {/* 概要带：优先级 / 类别 / 处理团队 / 版本 */}
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

        {/* 问题描述 */}
        <section className="detail-section">
          <h3>问题描述</h3>
          <p className="description">{ticket.description || '暂无问题描述'}</p>
        </section>

        {/* 请求人信息：渠道 + 创建时间 */}
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

        {/* 关联资产（仅当工单绑定了资产时展示） */}
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

        {/* SLA 计时信息：首响/解决时限、暂停与首响状态 */}
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

        {/* 知识引用：Agent 解答依据的知识文档分块 */}
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

        {/* 回访结果：已回访显示评分与反馈，否则显示回访状态 */}
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

        {/* 消息流：工单的往来消息记录 */}
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

        {/* 处理记录时间线：目前展示创建与解决两个节点 */}
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
