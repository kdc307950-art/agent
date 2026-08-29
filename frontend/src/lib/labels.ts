/** 展示标签与格式化工具（中文 UI 文案集中于此）。 */

export const statusLabel: Record<string, string> = {
  new: '新建',
  intaking: '受理中',
  awaiting_customer: '等待客户',
  classified: '已分类',
  answer_proposed: '方案待确认',
  awaiting_customer_confirmation: '等待确认',
  queued: '待分派',
  assigned: '已分派',
  in_progress: '处理中',
  awaiting_approval: '待审批',
  resolved: '已解决',
  closed: '已关闭',
  cancelled: '已取消',
}

export const categoryLabel: Record<string, string> = {
  it: 'IT 故障',
  finance: '财务咨询',
  admin: '行政申请',
  product: '产品问题',
  other: '其他',
}

export const priorityLabel: Record<string, string> = {
  low: '低',
  normal: '普通',
  high: '高',
  urgent: '紧急',
}

export function formatTime(value?: string | null): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}
