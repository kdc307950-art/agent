/**
 * 工单状态徽章组件（StatusBadge.tsx）。
 *
 * 职责：把工单状态枚举（TicketStatus）渲染为彩色徽章，
 * 样式类名形如 status-<状态值>（如 status-new / status-in_progress），配色由 CSS 决定。
 *
 * 与后端 API 的对应关系：纯展示组件，不调用 API；
 * 状态文案来自 lib/labels 的 statusLabel 映射表（后端返回的是英文枚举值，如 'in_progress'）。
 *
 * 关键交互逻辑：无状态、无副作用；未知状态时直接回退显示原始枚举值（?? status），保证不因缺映射而空白。
 */
import type { TicketStatus } from '../types'
import { statusLabel } from '../lib/labels'

// 渲染状态徽章：优先取 statusLabel 中文文案，未收录的状态回退显示原始值
export default function StatusBadge({ status }: { status: TicketStatus }) {
  return <span className={`status status-${status}`}>{statusLabel[status] ?? status}</span>
}
