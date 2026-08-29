/**
 * IT 策略配置视图（ItPoliciesView.tsx）。
 *
 * 职责：
 * - 按分类维护 IT 服务策略（category → 策略配置），支持关键词搜索（分类/SLA 策略 ID/必填字段）。
 * - 新建 / 编辑 / 删除策略；编辑时分类不可改（分类是策略的主键）。
 *
 * 与后端 API 的对应关系（src/api）：
 * - listItPolicies：拉取全部策略。
 * - upsertItPolicy：新建或更新（按 category 幂等写入）。
 * - deleteItPolicy：删除策略（删除前提示引用该策略的工单分类可能受影响）。
 *
 * 关键交互逻辑：
 * - required_fields 在表单中用逗号分隔文本，提交时拆分、trim、过滤空项后转为数组。
 * - 新建与编辑共用弹窗：editing 为空对象表示新建，含 category 表示编辑（分类输入框禁用）。
 * - busyId 记录正在操作的分类，操作期间禁用按钮防重复提交。
 */
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

// 策略表单字段（与后端 ItPolicy 对应；required_fields 在表单中为逗号分隔文本）
interface PolicyForm {
  category: string
  policy_id: string
  required_fields: string
  default_priority: TicketPriority
  approval_required: boolean
  auto_answer_enabled: boolean
}

// 空表单初始值：默认优先级 normal、无需审批、不自动回答
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
  // 搜索关键词（本地过滤）
  const [search, setSearch] = useState('')
  // 正在编辑的策略：null 关闭弹窗；{} as ItPolicy 表示新建；含 category 表示编辑
  const [editing, setEditing] = useState<ItPolicy | null>(null)
  // 弹窗表单值
  const [form, setForm] = useState<PolicyForm>(emptyForm)
  // 正在操作（保存/删除）的分类，用于禁用按钮防重复提交
  const [busyId, setBusyId] = useState<string | null>(null)

  // 从后端重新拉取全部策略
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

  // 挂载时加载一次
  useEffect(() => {
    void load()
  }, [])

  // 本地搜索：匹配分类 / SLA 策略 ID / 任一必填字段
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

  // 打开编辑弹窗：有 item 则回填表单（数组字段 join 成逗号文本）；无 item 则为新建（空表单）
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

  // 保存策略：按 category 调用 upsertItPolicy（新建/更新幂等），成功后刷新列表
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

  // 删除策略：先确认（引用该分类的工单可能受影响），成功后刷新列表
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
          {/* 手动刷新策略列表 */}
          <button className="icon-button" onClick={load} aria-label="刷新">
            <RefreshCw />
          </button>
          {/* 新建策略：无参数调用 startEdit 进入空表单 */}
          <button className="primary-action" onClick={() => startEdit()}>
            <Plus size={17} />
            新建策略
          </button>
        </div>
      </header>

      <div className="assistant-thread">
        {/* 搜索框：本地过滤 */}
        <div className="search-box">
          <Search size={16} />
          <input
            placeholder="搜索分类、SLA 策略 ID、必填字段"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        {/* 错误提示条 */}
        {error && (
          <div className="form-error">
            <AlertCircle size={16} />
            {error}
          </div>
        )}

        {loading && <p style={{ color: '#69747d' }}>加载中…</p>}

        {/* 空状态：无匹配策略时提示 */}
        {!loading && filtered.length === 0 && (
          <div className="assistant-empty">
            <Settings size={34} />
            <h2>暂无 IT 策略</h2>
          </div>
        )}

        {/* 策略行：展示分类、SLA 策略、默认优先级、审批/自动回答开关与必填字段；操作中禁用按钮 */}
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
              {/* 编辑按钮：回填表单打开弹窗 */}
              <button
                className="icon-button"
                onClick={() => startEdit(item)}
                disabled={busyId === item.category}
                aria-label="编辑"
              >
                <Settings size={16} />
              </button>
              {/* 删除按钮：先确认再删除 */}
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

      {/* 编辑/新建弹窗：点击遮罩空白处关闭；编辑时分类输入框禁用（分类为主键） */}
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
            {/* 需要审批：开启后该类工单进入 Agent 审批中断流程 */}
            <label>
              <input
                type="checkbox"
                checked={form.approval_required}
                onChange={(e) => setForm({ ...form, approval_required: e.target.checked })}
              />{' '}
              需要审批
            </label>
            {/* 允许自动回答：开启后 Agent 可自动回复该类请求 */}
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
              {/* 保存中禁用并显示进度文案 */}
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
