/**
 * IT 资产视图（AssetsView.tsx）。
 *
 * 职责：
 * - 展示资产台账列表：支持关键词搜索（名称/编号/主机名/部门）与类型筛选，前端本地分页（每页 PAGE_SIZE=10）。
 * - 提供新建 / 编辑 / 删除资产的操作入口（弹窗表单，删除需 window.confirm 确认）。
 *
 * 与后端 API 的对应关系（src/api）：
 * - listAssets：拉取全部资产（一次性加载，搜索/分页在本地完成）。
 * - createAsset / updateAsset / deleteAsset：增删改。
 *
 * 关键交互逻辑：
 * - busyId 记录正在操作的资产 id：操作期间禁用该行的编辑/删除按钮与提交按钮，防止重复提交。
 * - 搜索/类型筛选变化时自动把页码重置回第一页，避免停留在越界的页码。
 * - 新建与编辑共用同一个弹窗与表单：editing 为空对象（{} as Asset）表示新建，含 asset_id 表示编辑。
 */
import { useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  Boxes,
  ChevronLeft,
  ChevronRight,
  Pencil,
  RefreshCw,
  Search,
  Trash2,
} from 'lucide-react'
import type { Asset } from '../types'
import { ApiError, createAsset, deleteAsset, listAssets, updateAsset } from '../api'

// 资产表单字段（新建与编辑共用）
interface AssetForm {
  asset_id: string
  asset_no: string
  asset_type: string
  name: string
  hostname: string
  department: string
  owner_user_id: string
}

// 空表单初始值：类型默认 laptop
const emptyForm: AssetForm = {
  asset_id: '',
  asset_no: '',
  asset_type: 'laptop',
  name: '',
  hostname: '',
  department: '',
  owner_user_id: '',
}

// 每页展示的资产数量（本地分页）
const PAGE_SIZE = 10
// 可选资产类型（与后端枚举一致）
const ASSET_TYPES = ['laptop', 'desktop', 'mobile', 'printer', 'other']

function formatError(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return String(err)
}

export default function AssetsView() {
  const [items, setItems] = useState<Asset[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  // 搜索关键词（本地过滤）
  const [search, setSearch] = useState('')
  // 类型筛选（本地过滤）
  const [typeFilter, setTypeFilter] = useState('')
  // 当前页码（0 起）
  const [page, setPage] = useState(0)
  // 正在编辑的资产：null 关闭弹窗；{} as Asset 表示新建模式；含数据表示编辑模式
  const [editing, setEditing] = useState<Asset | null>(null)
  // 弹窗表单值
  const [form, setForm] = useState<AssetForm>(emptyForm)
  // 正在执行操作的资产 id（'new' 表示正在提交新建表单），用于禁用按钮防重复提交
  const [busyId, setBusyId] = useState<string | null>(null)

  // 从后端重新拉取全部资产
  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const data = await listAssets()
      setItems(data.items ?? [])
    } catch (err) {
      setError(formatError(err))
      setItems([])
    } finally {
      setLoading(false)
    }
  }

  // 挂载时加载一次资产列表
  useEffect(() => {
    void load()
  }, [])

  // 本地过滤：先按类型精确匹配，再按关键词模糊匹配名称/编号/主机名/部门
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return items.filter((item) => {
      if (typeFilter && item.asset_type !== typeFilter) return false
      if (!q) return true
      return (
        (item.name ?? '').toLowerCase().includes(q) ||
        (item.asset_no ?? '').toLowerCase().includes(q) ||
        (item.hostname ?? '').toLowerCase().includes(q) ||
        (item.department ?? '').toLowerCase().includes(q)
      )
    })
  }, [items, search, typeFilter])

  // 本地分页：按 PAGE_SIZE 切出当前页数据；至少 1 页（空列表时也显示第 1 页）
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const pageItems = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)

  // 搜索词或类型筛选变化时回到第一页，避免停留在旧数据的页码上
  useEffect(() => {
    setPage(0)
  }, [search, typeFilter])

  // 打开编辑弹窗：把资产字段回填到表单（null 兜底为空字符串）
  const startEdit = (item: Asset) => {
    setEditing(item)
    setForm({
      asset_id: item.asset_id,
      asset_no: item.asset_no,
      asset_type: item.asset_type,
      name: item.name ?? '',
      hostname: item.hostname ?? '',
      department: item.department ?? '',
      owner_user_id: item.owner_user_id ?? '',
    })
  }

  // 提交表单：编辑模式走 updateAsset（空字符串转 null 表示不更新该字段），新建模式走 createAsset；成功后重新加载
  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusyId(editing?.asset_id ?? 'new')
    setError('')
    try {
      if (editing) {
        await updateAsset(editing.asset_id, {
          name: form.name || null,
          hostname: form.hostname || null,
          department: form.department || null,
          owner_user_id: form.owner_user_id || null,
        })
      } else {
        await createAsset(form)
      }
      setForm(emptyForm)
      setEditing(null)
      await load()
    } catch (err) {
      setError(formatError(err))
    } finally {
      setBusyId(null)
    }
  }

  // 删除资产：先确认（删除不可恢复），成功后重新加载列表
  const remove = async (asset: Asset) => {
    if (!window.confirm(`确认删除资产 ${asset.name || asset.asset_id}（${asset.asset_no}）吗？`)) return
    setBusyId(asset.asset_id)
    setError('')
    try {
      await deleteAsset(asset.asset_id)
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
          <span className="eyebrow">资产台账</span>
          <h1>IT 资产</h1>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {/* 手动刷新资产列表 */}
          <button className="icon-button" onClick={load} aria-label="刷新">
            <RefreshCw />
          </button>
          {/* 新建：以空对象进入编辑模式，弹窗标题/表单会按「无 asset_id」切换为新建 */}
          <button className="primary-action" onClick={() => setEditing({} as Asset)}>
            新建资产
          </button>
        </div>
      </header>

      <div className="assistant-thread">
        {/* 筛选区：关键词搜索 + 类型下拉（均为本地过滤） */}
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <div className="search-box" style={{ flex: 1, minWidth: 200 }}>
            <Search size={16} />
            <input
              placeholder="搜索名称、编号、主机名、部门"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <label>
            <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
              <option value="">全部类型</option>
              {ASSET_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
        </div>

        {/* 错误提示条 */}
        {error && (
          <div className="form-error">
            <AlertCircle size={16} />
            {error}
          </div>
        )}

        {loading && <p style={{ color: '#69747d' }}>加载中…</p>}

        {/* 空状态：加载完成且当前页无数据时提示 */}
        {!loading && pageItems.length === 0 && (
          <div className="assistant-empty">
            <Boxes size={34} />
            <h2>暂无资产</h2>
          </div>
        )}

        {/* 资产行：名称/编号/类型/主机名/部门/使用人 + 编辑、删除按钮（操作中禁用） */}
        {pageItems.map((item) => (
          <div key={item.asset_id} className="requester" style={{ alignItems: 'flex-start' }}>
            <Boxes />
            <div style={{ flex: 1 }}>
              <strong>{item.name || item.asset_id}</strong>
              <span>
                {item.asset_no} · {item.asset_type}
                {item.hostname ? ` · ${item.hostname}` : ''}
                {item.department ? ` · ${item.department}` : ''}
                {item.owner_user_id ? ` · 使用人 ${item.owner_user_id}` : ''}
              </span>
            </div>
            <div style={{ display: 'flex', gap: 6 }}>
              {/* 编辑按钮：回填表单打开弹窗 */}
              <button
                className="icon-button"
                onClick={() => startEdit(item)}
                disabled={busyId === item.asset_id}
                aria-label="编辑"
              >
                <Pencil size={16} />
              </button>
              {/* 删除按钮：先确认再删除 */}
              <button
                className="icon-button"
                onClick={() => remove(item)}
                disabled={busyId === item.asset_id}
                aria-label="删除"
              >
                <Trash2 size={16} />
              </button>
            </div>
          </div>
        ))}

        {/* 本地分页：多于一页时显示上一页/页码/下一页 */}
        {pageCount > 1 && (
          <div style={{ display: 'flex', justifyContent: 'center', gap: 8, marginTop: 12 }}>
            <button
              className="icon-button"
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={page === 0}
              aria-label="上一页"
            >
              <ChevronLeft />
            </button>
            <span style={{ lineHeight: '32px', fontSize: 13 }}>
              {page + 1} / {pageCount}
            </span>
            <button
              className="icon-button"
              onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
              disabled={page === pageCount - 1}
              aria-label="下一页"
            >
              <ChevronRight />
            </button>
          </div>
        )}
      </div>

      {/* 编辑/新建弹窗：点击遮罩空白处（target === currentTarget）关闭 */}
      {editing && (
        <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && setEditing(null)}>
          <form className="modal" onSubmit={submit}>
            <header>
              <div>
                <span className="eyebrow">{editing.asset_id ? '编辑资产' : '新建资产'}</span>
                <h2>{editing.asset_id ? form.name || form.asset_id : '登记资产'}</h2>
              </div>
              <button type="button" className="icon-button" onClick={() => setEditing(null)} aria-label="关闭">
                ×
              </button>
            </header>
            <label>
              资产编号
              <input required value={form.asset_no} onChange={(e) => setForm({ ...form, asset_no: e.target.value })} />
            </label>
            <label>
              类型
              <select value={form.asset_type} onChange={(e) => setForm({ ...form, asset_type: e.target.value })}>
                {ASSET_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
            {/* 仅新建模式允许填写资产 ID（编辑时不可改） */}
            {!editing.asset_id && (
              <label>
                资产 ID
                <input required value={form.asset_id} onChange={(e) => setForm({ ...form, asset_id: e.target.value })} />
              </label>
            )}
            <label>
              名称
              <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </label>
            <label>
              主机名
              <input value={form.hostname} onChange={(e) => setForm({ ...form, hostname: e.target.value })} />
            </label>
            <label>
              部门
              <input value={form.department} onChange={(e) => setForm({ ...form, department: e.target.value })} />
            </label>
            <label>
              使用人 ID
              <input value={form.owner_user_id} onChange={(e) => setForm({ ...form, owner_user_id: e.target.value })} />
            </label>
            <footer>
              <button type="button" className="secondary-action" onClick={() => setEditing(null)}>
                取消
              </button>
              {/* 提交按钮：提交期间禁用并显示「保存中…」 */}
              <button className="primary-action" disabled={busyId !== null}>
                {busyId !== null ? '保存中…' : '保存'}
              </button>
            </footer>
          </form>
        </div>
      )}
    </main>
  )
}
