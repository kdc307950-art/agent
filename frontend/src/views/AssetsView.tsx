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

interface AssetForm {
  asset_id: string
  asset_no: string
  asset_type: string
  name: string
  hostname: string
  department: string
  owner_user_id: string
}

const emptyForm: AssetForm = {
  asset_id: '',
  asset_no: '',
  asset_type: 'laptop',
  name: '',
  hostname: '',
  department: '',
  owner_user_id: '',
}

const PAGE_SIZE = 10
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
  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [page, setPage] = useState(0)
  const [editing, setEditing] = useState<Asset | null>(null)
  const [form, setForm] = useState<AssetForm>(emptyForm)
  const [busyId, setBusyId] = useState<string | null>(null)

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

  useEffect(() => {
    void load()
  }, [])

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

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const pageItems = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)

  useEffect(() => {
    setPage(0)
  }, [search, typeFilter])

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
          <button className="icon-button" onClick={load} aria-label="刷新">
            <RefreshCw />
          </button>
          <button className="primary-action" onClick={() => setEditing({} as Asset)}>
            新建资产
          </button>
        </div>
      </header>

      <div className="assistant-thread">
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

        {error && (
          <div className="form-error">
            <AlertCircle size={16} />
            {error}
          </div>
        )}

        {loading && <p style={{ color: '#69747d' }}>加载中…</p>}

        {!loading && pageItems.length === 0 && (
          <div className="assistant-empty">
            <Boxes size={34} />
            <h2>暂无资产</h2>
          </div>
        )}

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
              <button
                className="icon-button"
                onClick={() => startEdit(item)}
                disabled={busyId === item.asset_id}
                aria-label="编辑"
              >
                <Pencil size={16} />
              </button>
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
