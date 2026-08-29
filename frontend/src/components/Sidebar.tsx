/**
 * 侧边导航组件（Sidebar.tsx）。
 *
 * 职责：
 * - 桌面端常驻显示导航；移动端以抽屉形式展示（mobileOpen 控制展开，点击导航项或按 Escape 收起）。
 * - 导航分三组：工单相关（队列/我的处理/已解决）、主要页面（助手/资产/知识库）、底部（IT 策略设置 + 坐席信息）。
 *
 * 与后端 API 的对应关系：纯导航组件，不调用 API；跳转目标为 React Router 路径，
 * 其中「我的处理」「已解决」通过 ?view=mine / ?view=resolved 查询参数区分工单队列视角。
 *
 * 关键交互逻辑：
 * - 工单导航用 button + 手动高亮（isTicketsActive）：详情页 /tickets/:ticketId 保持「工单队列」高亮；
 *   列表页按 path 与 view 参数精确匹配。
 * - 主导航用 NavLink 自动高亮；跳转后统一调用 onClose 收起移动端抽屉。
 * - 移动端展开时监听 Escape 键关闭；未展开时设置 inert 阻止键盘聚焦（React 19 原生布尔属性）。
 */
import { useEffect } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import {
  BookOpen,
  Bot,
  Boxes,
  CheckCircle2,
  CircleUserRound,
  Inbox,
  SlidersHorizontal,
  UserCheck,
  type LucideIcon,
} from 'lucide-react'

// 导航图标统一尺寸
const ICON_SIZE = 18

// 单个导航项：key 为工单视角标识（queue/mine/resolved），to 为目标路径
interface NavEntry {
  key: string
  label: string
  Icon: LucideIcon
  to: string
}

// 工单相关导航：三项共用 /tickets 前缀，靠 view 查询参数区分视角
const ticketsNav: NavEntry[] = [
  { key: 'queue', label: '工单队列', Icon: Inbox, to: '/tickets' },
  { key: 'mine', label: '我的处理', Icon: UserCheck, to: '/tickets?view=mine' },
  { key: 'resolved', label: '已解决', Icon: CheckCircle2, to: '/tickets?view=resolved' },
]

// 主要页面导航：与工单无关的独立路由
const mainNav: Omit<NavEntry, 'key'>[] = [
  { label: '智能助手', Icon: Bot, to: '/assistant' },
  { label: '资产', Icon: Boxes, to: '/assets' },
  { label: '知识库', Icon: BookOpen, to: '/knowledge' },
]

export default function Sidebar({
  mobileOpen,
  onClose,
}: {
  mobileOpen: boolean
  onClose: () => void
}) {
  const location = useLocation()
  const navigate = useNavigate()
  // 从 URL 查询参数读取当前工单视角，缺省为 queue
  const view = new URLSearchParams(location.search).get('view') ?? 'queue'
  const path = location.pathname

  // 判断工单导航项是否高亮：
  const isTicketsActive = (key: string) => {
    // 详情页 /tickets/:ticketId 保持"工单队列"高亮
    if (path.startsWith('/tickets/')) return key === 'queue'
    return path === '/tickets' && view === key
  }

  // 统一跳转：导航后关闭移动端抽屉
  const go = (to: string) => {
    navigate(to)
    onClose()
  }

  // 移动端：Escape 关闭侧栏
  useEffect(() => {
    if (!mobileOpen) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [mobileOpen, onClose])

  return (
    <aside
      className={`sidebar ${mobileOpen ? 'sidebar-open' : ''}`}
      aria-hidden={!mobileOpen ? 'true' : undefined}
      // 桌面端侧栏常驻可聚焦；移动端未展开时禁止 Tab 聚焦（React 19 原生布尔 inert）
      inert={!mobileOpen}
    >
      <div className="brand">
        <span className="brand-mark">H</span>
        <span>Helpdesk</span>
      </div>
      <nav>
        {/* 工单导航组：手动高亮（详情页时队列保持高亮） */}
        {ticketsNav.map(({ key, label, Icon, to }) => (
          <button
            key={key}
            className={isTicketsActive(key) ? 'nav-active' : ''}
            onClick={() => go(to)}
          >
            <Icon size={ICON_SIZE} />
            <span>{label}</span>
          </button>
        ))}
        <div style={{ height: 4 }} />
        {/* 主要页面导航组：NavLink 依据当前路由自动高亮 */}
        {mainNav.map(({ label, Icon, to }) => (
          <NavLink
            key={to}
            to={to}
            onClick={onClose}
            className={({ isActive }) => (isActive ? 'nav-active' : '')}
          >
            <Icon size={ICON_SIZE} />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>
      <div className="sidebar-bottom">
        {/* IT 策略设置：手动高亮（path 精确匹配 /it-policies） */}
        <button
          className={path === '/it-policies' ? 'nav-active' : ''}
          onClick={() => go('/it-policies')}
        >
          <SlidersHorizontal size={ICON_SIZE} />
          <span>IT 策略设置</span>
        </button>
        {/* 当前坐席信息（静态展示） */}
        <div className="operator">
          <CircleUserRound size={24} />
          <div>
            <strong>客服坐席</strong>
            <span>在线</span>
          </div>
        </div>
      </div>
    </aside>
  )
}
