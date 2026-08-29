import { ClipboardList, LoaderCircle } from 'lucide-react'
import type { Ticket } from '../types'
import StatusBadge from './StatusBadge'
import { categoryLabel, formatTime, priorityLabel } from '../lib/labels'

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
  if (loading && tickets.length === 0) {
    return (
      <div className="empty">
        <LoaderCircle className="spin" />
        <span>正在加载队列</span>
      </div>
    )
  }
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
      {hasMore && (
        <button className="load-more" onClick={onMore} disabled={loading}>
          {loading ? '加载中…' : '加载更多'}
        </button>
      )}
    </div>
  )
}
