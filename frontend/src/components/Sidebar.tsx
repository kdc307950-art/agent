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

const ICON_SIZE = 18

interface NavEntry {
  key: string
  label: string
  Icon: LucideIcon
  to: string
}

const ticketsNav: NavEntry[] = [
  { key: 'queue', label: '工单队列', Icon: Inbox, to: '/tickets' },
  { key: 'mine', label: '我的处理', Icon: UserCheck, to: '/tickets?view=mine' },
  { key: 'resolved', label: '已解决', Icon: CheckCircle2, to: '/tickets?view=resolved' },
]

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
  const view = new URLSearchParams(location.search).get('view') ?? 'queue'
  const path = location.pathname

  const isTicketsActive = (key: string) =>
    path === '/tickets' && view === key

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
      // 桌面端侧栏常驻可聚焦；移动端未展开时禁止 Tab 聚焦（inert）
      {...(!mobileOpen ? { inert: '' as unknown as boolean } : {})}
    >
      <div className="brand">
        <span className="brand-mark">H</span>
        <span>Helpdesk</span>
      </div>
      <nav>
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
        <button
          className={path === '/it-policies' ? 'nav-active' : ''}
          onClick={() => go('/it-policies')}
        >
          <SlidersHorizontal size={ICON_SIZE} />
          <span>IT 策略设置</span>
        </button>
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
