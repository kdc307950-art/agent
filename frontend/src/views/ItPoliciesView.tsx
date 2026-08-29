import { useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  Plus,
  RefreshCw,
  Search,
  Settings,
  SlidersHorizontal,
  Trash2,
} from 'lucide-react'
import type { ItPolicy, TicketPriority } from '../types'
import { ApiError, deleteItPolicy, listItPolicies, upsertItPolicy } from '../api'

interface PolicyForm {
  category: string
  policy_id: string
  required_fields: string
  default_priority: TicketPriority
  approval_required: boolean
  auto_answer_enabled: boolean
}

const emptyForm: PolicyForm = {
  category: '',
  policy_id: '',
  required_fields: '',
  default_priority: 'normal',
  approval_required: false,
  auto_answer_enabled: false,
}

function formatError(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return String(err)
}

export default function ItPoliciesView() {
  const [policies, setPolicies] = useState<ItPolicy[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [editing, setEditing] = useState<ItPolicy | null>(null)
  const [form, setForm] = useState<PolicyForm>(emptyForm)
  const [busyId, setBusyId] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const data = await listItPolicies()
      setPolicies(data.items ?? [])
    } catch (err) {
      setError(formatError(err))
      setPolicies([])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return policies
    return policies.filter(
      (p) =>
        p.category.toLowerCase().includes(q) ||
        p.policy_id.toLowerCase().includes(q) ||
        (p.required_fields ?? []).some((f) => f.toLowerCase().includes(q)),
    )
  }, [policies, search])

  const startEdit = (item?: ItPolicy) => {
    if (item) {
      setEditing(item)
      setForm({
        category: item.category,
        policy_id: item.policy_id,
        required_fields: (item.required_fields ?? []).join(','),
        default_priority: item.default_priority ?? 'normal',
        approval_required: Boolean(item.approval_required),
        auto_answer_enabled: Boolean(item.auto_answer_enabled),
      })
    } else {
      setEditing({} as ItPolicy)
      setForm(emptyForm)
    }
  }

  const save = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusyId(editing?.category ?? 'new')
    setError('')
    try {
      await upsertItPolicy(form.category, {
        category: form.category,
        policy_id: form.policy_id,
        required_fields: form.required_fields
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
        default_priority: form.default_priority,
        approval_required: form.approval_required,
        auto_answer_enabled: form.auto_answer_enabled,
      })
      setForm(emptyForm)
      setEditing(null)
      await load()
    } catch (err) {
      setError(formatError(err))
    } finally {
      setBusyId(null)
    }
  }

  const remove = async (category: string) => {
    if (!window.confirm(`确认删除策略 ${category} 吗？引用该策略的工单分类可能受影响。`)) return
    setBusyId(category)
    setError('')
    try {
      await deleteItPolicy(category)
      await load()
    } catch (err) {
      setError(formatError(err))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <main className="assistant-view">
      <header>
        <div>
          <span className="eyebrow">管理</span>
          <h1>IT 策略配置</h1>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="icon-button" onClick={load} aria-label="刷新">
            <RefreshCw />
          </button>
          <button className="primary-action" onClick={() => startEdit()}>
            <Plus size={17} />
            新建策略
          </button>
        </div>
      </header>

      <div className="assistant-thread">
        <div className="search-box">
          <Search size={16} />
          <input
            placeholder="搜索分类、SLA 策略 ID、必填字段"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        {error && (
          <div className="form-error">
            <AlertCircle size={16} />
            {error}
          </div>
        )}

        {loading && <p style={{ color: '#69747d' }}>加载中…</p>}

        {!loading && filtered.length === 0 && (
          <div className="assistant-empty">
            <Settings size={34} />
            <h2>暂无 IT 策略</h2>
          </div>
        )}

        {filtered.map((item) => (
          <div key={item.category} className="requester" style={{ alignItems: 'flex-start' }}>
            <SlidersHorizontal />
            <div style={{ flex: 1 }}>
              <strong>{item.category}</strong>
              <span>
                SLA: {item.policy_id} · 默认优先级 {item.default_priority}
                {item.approval_required ? ' · 需审批' : ''}
                {item.auto_answer_enabled ? ' · 自动回答' : ''}
                {item.required_fields && item.required_fields.length > 0
                  ? ` · 必填 ${item.required_fields.join(', ')}`
                  : ''}
              </span>
            </div>
            <div style={{ display: 'flex', gap: 6 }}>
              <button
                className="icon-button"
                onClick={() => startEdit(item)}
                disabled={busyId === item.category}
                aria-label="编辑"
              >
                <Settings size={16} />
              </button>
              <button
                className="icon-button"
                onClick={() => remove(item.category)}
                disabled={busyId === item.category}
                aria-label="删除"
              >
                <Trash2 size={16} />
              </button>
            </div>
          </div>
        ))}
      </div>

      {editing && (
        <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && setEditing(null)}>
          <form className="modal" onSubmit={save}>
            <header>
              <div>
                <span className="eyebrow">{editing.category ? '编辑策略' : '新建策略'}</span>
                <h2>{editing.category || 'IT 策略'}</h2>
              </div>
              <button type="button" className="icon-button" onClick={() => setEditing(null)} aria-label="关闭">
                ×
              </button>
            </header>
            <label>
              分类（支持 it.vpn 等子分类）
              <input
                required
                value={form.category}
                onChange={(e) => setForm({ ...form, category: e.target.value })}
                disabled={Boolean(editing.category)}
              />
            </label>
            <label>
              SLA 策略 ID（引用 sla_policies）
              <input
                required
                value={form.policy_id}
                onChange={(e) => setForm({ ...form, policy_id: e.target.value })}
              />
            </label>
            <label>
              必填字段（逗号分隔）
              <input
                value={form.required_fields}
                onChange={(e) => setForm({ ...form, required_fields: e.target.value })}
              />
            </label>
            <label>
              默认优先级
              <select
                value={form.default_priority}
                onChange={(e) => setForm({ ...form, default_priority: e.target.value as TicketPriority })}
              >
                <option value="low">低</option>
                <option value="normal">普通</option>
                <option value="high">高</option>
                <option value="urgent">紧急</option>
              </select>
            </label>
            <label>
              <input
                type="checkbox"
                checked={form.approval_required}
                onChange={(e) => setForm({ ...form, approval_required: e.target.checked })}
              />{' '}
              需要审批
            </label>
            <label>
              <input
                type="checkbox"
                checked={form.auto_answer_enabled}
                onChange={(e) => setForm({ ...form, auto_answer_enabled: e.target.checked })}
              />{' '}
              允许自动回答
            </label>
            <footer>
              <button type="button" className="secondary-action" onClick={() => setEditing(null)}>
                取消
              </button>
              <button className="primary-action" disabled={busyId !== null}>
                {busyId !== null ? '保存中…' : '保存策略'}
              </button>
            </footer>
          </form>
        </div>
      )}
    </main>
  )
}
