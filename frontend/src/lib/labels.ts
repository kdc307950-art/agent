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
  'it.vpn': 'VPN',
  'it.account': '账号',
  'it.network': '网络',
  'it.email': '邮箱',
  'it.hardware': '硬件',
  'it.software': '软件',
  'it.printer': '打印',
  'it.permission': '权限',
  finance: '财务咨询（非 V1）',
  admin: '行政申请（非 V1）',
  product: '产品问题（非 V1）',
  other: '其他（非 V1）',
}

/** V1 工作台默认只在 IT 服务台范围展示（Day 9：非 V1 类别从默认筛选隐藏）。 */
export const v1CategoryOptions: { value: string; label: string }[] = [
  { value: 'it.vpn', label: 'VPN' },
  { value: 'it.account', label: '账号' },
  { value: 'it.network', label: '网络' },
  { value: 'it.email', label: '邮箱' },
  { value: 'it.hardware', label: '硬件' },
  { value: 'it.software', label: '软件' },
  { value: 'it.printer', label: '打印' },
  { value: 'it.permission', label: '权限' },
]

/** V1 可见性：工单类别为 IT（含 it.* 子类）时才在工作台默认队列展示。 */
export function isV1Category(category?: string | null): boolean {
  if (!category) return true
  return category === 'it' || category.startsWith('it.')
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
