import { ShieldAlert } from 'lucide-react'
import { LoaderCircle } from 'lucide-react'

interface ApprovalCardProps {
  question: string
  busy: boolean
  onApprove: () => void
  onReject: () => void
}

/** 人工审批卡片：Agent 在 interrupt 处挂起时展示，输入框同时锁定。 */
export default function ApprovalCard({ question, busy, onApprove, onReject }: ApprovalCardProps) {
  return (
    <div className="approval-card">
      <div className="approval-card-title">
        <ShieldAlert size={16} />
        <span>需要审批</span>
      </div>
      <p>{question}</p>
      <div className="approval-actions">
        <button className="primary-action" onClick={onApprove} disabled={busy}>
          {busy ? <LoaderCircle className="spin" size={15} /> : null}
          同意
        </button>
        <button className="secondary-action" onClick={onReject} disabled={busy}>
          拒绝
        </button>
      </div>
    </div>
  )
}
