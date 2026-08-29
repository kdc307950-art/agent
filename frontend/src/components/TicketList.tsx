/**
 * 工单列表组件（TicketList.tsx）。
 *
 * 职责：
 * - 在队列视图中渲染工单行列表：状态徽章、优先级、更新时间、标题、描述与元信息（编号/类别/处理团队）。
 * - 支持选中高亮（selectedId 与路由 ticketId 联动）与「加载更多」分页按钮。
 *
 * 与后端 API 的对应关系：纯展示组件，不调用 API；
 * 数据与分页状态由父组件 QueueView 提供（tickets 来自 listTickets，hasMore/onMore 由游标驱动）。
 *
 * 关键交互逻辑：
 * - 三种渲染分支：首屏加载中（loading 且列表为空）/ 空列表 / 正常列表。
 * - 每行是 <button>，点击触发 onSelect(ticketId)，由父组件跳转到详情路由。
 * - hasMore 为 true 时底部显示「加载更多」，点击触发 onMore（追加下一页并去重）。
 */
import { ClipboardList, LoaderCircle } from 'lucide-react'
import type { Ticket } from '../types'
import StatusBadge from './StatusBadge'
import { categoryLabel, formatTime, priorityLabel } from '../lib/labels'

// 组件 props：tickets 列表数据；selectedId 当前选中（高亮）；loading/hasMore/onMore 控制加载与分页
interface TicketListProps {
  tickets: Ticket[]
  selectedId?: string | null
  onSelect: (ticketId: string) => void
  loading: boolean
  hasMore: boolean
  onMore: () => void
}

export default function TicketList({
  tickets,
  selectedId,
  onSelect,
  loading,
  hasMore,
  onMore,
}: TicketListProps) {
  // 首屏加载：列表为空且正在请求时显示加载占位
  if (loading && tickets.length === 0) {
    return (
      <div className="empty">
        <LoaderCircle className="spin" />
        <span>正在加载队列</span>
      </div>
    )
  }
  // 空列表：无匹配工单时提示
  if (tickets.length === 0) {
    return (
      <div className="empty">
        <ClipboardList />
        <span>当前没有匹配的工单</span>
      </div>
    )
  }
  return (
    <div className="ticket-list">
      {/* 工单行：整行可点击，选中项（与路由 ticketId 相同）加 ticket-selected 高亮类 */}
      {tickets.map((ticket) => (
        <button
          key={ticket.ticket_id}
          className={`ticket-row ${selectedId === ticket.ticket_id ? 'ticket-selected' : ''}`}
          onClick={() => onSelect(ticket.ticket_id)}
        >
          <div className="ticket-row-top">
            <StatusBadge status={ticket.status} />
            <span className={`priority priority-${ticket.priority}`}>
              {priorityLabel[ticket.priority]}
            </span>
            <time>{formatTime(ticket.updated_at)}</time>
          </div>
          <strong>{ticket.title}</strong>
          <p>{ticket.description || '暂无问题描述'}</p>
          <div className="ticket-meta">
            <span>#{ticket.ticket_id.slice(0, 8)}</span>
            <span>{categoryLabel[ticket.category ?? ''] || '未分类'}</span>
            <span>{ticket.assigned_team_id || '未分派'}</span>
          </div>
        </button>
      ))}
      {/* 加载更多：存在下一页时显示；加载中禁用并切换文案 */}
      {hasMore && (
        <button className="load-more" onClick={onMore} disabled={loading}>
          {loading ? '加载中…' : '加载更多'}
        </button>
      )}
    </div>
  )
}
