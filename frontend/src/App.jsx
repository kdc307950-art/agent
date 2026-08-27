import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  Bot,
  CheckCircle2,
  ChevronLeft,
  CircleUserRound,
  ClipboardList,
  Filter,
  Inbox,
  LoaderCircle,
  Menu,
  MessageSquareText,
  Plus,
  RefreshCw,
  Search,
  Send,
  Settings,
  Star,
  UserCheck,
  X,
} from 'lucide-react'
import './App.css'

const statusLabel = {
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

const categoryLabel = { it: 'IT 故障', finance: '财务咨询', admin: '行政申请', product: '产品问题', other: '其他' }
const priorityLabel = { low: '低', normal: '普通', high: '高', urgent: '紧急' }

const actionByStatus = {
  new: { action: 'start_intake', actor_type: 'agent', label: '开始受理' },
  queued: { action: 'assign', actor_type: 'agent', label: '接单' },
  assigned: { action: 'start_work', actor_type: 'agent', label: '开始处理' },
  in_progress: { action: 'resolve', actor_type: 'agent', label: '标记解决' },
  resolved: { action: 'close', actor_type: 'agent', label: '关闭工单' },
}

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.detail || `请求失败 (${response.status})`)
  return body
}

function formatTime(value) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

function StatusBadge({ status }) {
  return <span className={`status status-${status}`}>{statusLabel[status] || status}</span>
}

function Sidebar({ view, setView, mobileOpen, onClose }) {
  const items = [
    ['queue', Inbox, '工单队列'],
    ['mine', UserCheck, '我的处理'],
    ['resolved', CheckCircle2, '已解决'],
    ['assistant', Bot, '智能助手'],
  ]
  return (
    <aside className={`sidebar ${mobileOpen ? 'sidebar-open' : ''}`}>
      <div className="brand"><span className="brand-mark">H</span><span>Helpdesk</span></div>
      <nav>
        {items.map(([key, Icon, label]) => (
          <button key={key} className={view === key ? 'nav-active' : ''} onClick={() => { setView(key); onClose() }}>
            <Icon size={18} /><span>{label}</span>
          </button>
        ))}
      </nav>
      <div className="sidebar-bottom">
        <button><Settings size={18} /><span>设置</span></button>
        <div className="operator"><CircleUserRound size={24} /><div><strong>客服坐席</strong><span>在线</span></div></div>
      </div>
    </aside>
  )
}

function TicketList({ tickets, selectedId, onSelect, loading, hasMore, onMore }) {
  if (loading && !tickets.length) return <div className="empty"><LoaderCircle className="spin" /><span>正在加载队列</span></div>
  if (!tickets.length) return <div className="empty"><ClipboardList /><span>当前没有匹配的工单</span></div>
  return (
    <div className="ticket-list">
      {tickets.map((ticket) => (
        <button key={ticket.ticket_id} className={`ticket-row ${selectedId === ticket.ticket_id ? 'ticket-selected' : ''}`} onClick={() => onSelect(ticket.ticket_id)}>
          <div className="ticket-row-top"><StatusBadge status={ticket.status} /><span className={`priority priority-${ticket.priority}`}>{priorityLabel[ticket.priority]}</span><time>{formatTime(ticket.updated_at)}</time></div>
          <strong>{ticket.title}</strong>
          <p>{ticket.description || '暂无问题描述'}</p>
          <div className="ticket-meta"><span>#{ticket.ticket_id.slice(0, 8)}</span><span>{categoryLabel[ticket.category] || '未分类'}</span><span>{ticket.assigned_team_id || '未分派'}</span></div>
        </button>
      ))}
      {hasMore && <button className="load-more" onClick={onMore} disabled={loading}>{loading ? '加载中…' : '加载更多'}</button>}
    </div>
  )
}

function TicketDetail({ ticket, overview, busy, pendingClarification, onTransition, onSurvey, onResume, onBack }) {
  const [clarificationFields, setClarificationFields] = useState({})
  if (!ticket) return <section className="detail detail-empty"><ClipboardList size={32} /><h2>选择一张工单</h2><p>从队列中选择工单查看处理上下文。</p></section>
  const nextAction = actionByStatus[ticket.status]
  const missingFields = pendingClarification?.missing_fields || []
  const updateClarificationField = (name, value) => setClarificationFields((fields) => ({ ...fields, [name]: value }))
  const submitClarification = async () => {
    const complete = missingFields.every((name) => String(clarificationFields[name] || '').trim())
    if (!complete) return
    await onResume({
      operation_id: crypto.randomUUID(),
      interrupt_id: pendingClarification.interrupt_id,
      ticket_id: ticket.ticket_id,
      actor_type: 'customer',
      actor_id: 'current-user',
      action: 'provide_information',
      expected_version: ticket.version,
      payload: { fields: clarificationFields },
    })
    setClarificationFields({})
  }
  return (
    <section className="detail detail-visible">
      <header className="detail-header">
        <button className="icon-button mobile-only" onClick={onBack} aria-label="返回列表"><ChevronLeft /></button>
        <div><div className="eyebrow">工单 #{ticket.ticket_id.slice(0, 8)}</div><h2>{ticket.title}</h2></div>
        <button className="icon-button" aria-label="更多操作"><Menu /></button>
      </header>
      <div className="detail-actions">
        <StatusBadge status={ticket.status} />
        {nextAction && <button className="primary-action" onClick={() => onTransition(nextAction)} disabled={busy}>{busy ? <LoaderCircle className="spin" size={16} /> : null}{nextAction.label}</button>}
        {ticket.status === 'resolved' && <button className="secondary-action" onClick={onSurvey} disabled={busy}><Star size={16} />发起回访</button>}
      </div>
      {pendingClarification && <section className="clarification-panel"><div className="clarification-title"><MessageSquareText size={16} /><strong>{pendingClarification.question || '请补充工单信息'}</strong></div>{missingFields.map((name) => <label key={name}>{name}<input value={clarificationFields[name] || ''} onChange={(event) => updateClarificationField(name, event.target.value)} /></label>)}<button className="primary-action" onClick={submitClarification} disabled={busy || !missingFields.every((name) => String(clarificationFields[name] || '').trim())}><Send size={15} />提交补充</button></section>}
      <div className="detail-scroll">
        <section className="summary-band">
          <dl><div><dt>优先级</dt><dd>{priorityLabel[ticket.priority]}</dd></div><div><dt>类别</dt><dd>{categoryLabel[ticket.category] || '未分类'}</dd></div><div><dt>处理团队</dt><dd>{ticket.assigned_team_id || '待分派'}</dd></div><div><dt>版本</dt><dd>v{ticket.version}</dd></div></dl>
        </section>
        <section className="detail-section"><h3>问题描述</h3><p className="description">{ticket.description || '暂无问题描述'}</p></section>
        <section className="detail-section"><h3>请求人</h3><div className="requester"><CircleUserRound /><div><strong>{ticket.requester_id}</strong><span>{ticket.channel} · 创建于 {formatTime(ticket.created_at)}</span></div></div></section>
        {overview?.sla && <section className="detail-section"><h3>SLA</h3><div className="ticket-meta"><span>首次响应 {formatTime(overview.sla.first_response_due_at)}</span><span>解决时限 {formatTime(overview.sla.resolution_due_at)}</span><span>{overview.sla.paused_at ? '已暂停' : '计时中'}</span></div></section>}{overview?.survey && <section className="detail-section"><h3>回访结果</h3><p className="description">{overview.survey.status === 'responded' ? `${overview.survey.score} 分 · ${overview.survey.feedback || '无文字反馈'}` : statusLabel[overview.survey.status] || overview.survey.status}</p></section>}{overview?.messages?.length > 0 && <section className="detail-section"><h3>消息流</h3>{overview.messages.map((message) => <div className="requester" key={message.message_id}><MessageSquareText /><div><strong>{message.actor_id}</strong><span>{message.content}</span></div></div>)}</section>}<section className="detail-section"><h3>处理记录</h3><div className="timeline"><div className="timeline-item"><span /><div><strong>工单已创建</strong><p>{ticket.channel} 渠道进入服务台</p><time>{formatTime(ticket.created_at)}</time></div></div>{ticket.resolved_at && <div className="timeline-item"><span /><div><strong>问题已解决</strong><p>等待关闭或回访</p><time>{formatTime(ticket.resolved_at)}</time></div></div>}</div></section>
      </div>
    </section>
  )
}

function CreateTicketDialog({ open, onClose, onCreated }) {
  const [form, setForm] = useState({ title: '', description: '', priority: 'normal' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  if (!open) return null
  const submit = async (event) => {
    event.preventDefault(); setBusy(true); setError('')
    try {
      const ticket = await api('/tickets', { method: 'POST', body: JSON.stringify({ ...form, channel: 'web' }) })
      const intake = await api(`/tickets/${ticket.ticket_id}/intake`, { method: 'POST', body: JSON.stringify({ operation_id: crypto.randomUUID(), text: `${form.title}\n${form.description}`, fields: { title: form.title, description: form.description }, expected_version: ticket.version }) })
      setForm({ title: '', description: '', priority: 'normal' }); onCreated(intake); onClose()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}><form className="modal" onSubmit={submit}><header><div><span className="eyebrow">新建工单</span><h2>提交服务请求</h2></div><button type="button" className="icon-button" onClick={onClose} aria-label="关闭"><X /></button></header><label>标题<input autoFocus required maxLength="512" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></label><label>问题描述<textarea required rows="5" maxLength="8000" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></label><label>优先级<select value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })}><option value="low">低</option><option value="normal">普通</option><option value="high">高</option><option value="urgent">紧急</option></select></label>{error && <div className="form-error"><AlertCircle size={16} />{error}</div>}<footer><button type="button" className="secondary-action" onClick={onClose}>取消</button><button className="primary-action" disabled={busy}>{busy ? '提交中…' : '提交工单'}</button></footer></form></div>
}

function AssistantView() {
  const [messages, setMessages] = useState([])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const send = async () => {
    if (!text.trim() || busy) return
    const question = text.trim(); setText(''); setMessages((items) => [...items, { role: 'user', text: question }]); setBusy(true)
    try {
      const response = await fetch('/api/chat/stream', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: question, thread_id: `workbench_${Date.now()}` }) })
      const raw = await response.text(); const content = [...raw.matchAll(/^data: (.+)$/gm)].map((match) => { try { return JSON.parse(match[1]) } catch { return null } }).filter((item) => item?.type === 'text').map((item) => item.content).join('')
      setMessages((items) => [...items, { role: 'assistant', text: content || '暂时没有可用答案。' }])
    } catch { setMessages((items) => [...items, { role: 'assistant', text: '服务暂时不可用。' }]) } finally { setBusy(false) }
  }
  return <main className="assistant-view"><header><div><span className="eyebrow">内部协作</span><h1>智能助手</h1></div></header><div className="assistant-thread">{messages.length === 0 && <div className="assistant-empty"><Bot size={34} /><h2>查询知识与处理建议</h2></div>}{messages.map((item, index) => <div key={index} className={`assistant-message ${item.role}`}>{item.text}</div>)}{busy && <div className="assistant-message assistant"><LoaderCircle className="spin" size={18} /></div>}</div><div className="assistant-input"><textarea value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }} placeholder="输入问题" /><button className="icon-button send-button" onClick={send} disabled={busy} aria-label="发送"><Send /></button></div></main>
}

function App() {
  const [view, setView] = useState('queue')
  const [tickets, setTickets] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [selected, setSelected] = useState(null)
  const [overview, setOverview] = useState(null)
  const [filters, setFilters] = useState({ status: '', category: '', priority: '', q: '' })
  const [cursor, setCursor] = useState(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [detailOpen, setDetailOpen] = useState(false)
  const [pendingClarification, setPendingClarification] = useState(null)

  const baseQuery = useMemo(() => {
    const params = new URLSearchParams({ limit: '30' })
    const status = view === 'resolved' ? 'resolved' : filters.status
    if (status) params.append('status', status)
    if (filters.category) params.set('category', filters.category)
    if (filters.priority) params.set('priority', filters.priority)
    if (filters.q.trim()) params.set('q', filters.q.trim())
    if (view === 'mine') params.set('assigned_user_id', 'current_user')
    return params
  }, [view, filters])

  const loadTickets = useCallback(async (append = false, pageCursor = null) => {
    setLoading(true); setError('')
    try {
      const params = new URLSearchParams(baseQuery)
      if (pageCursor) params.set('cursor', pageCursor)
      const result = await api(`/tickets?${params}`)
      setTickets((items) => append ? [...items, ...result.items] : result.items)
      setCursor(result.next_cursor)
      if (!append && result.items.length && !selectedId) setSelectedId(result.items[0].ticket_id)
    } catch (err) { setError(err.message); if (!append) setTickets([]) } finally { setLoading(false) }
  }, [baseQuery, selectedId])

  useEffect(() => { if (view !== 'assistant') loadTickets(false, null) }, [view, filters.status, filters.category, filters.priority, filters.q]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (!selectedId) { setSelected(null); setOverview(null); return } Promise.all([api(`/tickets/${selectedId}`), api(`/tickets/${selectedId}/overview`), api(`/tickets/${selectedId}/pending-interrupt`)]).then(([ticket, detail, pending]) => { setSelected(ticket); setOverview(detail); setPendingClarification(pending.interrupt || null) }).catch((err) => setError(err.message)) }, [selectedId])

  const transition = async (action) => {
    setBusy(true); setError('')
    try { const updated = await api(`/tickets/${selected.ticket_id}/transitions`, { method: 'POST', body: JSON.stringify({ ...action, expected_version: selected.version }) }); setSelected(updated); setTickets((items) => items.map((item) => item.ticket_id === updated.ticket_id ? updated : item)) } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  const survey = async () => { setBusy(true); try { await api(`/tickets/${selected.ticket_id}/survey`, { method: 'POST', body: JSON.stringify({ expires_in_days: 7 }) }) } catch (err) { setError(err.message) } finally { setBusy(false) } }
  const resumeClarification = async (payload) => {
    setBusy(true); setError('')
    try {
      const result = await api(`/tickets/${selected.ticket_id}/resume`, { method: 'POST', body: JSON.stringify(payload) })
      setSelected(result.ticket)
      setTickets((items) => items.map((item) => item.ticket_id === result.ticket.ticket_id ? result.ticket : item))
      setPendingClarification(result.interrupt || null)
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  if (view === 'assistant') return <div className="app-shell"><Sidebar view={view} setView={setView} mobileOpen={sidebarOpen} onClose={() => setSidebarOpen(false)} /><AssistantView /></div>

  return <div className="app-shell"><Sidebar view={view} setView={setView} mobileOpen={sidebarOpen} onClose={() => setSidebarOpen(false)} /><main className={`workspace ${detailOpen ? 'show-detail' : ''}`}><section className="queue-pane"><header className="topbar"><button className="icon-button mobile-only" onClick={() => setSidebarOpen(true)} aria-label="打开导航"><Menu /></button><div><span className="eyebrow">服务台</span><h1>{view === 'resolved' ? '已解决工单' : view === 'mine' ? '我的处理' : '工单队列'}</h1></div><button className="primary-action" onClick={() => setCreateOpen(true)}><Plus size={17} />新建</button></header><div className="filters"><div className="search-box"><Search size={16} /><input placeholder="搜索工单" value={filters.q} onChange={(e) => { setCursor(null); setFilters({ ...filters, q: e.target.value }) }} /></div><label><Filter size={15} /><select value={filters.status} onChange={(e) => { setCursor(null); setFilters({ ...filters, status: e.target.value }) }} disabled={view === 'resolved'}><option value="">全部状态</option><option value="new">新建</option><option value="queued">待分派</option><option value="assigned">已分派</option><option value="in_progress">处理中</option><option value="awaiting_customer">等待客户</option><option value="resolved">已解决</option></select></label><label><select value={filters.category} onChange={(e) => { setCursor(null); setFilters({ ...filters, category: e.target.value }) }}><option value="">全部类别</option>{Object.entries(categoryLabel).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label><select value={filters.priority} onChange={(e) => { setCursor(null); setFilters({ ...filters, priority: e.target.value }) }}><option value="">全部优先级</option><option value="urgent">紧急</option><option value="high">高</option><option value="normal">普通</option><option value="low">低</option></select></label><button className="icon-button" onClick={() => loadTickets(false, null)} aria-label="刷新"><RefreshCw size={17} /></button></div>{error && <div className="error-banner"><AlertCircle size={16} /><span>{error}</span><button onClick={() => setError('')}><X size={15} /></button></div>}<TicketList tickets={tickets} selectedId={selectedId} onSelect={(id) => { setSelectedId(id); setDetailOpen(true) }} loading={loading} hasMore={Boolean(cursor)} onMore={() => loadTickets(true, cursor)} /></section><TicketDetail ticket={selected} overview={overview} busy={busy} pendingClarification={pendingClarification} onTransition={transition} onSurvey={survey} onResume={resumeClarification} onBack={() => setDetailOpen(false)} /></main><CreateTicketDialog open={createOpen} onClose={() => setCreateOpen(false)} onCreated={(result) => { const ticket = result.ticket || result; setTickets((items) => [ticket, ...items]); setSelectedId(ticket.ticket_id); setPendingClarification(result.interrupt || null); setDetailOpen(true) }} /></div>
}

export default App
