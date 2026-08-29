import type { TicketStatus } from '../types'
import { statusLabel } from '../lib/labels'

export default function StatusBadge({ status }: { status: TicketStatus }) {
  return <span className={`status status-${status}`}>{statusLabel[status] ?? status}</span>
}
