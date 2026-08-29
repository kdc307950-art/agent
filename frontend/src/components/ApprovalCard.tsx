/**
 * 人工审批卡片组件（ApprovalCard.tsx）。
 *
 * 职责：
 * - 当 AI 助手（Agent）在 LangGraph 的 interrupt 节点挂起、等待人工决策时，在对话流中展示审批卡片。
 * - 展示审批问题文案，并提供「同意 / 拒绝」两个操作按钮。
 *
 * 与后端 API 的对应关系：本组件为纯展示组件，不直接调用 API；
 * 按钮回调由父组件（AssistantView）透传——onApprove/onReject 最终调用 streamResume 恢复 Agent 执行。
 *
 * 关键交互逻辑：
 * - busy 为 true（审批请求提交中）时禁用两个按钮，防止重复提交；
 *   同屏输入框也会被父组件锁定，保证审批期间无法发送新消息。
 */
import { ShieldAlert } from 'lucide-react'
import { LoaderCircle } from 'lucide-react'

// 组件 props：question 审批问题文案；busy 提交中状态；onApprove/onReject 同意/拒绝回调
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
      {/* 审批问题：来自后端 interrupt 的 question 字段 */}
      <p>{question}</p>
      <div className="approval-actions">
        {/* 同意：提交中显示旋转图标并禁用 */}
        <button className="primary-action" onClick={onApprove} disabled={busy}>
          {busy ? <LoaderCircle className="spin" size={15} /> : null}
          同意
        </button>
        {/* 拒绝 */}
        <button className="secondary-action" onClick={onReject} disabled={busy}>
          拒绝
        </button>
      </div>
    </div>
  )
}
