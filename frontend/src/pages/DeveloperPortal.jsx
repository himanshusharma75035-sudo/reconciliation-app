import React, { useState, useEffect, useRef } from 'react'
import {
  Code2, BookOpen, Database, Network, Activity, ScrollText,
  GitBranch, Search, ChevronRight, ChevronDown, RefreshCw, KeyRound,
  Sparkles, Send, Plus, Trash2, Wrench, ShieldCheck, Inbox, X, Bot,
  CalendarClock, Play, Power, History, GitCommit, MessageSquare,
  ExternalLink, AlertTriangle, Lightbulb, Clock,
  Hammer, GitCompare, CheckCircle2, XCircle, RotateCcw, Rocket, ShieldAlert,
  GraduationCap, Terminal, BarChart3,
} from 'lucide-react'
import { createPortal } from 'react-dom'
import toast from 'react-hot-toast'
import api from '../utils/api'
import { isAdmin, hasPermission } from '../utils/permissions'
import Markdown from '../components/Markdown'

const TABS = [
  { id: 'agent',  label: 'Agent',         icon: Sparkles },
  { id: 'builder', label: 'Builder',      icon: Hammer, perm: 'portal_build' },
  { id: 'requests', label: 'Requests',    icon: Inbox },
  { id: 'scheduled', label: 'Scheduled',  icon: CalendarClock },
  { id: 'changelog', label: "What's New", icon: History },
  { id: 'onboarding', label: 'Onboarding', icon: GraduationCap },
  { id: 'docs',   label: 'Documentation', icon: BookOpen },
  { id: 'schema', label: 'Database Schema', icon: Database },
  { id: 'explorer', label: 'Data Explorer', icon: Terminal },
  { id: 'api',    label: 'API Surface',   icon: Network },
  { id: 'metrics', label: 'Metrics',      icon: BarChart3 },
  { id: 'health', label: 'Live Health',   icon: Activity },
  { id: 'audit',  label: 'Activity',      icon: ScrollText },
]

const REQ_TYPES = [
  { id: 'bug', label: 'Bug' },
  { id: 'faulty_data', label: 'Faulty data' },
  { id: 'feature', label: 'Feature' },
  { id: 'change', label: 'Change' },
  { id: 'other', label: 'Other' },
]
const STATUS_STYLE = {
  open: 'badge-yellow', triaged: 'badge-blue', approved: 'badge-green',
  rejected: 'badge-red', done: 'badge-gray',
}
const PRIORITY_STYLE = { high: 'badge-red', medium: 'badge-yellow', low: 'badge-gray' }

const METHOD_COLORS = {
  GET: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  POST: 'bg-blue-50 text-blue-700 border-blue-200',
  PUT: 'bg-amber-50 text-amber-700 border-amber-200',
  PATCH: 'bg-purple-50 text-purple-700 border-purple-200',
  DELETE: 'bg-red-50 text-red-700 border-red-200',
}

function errMsg(err, fallback = 'Failed to load') {
  if (err?.response?.status === 403) return 'You do not have Developer Portal access.'
  return err?.response?.data?.detail || fallback
}

// ── Documentation tab ─────────────────────────────────────────────────────────
function DocsTab() {
  const [docs, setDocs] = useState([])
  const [active, setActive] = useState(null)
  const [content, setContent] = useState(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    api.get('/portal/docs')
      .then(({ data }) => {
        setDocs(data.docs || [])
        if (data.docs?.length) openDoc(data.docs[0].name)
      })
      .catch(err => toast.error(errMsg(err)))
  }, [])

  const openDoc = async (name) => {
    setActive(name); setLoading(true); setContent(null)
    try {
      const { data } = await api.get(`/portal/docs/${name}`)
      setContent(data)
    } catch (err) { toast.error(errMsg(err, 'Could not load document')) }
    finally { setLoading(false) }
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-[230px_1fr] gap-5">
      <div className="card p-2 h-fit md:sticky md:top-4">
        <div className="text-[11px] uppercase tracking-wide text-gray-400 px-2 py-1.5 font-semibold">Documents</div>
        {docs.map(d => (
          <button key={d.name} onClick={() => openDoc(d.name)}
            className={`w-full text-left px-2.5 py-2 rounded-lg text-sm flex items-center gap-2 transition-colors
              ${active === d.name ? 'bg-primary/10 text-primary font-medium' : 'text-gray-600 hover:bg-gray-50'}`}>
            <BookOpen size={14} className="shrink-0" />
            <span className="truncate">{d.title}</span>
          </button>
        ))}
        {!docs.length && <div className="px-2.5 py-2 text-xs text-gray-400">No documents found.</div>}
      </div>
      <div className="card p-6 min-h-[300px]">
        {loading && <div className="flex items-center gap-2 text-gray-400 text-sm"><RefreshCw size={14} className="animate-spin" /> Loading…</div>}
        {!loading && content && <Markdown content={content.content} />}
        {!loading && !content && <div className="text-gray-400 text-sm">Select a document to read.</div>}
      </div>
    </div>
  )
}

// ── Database schema tab ───────────────────────────────────────────────────────
function SchemaTab() {
  const [data, setData] = useState(null)
  const [q, setQ] = useState('')
  const [open, setOpen] = useState({})

  useEffect(() => {
    api.get('/portal/schema').then(({ data }) => setData(data)).catch(err => toast.error(errMsg(err)))
  }, [])

  if (!data) return <div className="text-gray-400 text-sm flex items-center gap-2"><RefreshCw size={14} className="animate-spin" /> Loading schema…</div>
  const tables = data.tables.filter(t => t.name.toLowerCase().includes(q.toLowerCase()))

  return (
    <div>
      <div className="flex items-center justify-between mb-3 gap-3 flex-wrap">
        <div className="text-sm text-gray-500">{data.table_count} tables</div>
        <div className="relative">
          <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
          <input className="input pl-8 w-64" placeholder="Filter tables…" value={q} onChange={e => setQ(e.target.value)} />
        </div>
      </div>
      <div className="space-y-2">
        {tables.map(t => (
          <div key={t.name} className="card p-0 overflow-hidden">
            <button onClick={() => setOpen(o => ({ ...o, [t.name]: !o[t.name] }))}
              className="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-50">
              <div className="flex items-center gap-2">
                {open[t.name] ? <ChevronDown size={15} className="text-gray-400" /> : <ChevronRight size={15} className="text-gray-400" />}
                <Database size={14} className="text-primary" />
                <span className="font-mono text-sm font-medium text-gray-800">{t.name}</span>
              </div>
              <span className="badge-gray">{t.column_count} cols</span>
            </button>
            {open[t.name] && (
              <div className="overflow-x-auto border-t border-gray-100">
                <table className="w-full text-xs">
                  <thead className="bg-gray-50 text-gray-500">
                    <tr>
                      <th className="text-left px-4 py-2 font-semibold">Column</th>
                      <th className="text-left px-3 py-2 font-semibold">Type</th>
                      <th className="text-left px-3 py-2 font-semibold">Attributes</th>
                    </tr>
                  </thead>
                  <tbody>
                    {t.columns.map(c => (
                      <tr key={c.name} className="border-b border-gray-50 last:border-0">
                        <td className="px-4 py-1.5 font-mono text-gray-800">{c.name}</td>
                        <td className="px-3 py-1.5 font-mono text-gray-500">{c.type}</td>
                        <td className="px-3 py-1.5">
                          <div className="flex flex-wrap gap-1">
                            {c.primary_key && <span className="badge-blue">PK</span>}
                            {c.foreign_keys.map(fk => <span key={fk} className="badge-gray" title="foreign key">→ {fk}</span>)}
                            {c.unique && <span className="badge-gray">unique</span>}
                            {c.index && !c.primary_key && <span className="badge-gray">index</span>}
                            {!c.nullable && <span className="badge-gray">not null</span>}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

// ── API surface tab ───────────────────────────────────────────────────────────
function ApiTab() {
  const [data, setData] = useState(null)
  const [q, setQ] = useState('')

  useEffect(() => {
    api.get('/portal/endpoints').then(({ data }) => setData(data)).catch(err => toast.error(errMsg(err)))
  }, [])

  if (!data) return <div className="text-gray-400 text-sm flex items-center gap-2"><RefreshCw size={14} className="animate-spin" /> Loading endpoints…</div>
  const ql = q.toLowerCase()
  const groups = data.groups
    .map(g => ({ ...g, endpoints: g.endpoints.filter(e => e.path.toLowerCase().includes(ql) || (e.summary || '').toLowerCase().includes(ql) || g.tag.toLowerCase().includes(ql)) }))
    .filter(g => g.endpoints.length)

  return (
    <div>
      <div className="flex items-center justify-between mb-3 gap-3 flex-wrap">
        <div className="text-sm text-gray-500">{data.endpoint_count} endpoints across {data.tag_count} groups</div>
        <div className="relative">
          <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
          <input className="input pl-8 w-64" placeholder="Filter endpoints…" value={q} onChange={e => setQ(e.target.value)} />
        </div>
      </div>
      <div className="space-y-4">
        {groups.map(g => (
          <div key={g.tag} className="card p-0 overflow-hidden">
            <div className="bg-gray-50 px-4 py-2 text-[11px] font-semibold text-gray-500 uppercase tracking-wide border-b border-gray-100">{g.tag}</div>
            <div className="divide-y divide-gray-50">
              {g.endpoints.map((e, j) => (
                <div key={j} className="flex items-center gap-3 px-4 py-2">
                  <span className={`text-[10px] font-bold font-mono px-1.5 py-0.5 rounded border w-14 text-center shrink-0 ${METHOD_COLORS[e.method] || 'bg-gray-50 text-gray-600 border-gray-200'}`}>{e.method}</span>
                  <span className="font-mono text-xs text-gray-800 shrink-0">{e.path}</span>
                  {e.summary && <span className="text-xs text-gray-400 truncate">— {e.summary}</span>}
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Live health tab ───────────────────────────────────────────────────────────
const STATUS_BADGE = {
  ok: 'badge-green', warn: 'badge-yellow', critical: 'badge-red', unknown: 'badge-gray',
}
function HealthTab() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)

  const load = () => {
    setLoading(true)
    api.get('/portal/health').then(({ data }) => setData(data))
      .catch(err => toast.error(errMsg(err)))
      .finally(() => setLoading(false))
  }
  useEffect(() => { load() }, [])

  if (loading || !data) return <div className="text-gray-400 text-sm flex items-center gap-2"><RefreshCw size={14} className="animate-spin" /> Loading health…</div>
  const rh = data.recon_health || {}
  const sum = data.ingestion_summary || {}

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-sm text-gray-500">Overall status:</span>
          <span className={STATUS_BADGE[rh.status] || 'badge-gray'}>{(rh.status || 'unknown').toUpperCase()}</span>
        </div>
        <button onClick={load} className="btn-ghost flex items-center gap-1.5 text-xs"><RefreshCw size={13} /> Refresh</button>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Stat label="DB" value={data.system?.db_ok ? `${data.system.db} ✓` : 'unreachable'} ok={data.system?.db_ok} />
        <Stat label="App version" value={data.system?.version} />
        <Stat label={`Ingests (${data.days}d)`} value={sum.events ?? 0} />
        <Stat label={`Rows accepted (${data.days}d)`} value={(sum.rows_accepted ?? 0).toLocaleString()} />
      </div>

      <div className="card p-4">
        <h3 className="text-sm font-semibold text-gray-700 mb-3">Health checks</h3>
        <div className="space-y-2">
          {(rh.checks || []).map((c, j) => (
            <div key={j} className="flex items-start justify-between gap-3 py-1.5 border-b border-gray-50 last:border-0">
              <div className="min-w-0">
                <div className="text-sm text-gray-700 font-medium">{c.name || c.check || `Check ${j + 1}`}</div>
                {c.detail && <div className="text-xs text-gray-400 mt-0.5">{c.detail}</div>}
              </div>
              <span className={STATUS_BADGE[c.status] || 'badge-gray'}>{(c.status || 'unknown').toUpperCase()}</span>
            </div>
          ))}
          {!(rh.checks || []).length && <div className="text-xs text-gray-400">No checks reported.</div>}
        </div>
      </div>
    </div>
  )
}
function Stat({ label, value, ok }) {
  return (
    <div className="card p-3">
      <div className="text-[11px] text-gray-400 uppercase tracking-wide">{label}</div>
      <div className={`text-lg font-bold mt-0.5 ${ok === false ? 'text-red-600' : 'text-gray-800'}`}>{value}</div>
    </div>
  )
}

// ── Activity / audit tab ──────────────────────────────────────────────────────
function AuditTab() {
  const [rows, setRows] = useState(null)
  const [type, setType] = useState('')

  const load = (t) => {
    api.get('/portal/audit', { params: { limit: 100, action_type: t || undefined } })
      .then(({ data }) => setRows(data.items || []))
      .catch(err => toast.error(errMsg(err)))
  }
  useEffect(() => { load(type) }, [type])

  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        {['', 'human', 'app'].map(t => (
          <button key={t} onClick={() => setType(t)}
            className={`text-xs px-3 py-1.5 rounded-lg border ${type === t ? 'bg-primary text-white border-primary' : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50'}`}>
            {t === '' ? 'All' : t === 'human' ? 'Human' : 'System'}
          </button>
        ))}
      </div>
      {!rows ? (
        <div className="text-gray-400 text-sm flex items-center gap-2"><RefreshCw size={14} className="animate-spin" /> Loading activity…</div>
      ) : (
        <div className="card p-0 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-100">
              <tr>
                <th className="table-th">Time</th>
                <th className="table-th">User</th>
                <th className="table-th">Action</th>
                <th className="table-th">Type</th>
                <th className="table-th">Entity</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.id} className="border-b border-gray-50 hover:bg-gray-50">
                  <td className="table-td text-xs text-gray-500 whitespace-nowrap">{r.created_at}</td>
                  <td className="table-td text-gray-700">{r.username || '—'}</td>
                  <td className="table-td font-mono text-xs text-gray-800">{r.action}</td>
                  <td className="table-td"><span className={r.action_type === 'human' ? 'badge-blue' : 'badge-gray'}>{r.action_type}</span></td>
                  <td className="table-td text-xs text-gray-500">{r.entity_type || '—'}</td>
                </tr>
              ))}
              {!rows.length && <tr><td colSpan={5} className="table-td text-center text-gray-400 py-6">No activity.</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── Requests / approval queue tab ─────────────────────────────────────────────
function RequestsTab() {
  const canApprove = isAdmin() || hasPermission('portal_approve')
  const [data, setData] = useState({ items: [], counts: {} })
  const [fStatus, setFStatus] = useState('')
  const [fType, setFType] = useState('')
  const [selected, setSelected] = useState(null)
  const [showCreate, setShowCreate] = useState(false)
  const [form, setForm] = useState({ type: 'bug', title: '', description: '', proposed_change: '', priority: 'medium' })
  const [note, setNote] = useState('')
  const [comments, setComments] = useState([])
  const [commentBody, setCommentBody] = useState('')
  const [assigneeDraft, setAssigneeDraft] = useState('')
  const [ghBusy, setGhBusy] = useState(false)

  const load = () => {
    const params = {}
    if (fStatus) params.status = fStatus
    if (fType) params.req_type = fType
    api.get('/portal/requests', { params }).then(({ data }) => setData(data)).catch(() => toast.error('Could not load requests'))
  }
  useEffect(() => { load() }, [fStatus, fType])

  const openDetail = async (id) => {
    try {
      const { data } = await api.get(`/portal/requests/${id}`)
      setSelected(data); setNote(data.review_note || ''); setAssigneeDraft(data.assignee || ''); setCommentBody('')
      api.get(`/portal/requests/${id}/comments`).then(({ data }) => setComments(data.comments || [])).catch(() => setComments([]))
    } catch { toast.error('Could not load request') }
  }

  const addComment = async () => {
    const b = commentBody.trim()
    if (!b || !selected) return
    try {
      const { data } = await api.post(`/portal/requests/${selected.id}/comments`, { body: b })
      setComments(c => [...c, data]); setCommentBody(''); load()
    } catch (err) { toast.error(err.response?.data?.detail || 'Failed') }
  }

  const saveAssignee = async () => {
    if (!selected) return
    try {
      const { data } = await api.patch(`/portal/requests/${selected.id}`, { assignee: assigneeDraft })
      setSelected(data); load(); toast.success(data.assignee ? `Assigned to ${data.assignee}` : 'Unassigned')
    } catch (err) { toast.error(err.response?.data?.detail || 'Failed') }
  }

  const createGithubIssue = async () => {
    if (!selected) return
    setGhBusy(true)
    try {
      const { data } = await api.get(`/portal/requests/${selected.id}/github-issue`)
      if (!data.url) { toast.error(data.reason || 'No GitHub remote configured'); return }
      window.open(data.url, '_blank', 'noopener')
      // Remember the compose link so the request shows it's been pushed to the tracker.
      const { data: upd } = await api.patch(`/portal/requests/${selected.id}`, { github_issue_url: data.url })
      setSelected(upd); load()
    } catch (err) { toast.error(err.response?.data?.detail || 'Failed') }
    finally { setGhBusy(false) }
  }

  const fmtAge = (h) => h == null ? '' : h < 1 ? '<1h' : h < 24 ? `${Math.round(h)}h` : `${Math.round(h / 24)}d`

  const submitCreate = async () => {
    if (!form.title.trim() || !form.description.trim()) { toast.error('Title and description required'); return }
    try {
      await api.post('/portal/requests', form)
      toast.success('Request filed')
      setShowCreate(false)
      setForm({ type: 'bug', title: '', description: '', proposed_change: '', priority: 'medium' })
      load()
    } catch (err) { toast.error(err.response?.data?.detail || 'Failed') }
  }

  const review = async (status) => {
    if (!selected) return
    try {
      const { data } = await api.patch(`/portal/requests/${selected.id}`, { status, review_note: note })
      setSelected(data); load(); toast.success(`Marked ${status}`)
    } catch (err) { toast.error(err.response?.data?.detail || 'Failed') }
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-3 gap-3 flex-wrap">
        <div className="flex items-center gap-1.5 flex-wrap">
          {['', 'open', 'triaged', 'approved', 'rejected', 'done'].map(s => (
            <button key={s} onClick={() => setFStatus(s)}
              className={`text-xs px-2.5 py-1 rounded-lg border ${fStatus === s ? 'bg-primary text-white border-primary' : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50'}`}>
              {s === '' ? 'All' : s}{s && data.counts?.[s] != null ? ` (${data.counts[s]})` : ''}
            </button>
          ))}
          <select className="select w-36 ml-1 text-xs" value={fType} onChange={e => setFType(e.target.value)}>
            <option value="">All types</option>
            {REQ_TYPES.map(t => <option key={t.id} value={t.id}>{t.label}</option>)}
          </select>
        </div>
        <button onClick={() => setShowCreate(true)} className="btn-primary flex items-center gap-1.5 text-sm"><Plus size={14} /> New request</button>
      </div>

      <div className="card p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b border-gray-100">
            <tr>
              <th className="table-th">Title</th><th className="table-th">Type</th>
              <th className="table-th">Priority</th><th className="table-th">Status</th>
              <th className="table-th">Assignee</th><th className="table-th">Age</th>
              <th className="table-th">Source</th><th className="table-th">Raised by</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map(r => (
              <tr key={r.id} onClick={() => openDetail(r.id)} className="border-b border-gray-50 hover:bg-gray-50 cursor-pointer">
                <td className="table-td font-medium text-gray-800 max-w-xs">
                  <div className="flex items-center gap-1.5">
                    <span className="truncate">{r.title}</span>
                    {r.github_issue_url && <ExternalLink size={11} className="text-gray-400 shrink-0" title="Linked to GitHub" />}
                    {r.comment_count > 0 && <span className="inline-flex items-center gap-0.5 text-[10px] text-gray-400 shrink-0"><MessageSquare size={10} />{r.comment_count}</span>}
                  </div>
                </td>
                <td className="table-td"><span className="badge-gray">{r.type}</span></td>
                <td className="table-td"><span className={PRIORITY_STYLE[r.priority] || 'badge-gray'}>{r.priority}</span></td>
                <td className="table-td"><span className={STATUS_STYLE[r.status] || 'badge-gray'}>{r.status}</span></td>
                <td className="table-td text-xs text-gray-600">{r.assignee || <span className="text-gray-300">—</span>}</td>
                <td className="table-td whitespace-nowrap">
                  {r.overdue
                    ? <span className="inline-flex items-center gap-1 badge-red" title="Past SLA for its priority"><Clock size={10} /> {fmtAge(r.age_hours)}</span>
                    : <span className="text-xs text-gray-400">{fmtAge(r.age_hours)}</span>}
                </td>
                <td className="table-td">{r.source === 'agent' ? <span className="inline-flex items-center gap-1 text-primary text-xs"><Bot size={12} /> agent</span> : <span className="text-xs text-gray-500">manual</span>}</td>
                <td className="table-td text-gray-600">{r.created_by}</td>
              </tr>
            ))}
            {!data.items.length && <tr><td colSpan={8} className="table-td text-center text-gray-400 py-6">No requests.</td></tr>}
          </tbody>
        </table>
      </div>

      {/* Create modal */}
      {showCreate && createPortal((
        <div className="fixed inset-0 bg-black/50 z-50 overflow-y-auto" onClick={() => setShowCreate(false)}>
          <div className="min-h-full flex items-center justify-center p-4">
            <div className="bg-white rounded-xl shadow-2xl w-full max-w-lg" onClick={e => e.stopPropagation()}>
              <div className="border-b border-gray-100 px-5 py-3 flex items-center justify-between">
                <h3 className="font-semibold text-gray-800">New request</h3>
                <button onClick={() => setShowCreate(false)} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
              </div>
              <div className="px-5 py-4 space-y-3">
                <div className="grid grid-cols-2 gap-3">
                  <div><label className="text-xs font-medium text-gray-600 block mb-1">Type</label>
                    <select className="select" value={form.type} onChange={e => setForm({ ...form, type: e.target.value })}>
                      {REQ_TYPES.map(t => <option key={t.id} value={t.id}>{t.label}</option>)}
                    </select></div>
                  <div><label className="text-xs font-medium text-gray-600 block mb-1">Priority</label>
                    <select className="select" value={form.priority} onChange={e => setForm({ ...form, priority: e.target.value })}>
                      <option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option>
                    </select></div>
                </div>
                <div><label className="text-xs font-medium text-gray-600 block mb-1">Title</label>
                  <input className="input" value={form.title} onChange={e => setForm({ ...form, title: e.target.value })} /></div>
                <div><label className="text-xs font-medium text-gray-600 block mb-1">Description</label>
                  <textarea className="input h-24 resize-none" value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} /></div>
                <div><label className="text-xs font-medium text-gray-600 block mb-1">Proposed change (optional)</label>
                  <textarea className="input h-20 resize-none" value={form.proposed_change} onChange={e => setForm({ ...form, proposed_change: e.target.value })} /></div>
              </div>
              <div className="border-t border-gray-100 bg-gray-50/50 px-5 py-3 flex gap-2">
                <button onClick={() => setShowCreate(false)} className="btn-ghost flex-1">Cancel</button>
                <button onClick={submitCreate} className="btn-primary flex-1">File request</button>
              </div>
            </div>
          </div>
        </div>
      ), document.body)}

      {/* Detail modal */}
      {selected && createPortal((
        <div className="fixed inset-0 bg-black/50 z-50 overflow-y-auto" onClick={() => setSelected(null)}>
          <div className="min-h-full flex items-center justify-center p-4">
            <div className="bg-white rounded-xl shadow-2xl w-full max-w-2xl my-4" onClick={e => e.stopPropagation()}>
              <div className="border-b border-gray-100 px-5 py-3 flex items-start justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className={STATUS_STYLE[selected.status] || 'badge-gray'}>{selected.status}</span>
                    <span className="badge-gray">{selected.type}</span>
                    <span className={PRIORITY_STYLE[selected.priority] || 'badge-gray'}>{selected.priority}</span>
                    {selected.source === 'agent' && <span className="inline-flex items-center gap-1 text-primary text-xs"><Bot size={12} /> agent-filed</span>}
                  </div>
                  <h3 className="font-semibold text-gray-800 mt-1.5">{selected.title}</h3>
                  <p className="text-xs text-gray-400">Raised by {selected.created_by} · {selected.created_at}</p>
                </div>
                <button onClick={() => setSelected(null)} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
              </div>
              <div className="px-5 py-4 space-y-4 text-sm">
                <div><div className="text-[11px] uppercase tracking-wide text-gray-400 mb-1">Description</div>
                  <div className="text-gray-700 whitespace-pre-wrap">{selected.description}</div></div>
                {selected.proposed_change && (
                  <div><div className="text-[11px] uppercase tracking-wide text-gray-400 mb-1">Proposed change</div>
                    <pre className="bg-gray-50 border border-gray-100 rounded-lg p-3 text-xs whitespace-pre-wrap">{selected.proposed_change}</pre></div>
                )}
                {selected.reviewed_by && (
                  <div className="text-xs text-gray-500">Reviewed by {selected.reviewed_by} · {selected.reviewed_at}{selected.review_note ? ` — “${selected.review_note}”` : ''}</div>
                )}

                {/* Workflow: assignee + tracker link */}
                <div className="border-t border-gray-100 pt-3 grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <div className="text-[11px] uppercase tracking-wide text-gray-400 mb-1">Assignee</div>
                    {canApprove ? (
                      <div className="flex items-center gap-2">
                        <input className="input flex-1 text-sm" placeholder="username" value={assigneeDraft}
                          onChange={e => setAssigneeDraft(e.target.value)} />
                        <button onClick={saveAssignee} className="btn-ghost text-xs shrink-0">Save</button>
                      </div>
                    ) : (
                      <div className="text-sm text-gray-700">{selected.assignee || <span className="text-gray-400">Unassigned</span>}</div>
                    )}
                  </div>
                  <div>
                    <div className="text-[11px] uppercase tracking-wide text-gray-400 mb-1">GitHub issue</div>
                    {selected.github_issue_url ? (
                      <a href={selected.github_issue_url} target="_blank" rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 text-sm text-primary hover:underline break-all">
                        <ExternalLink size={13} /> Open linked issue
                      </a>
                    ) : canApprove ? (
                      <button onClick={createGithubIssue} disabled={ghBusy}
                        className="btn-ghost text-xs flex items-center gap-1.5 disabled:opacity-50">
                        {ghBusy ? <RefreshCw size={12} className="animate-spin" /> : <GitBranch size={12} />} Create GitHub issue
                      </button>
                    ) : (
                      <div className="text-xs text-gray-400">Not linked</div>
                    )}
                  </div>
                </div>

                {/* Discussion */}
                <div className="border-t border-gray-100 pt-3">
                  <div className="text-[11px] uppercase tracking-wide text-gray-400 mb-2 flex items-center gap-1.5">
                    <MessageSquare size={12} /> Discussion {comments.length > 0 && `(${comments.length})`}
                  </div>
                  <div className="space-y-2 mb-2">
                    {comments.map(c => (
                      <div key={c.id} className="bg-gray-50 rounded-lg px-3 py-2">
                        <div className="text-xs text-gray-400 mb-0.5">{c.author} · {c.created_at}</div>
                        <div className="text-sm text-gray-700 whitespace-pre-wrap">{c.body}</div>
                      </div>
                    ))}
                    {!comments.length && <div className="text-xs text-gray-400">No comments yet.</div>}
                  </div>
                  <div className="flex items-end gap-2">
                    <textarea className="input flex-1 resize-none h-[38px] py-1.5 text-sm" placeholder="Add a comment…"
                      value={commentBody} onChange={e => setCommentBody(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); addComment() } }} />
                    <button onClick={addComment} disabled={!commentBody.trim()} className="btn-primary h-[38px] px-3 disabled:opacity-40"><Send size={14} /></button>
                  </div>
                </div>

                {canApprove ? (
                  <div className="border-t border-gray-100 pt-3">
                    <div className="text-[11px] uppercase tracking-wide text-gray-400 mb-1">Review (this records the decision; it does not auto-apply the change)</div>
                    <textarea className="input h-16 resize-none mb-2" placeholder="Review note (optional)" value={note} onChange={e => setNote(e.target.value)} />
                    <div className="flex gap-2 flex-wrap">
                      <button onClick={() => review('triaged')} className="btn-ghost text-xs">Triage</button>
                      <button onClick={() => review('approved')} className="text-xs px-3 py-1.5 rounded-lg bg-emerald-600 text-white hover:bg-emerald-700">Approve</button>
                      <button onClick={() => review('rejected')} className="text-xs px-3 py-1.5 rounded-lg bg-red-600 text-white hover:bg-red-700">Reject</button>
                      <button onClick={() => review('done')} className="btn-ghost text-xs">Mark done</button>
                    </div>
                  </div>
                ) : (
                  <div className="text-xs text-gray-400 border-t border-gray-100 pt-3">You can view this request. Approving/rejecting needs the “Approve Portal Requests” permission.</div>
                )}
              </div>
            </div>
          </div>
        </div>
      ), document.body)}
    </div>
  )
}

// ── Agent (read-only chat) tab ────────────────────────────────────────────────
function AgentTab() {
  const [status, setStatus] = useState(null)
  const [sessions, setSessions] = useState([])
  const [sessionId, setSessionId] = useState(null)
  const [messages, setMessages] = useState([])   // {role, content, tool_trace[]}
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [playbook, setPlaybook] = useState([])
  const scroller = useRef(null)

  const loadSessions = () =>
    api.get('/portal/agent/sessions').then(({ data }) => setSessions(data.sessions || [])).catch(() => {})

  useEffect(() => {
    api.get('/portal/agent/status').then(({ data }) => setStatus(data)).catch(() => setStatus({ enabled: false }))
    api.get('/portal/agent/playbook').then(({ data }) => setPlaybook(data.groups || [])).catch(() => {})
    loadSessions()
  }, [])

  useEffect(() => {
    if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight
  }, [messages, streaming])

  const openSession = async (id) => {
    setSessionId(id)
    try {
      const { data } = await api.get(`/portal/agent/sessions/${id}`)
      setMessages((data.messages || []).map(m => ({ role: m.role, content: m.content, tool_trace: m.tool_trace || [] })))
    } catch { toast.error('Could not load conversation') }
  }

  const newChat = () => { setSessionId(null); setMessages([]) }

  const deleteSession = async (id, e) => {
    e.stopPropagation()
    if (!confirm('Delete this conversation?')) return
    try {
      await api.delete(`/portal/agent/sessions/${id}`)
      if (id === sessionId) newChat()
      loadSessions()
    } catch { toast.error('Delete failed') }
  }

  const send = async (preset) => {
    const text = (typeof preset === 'string' ? preset : input).trim()
    if (!text || streaming) return
    if (typeof preset !== 'string') setInput('')
    setMessages(m => [...m, { role: 'user', content: text }, { role: 'assistant', content: '', tool_trace: [] }])
    setStreaming(true)

    try {
      const token = localStorage.getItem('token')
      const resp = await fetch('/api/portal/agent/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ message: text, session_id: sessionId }),
      })
      if (!resp.ok || !resp.body) {
        const detail = await resp.json().catch(() => ({}))
        throw new Error(detail.detail || `HTTP ${resp.status}`)
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let curSid = sessionId
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let idx
        while ((idx = buffer.indexOf('\n\n')) >= 0) {
          const chunk = buffer.slice(0, idx); buffer = buffer.slice(idx + 2)
          if (!chunk.startsWith('data: ')) continue
          let ev; try { ev = JSON.parse(chunk.slice(6)) } catch { continue }
          if (ev.type === 'session') { curSid = ev.id; if (!sessionId) setSessionId(ev.id) }
          else if (ev.type === 'text') {
            setMessages(m => { const n = [...m]; n[n.length - 1] = { ...n[n.length - 1], content: n[n.length - 1].content + ev.text }; return n })
          } else if (ev.type === 'tool') {
            setMessages(m => { const n = [...m]; const last = n[n.length - 1]; n[n.length - 1] = { ...last, tool_trace: [...(last.tool_trace || []), ev.summary] }; return n })
          } else if (ev.type === 'error') {
            setMessages(m => { const n = [...m]; n[n.length - 1] = { ...n[n.length - 1], content: (n[n.length - 1].content || '') + `\n\n⚠️ ${ev.error}` }; return n })
          }
        }
      }
      if (curSid && !sessions.find(s => s.id === curSid)) loadSessions()
    } catch (err) {
      setMessages(m => { const n = [...m]; n[n.length - 1] = { ...n[n.length - 1], content: `⚠️ ${err.message}` }; return n })
    } finally {
      setStreaming(false)
    }
  }

  if (status && !status.enabled) {
    return (
      <div className="card p-6 max-w-2xl">
        <div className="flex items-center gap-2 mb-2"><Sparkles size={18} className="text-primary" /><h3 className="font-semibold text-gray-800">Agent not configured</h3></div>
        <p className="text-sm text-gray-600 mb-3">The read-only Developer Portal agent needs an Anthropic API key. Set <code className="px-1 bg-gray-100 rounded">ANTHROPIC_API_KEY</code> in <code className="px-1 bg-gray-100 rounded">backend/.env</code> and restart the backend.</p>
        <div className="text-xs text-gray-400">SDK present: {String(status.has_sdk)} · Key set: {String(status.has_key)}</div>
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-[220px_1fr] gap-4" style={{ height: 'calc(100vh - 230px)' }}>
      {/* Threads */}
      <div className="card p-2 flex flex-col min-h-0">
        <button onClick={newChat} className="btn-primary w-full flex items-center justify-center gap-1.5 text-sm mb-2"><Plus size={14} /> New chat</button>
        <div className="overflow-y-auto min-h-0 space-y-0.5">
          {sessions.map(s => (
            <div key={s.id} onClick={() => openSession(s.id)}
              className={`group flex items-center justify-between gap-1 px-2 py-2 rounded-lg cursor-pointer text-sm ${sessionId === s.id ? 'bg-primary/10 text-primary' : 'text-gray-600 hover:bg-gray-50'}`}>
              <span className="truncate">{s.title || 'Untitled'}</span>
              <button onClick={(e) => deleteSession(s.id, e)} className="opacity-0 group-hover:opacity-100 text-gray-400 hover:text-red-500 shrink-0"><Trash2 size={13} /></button>
            </div>
          ))}
          {!sessions.length && <div className="px-2 py-2 text-xs text-gray-400">No conversations yet.</div>}
        </div>
      </div>

      {/* Conversation */}
      <div className="card p-0 flex flex-col min-h-0 overflow-hidden">
        <div className="px-4 py-2.5 border-b border-gray-100 flex items-center gap-2 text-xs text-gray-500">
          <ShieldCheck size={14} className="text-emerald-500" />
          Read-only assistant · live codebase + database · cannot change anything
          {status?.model && <span className="ml-auto font-mono text-gray-400">{status.model}</span>}
        </div>

        <div ref={scroller} className="flex-1 overflow-y-auto p-4 space-y-4 min-h-0">
          {!messages.length && (
            <div className="py-6">
              <div className="text-center text-gray-400 text-sm mb-5">
                <Sparkles size={28} className="mx-auto mb-3 text-primary/40" />
                Ask about the codebase, the schema, or the live data — or start with a playbook prompt.
              </div>
              <div className="max-w-3xl mx-auto space-y-4">
                {playbook.map(g => (
                  <div key={g.group}>
                    <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-gray-400 font-semibold mb-2">
                      <Lightbulb size={12} /> {g.group}
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {g.items.map(it => (
                        <button key={it.label} onClick={() => send(it.prompt)} title={it.prompt}
                          className="text-left text-xs px-3 py-2 rounded-xl border border-gray-200 bg-white hover:border-primary/50 hover:bg-primary/5 text-gray-700 transition-colors max-w-xs">
                          {it.label}
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={m.role === 'user' ? 'flex justify-end' : ''}>
              {m.role === 'user' ? (
                <div className="bg-primary text-white rounded-2xl rounded-br-sm px-3.5 py-2 text-sm max-w-[80%] whitespace-pre-wrap">{m.content}</div>
              ) : (
                <div className="max-w-[88%]">
                  {!!(m.tool_trace || []).length && (
                    <div className="mb-1.5 space-y-1">
                      {m.tool_trace.map((t, j) => (
                        <div key={j} className="inline-flex items-center gap-1.5 text-[11px] text-gray-500 bg-gray-50 border border-gray-100 rounded-full px-2 py-0.5 mr-1 font-mono">
                          <Wrench size={10} /> {t}
                        </div>
                      ))}
                    </div>
                  )}
                  {m.content
                    ? <div className="bg-gray-50 rounded-2xl rounded-bl-sm px-3.5 py-2"><Markdown content={m.content} /></div>
                    : (streaming && i === messages.length - 1 && <div className="text-gray-400 text-sm flex items-center gap-2"><RefreshCw size={13} className="animate-spin" /> thinking…</div>)}
                </div>
              )}
            </div>
          ))}
        </div>

        <div className="border-t border-gray-100 p-3">
          <div className="flex items-end gap-2">
            <textarea
              className="input flex-1 resize-none h-[42px] max-h-32 py-2"
              placeholder="Ask the agent… (Enter to send, Shift+Enter for newline)"
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
              disabled={streaming}
            />
            <button onClick={send} disabled={streaming || !input.trim()} className="btn-primary h-[42px] px-3 flex items-center gap-1.5 disabled:opacity-40">
              {streaming ? <RefreshCw size={15} className="animate-spin" /> : <Send size={15} />}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Scheduled agent tab ───────────────────────────────────────────────────────
const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
function ScheduledTab() {
  const canManage = isAdmin() || hasPermission('portal_approve')
  const [jobs, setJobs] = useState([])
  const [showCreate, setShowCreate] = useState(false)
  const [form, setForm] = useState({ name: '', prompt: '', frequency: 'daily', hour: 8, minute: 0, day_of_week: 0, is_enabled: true })
  const [runsFor, setRunsFor] = useState(null)   // job obj
  const [runs, setRuns] = useState([])

  const load = () => api.get('/portal/agent/jobs').then(({ data }) => setJobs(data.jobs || [])).catch(() => {})
  useEffect(() => { load() }, [])

  const create = async () => {
    if (!form.name.trim() || !form.prompt.trim()) { toast.error('Name and prompt required'); return }
    try { await api.post('/portal/agent/jobs', form); toast.success('Scheduled job created'); setShowCreate(false); setForm({ name: '', prompt: '', frequency: 'daily', hour: 8, minute: 0, day_of_week: 0, is_enabled: true }); load() }
    catch (err) { toast.error(err.response?.data?.detail || 'Failed') }
  }
  const toggle = async (j) => { try { await api.patch(`/portal/agent/jobs/${j.id}`, { is_enabled: !j.is_enabled }); load() } catch { toast.error('Failed') } }
  const runNow = async (j) => { try { await api.post(`/portal/agent/jobs/${j.id}/run-now`); toast.success('Run started — check runs in a moment') } catch (err) { toast.error(err.response?.data?.detail || 'Failed') } }
  const remove = async (j) => { if (!confirm(`Delete job “${j.name}”?`)) return; try { await api.delete(`/portal/agent/jobs/${j.id}`); load() } catch { toast.error('Failed') } }
  const openRuns = async (j) => { setRunsFor(j); try { const { data } = await api.get(`/portal/agent/jobs/${j.id}/runs`); setRuns(data.runs || []) } catch { setRuns([]) } }

  const sched = (j) => j.frequency === 'weekly'
    ? `Weekly · ${DOW[j.day_of_week ?? 0]} ${String(j.hour).padStart(2, '0')}:${String(j.minute).padStart(2, '0')} IST`
    : `Daily · ${String(j.hour).padStart(2, '0')}:${String(j.minute).padStart(2, '0')} IST`

  return (
    <div>
      <div className="flex items-center justify-between mb-3 gap-3 flex-wrap">
        <p className="text-sm text-gray-500">Recurring autonomous agent runs. The agent runs your prompt on schedule and may file requests into the queue — which still need human approval.</p>
        {canManage && <button onClick={() => setShowCreate(true)} className="btn-primary flex items-center gap-1.5 text-sm shrink-0"><Plus size={14} /> New job</button>}
      </div>

      <div className="space-y-2">
        {jobs.map(j => (
          <div key={j.id} className="card p-4 flex items-start justify-between gap-3 flex-wrap">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <CalendarClock size={15} className="text-primary shrink-0" />
                <span className="font-medium text-gray-800">{j.name}</span>
                <span className={j.is_enabled ? 'badge-green' : 'badge-gray'}>{j.is_enabled ? 'enabled' : 'paused'}</span>
                {j.last_status && <span className={j.last_status === 'ok' ? 'badge-green' : 'badge-red'}>last: {j.last_status}</span>}
              </div>
              <div className="text-xs text-gray-500 mt-1">{sched(j)}{j.last_run_at ? ` · last run ${j.last_run_at}` : ' · never run'}</div>
              <div className="text-xs text-gray-400 mt-1 line-clamp-2 max-w-2xl">{j.prompt}</div>
            </div>
            <div className="flex items-center gap-1.5 shrink-0">
              <button onClick={() => openRuns(j)} className="btn-ghost text-xs">Runs</button>
              {canManage && <>
                <button onClick={() => runNow(j)} title="Run now" className="text-gray-400 hover:text-primary p-1.5"><Play size={15} /></button>
                <button onClick={() => toggle(j)} title={j.is_enabled ? 'Pause' : 'Enable'} className={`p-1.5 ${j.is_enabled ? 'text-emerald-500' : 'text-gray-300'} hover:opacity-80`}><Power size={15} /></button>
                <button onClick={() => remove(j)} title="Delete" className="text-gray-400 hover:text-red-500 p-1.5"><Trash2 size={14} /></button>
              </>}
            </div>
          </div>
        ))}
        {!jobs.length && <div className="card p-6 text-center text-gray-400 text-sm">No scheduled jobs yet.</div>}
      </div>

      {/* Create modal */}
      {showCreate && createPortal((
        <div className="fixed inset-0 bg-black/50 z-50 overflow-y-auto" onClick={() => setShowCreate(false)}>
          <div className="min-h-full flex items-center justify-center p-4">
            <div className="bg-white rounded-xl shadow-2xl w-full max-w-lg" onClick={e => e.stopPropagation()}>
              <div className="border-b border-gray-100 px-5 py-3 flex items-center justify-between">
                <h3 className="font-semibold text-gray-800">New scheduled job</h3>
                <button onClick={() => setShowCreate(false)} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
              </div>
              <div className="px-5 py-4 space-y-3">
                <div><label className="text-xs font-medium text-gray-600 block mb-1">Name</label>
                  <input className="input" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="Nightly health digest" /></div>
                <div><label className="text-xs font-medium text-gray-600 block mb-1">Prompt (what the agent should do each run)</label>
                  <textarea className="input h-24 resize-none" value={form.prompt} onChange={e => setForm({ ...form, prompt: e.target.value })} placeholder="Check ingestion health and the last 24h match rate. If anything looks wrong, file a request." /></div>
                <div className="grid grid-cols-2 gap-3">
                  <div><label className="text-xs font-medium text-gray-600 block mb-1">Frequency</label>
                    <select className="select" value={form.frequency} onChange={e => setForm({ ...form, frequency: e.target.value })}>
                      <option value="daily">Daily</option><option value="weekly">Weekly</option>
                    </select></div>
                  {form.frequency === 'weekly' && (
                    <div><label className="text-xs font-medium text-gray-600 block mb-1">Day</label>
                      <select className="select" value={form.day_of_week} onChange={e => setForm({ ...form, day_of_week: Number(e.target.value) })}>
                        {DOW.map((d, i) => <option key={i} value={i}>{d}</option>)}
                      </select></div>
                  )}
                  <div><label className="text-xs font-medium text-gray-600 block mb-1">Hour (IST)</label>
                    <input type="number" min={0} max={23} className="input" value={form.hour} onChange={e => setForm({ ...form, hour: Number(e.target.value) })} /></div>
                  <div><label className="text-xs font-medium text-gray-600 block mb-1">Minute</label>
                    <input type="number" min={0} max={59} className="input" value={form.minute} onChange={e => setForm({ ...form, minute: Number(e.target.value) })} /></div>
                </div>
              </div>
              <div className="border-t border-gray-100 bg-gray-50/50 px-5 py-3 flex gap-2">
                <button onClick={() => setShowCreate(false)} className="btn-ghost flex-1">Cancel</button>
                <button onClick={create} className="btn-primary flex-1">Create job</button>
              </div>
            </div>
          </div>
        </div>
      ), document.body)}

      {/* Runs modal */}
      {runsFor && createPortal((
        <div className="fixed inset-0 bg-black/50 z-50 overflow-y-auto" onClick={() => setRunsFor(null)}>
          <div className="min-h-full flex items-center justify-center p-4">
            <div className="bg-white rounded-xl shadow-2xl w-full max-w-2xl my-4" onClick={e => e.stopPropagation()}>
              <div className="border-b border-gray-100 px-5 py-3 flex items-center justify-between">
                <h3 className="font-semibold text-gray-800">Runs · {runsFor.name}</h3>
                <button onClick={() => setRunsFor(null)} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
              </div>
              <div className="px-5 py-4 space-y-3 max-h-[70vh] overflow-y-auto">
                {!runs.length && <div className="text-sm text-gray-400">No runs yet.</div>}
                {runs.map(r => (
                  <div key={r.id} className="border border-gray-100 rounded-lg p-3">
                    <div className="flex items-center gap-2 text-xs mb-1">
                      <span className={r.status === 'ok' ? 'badge-green' : r.status === 'error' ? 'badge-red' : 'badge-yellow'}>{r.status}</span>
                      <span className="badge-gray">{r.trigger}</span>
                      {r.requests_filed > 0 && <span className="inline-flex items-center gap-1 text-primary"><Inbox size={11} /> {r.requests_filed} filed</span>}
                      <span className="text-gray-400 ml-auto">{r.started_at}</span>
                    </div>
                    {r.error && <div className="text-xs text-red-600 mb-1">{r.error}</div>}
                    {!!(r.tools_used || []).length && <div className="text-[11px] text-gray-400 font-mono mb-1">{r.tools_used.join(' · ')}</div>}
                    {r.summary && <div className="text-sm text-gray-600"><Markdown content={r.summary} /></div>}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      ), document.body)}
    </div>
  )
}

// ── Builder (write-capable) agent tab ─────────────────────────────────────────
const BUILDER_STATUS_STYLE = {
  planning: 'badge-gray', building: 'badge-blue', gating: 'badge-blue',
  awaiting_input: 'badge-yellow', ready: 'badge-green', applied: 'badge-green',
  failed: 'badge-red', rejected: 'badge-red', rolled_back: 'badge-gray',
}
const GATE_LABEL = {
  compileall: 'Compile', pytest: 'Tests', ruff: 'Lint',
  frontend_build: 'Frontend build', behavior_contract: 'Behavior contract',
}

function GateResults({ gates }) {
  if (!gates) return null
  const entries = Object.entries(gates)
  return (
    <div className="space-y-1">
      {entries.map(([k, v]) => (
        <div key={k} className="flex items-start gap-2 text-xs">
          {v.skipped
            ? <span className="badge-gray shrink-0 mt-0.5">skip</span>
            : v.ok
              ? <CheckCircle2 size={14} className="text-emerald-500 shrink-0 mt-0.5" />
              : <XCircle size={14} className="text-red-500 shrink-0 mt-0.5" />}
          <div className="min-w-0">
            <span className="font-medium text-gray-700">{GATE_LABEL[k] || k}</span>
            {v.detail && <span className="text-gray-400"> — {String(v.detail).split('\n').slice(-1)[0].slice(0, 120)}</span>}
          </div>
        </div>
      ))}
    </div>
  )
}

function BuilderTab() {
  const [status, setStatus] = useState(null)
  const [tasks, setTasks] = useState([])
  const [taskId, setTaskId] = useState(null)
  const [task, setTask] = useState(null)          // current task detail (gates, files, status)
  const [messages, setMessages] = useState([])    // {role, content, plan, tools[], gates, awaitQ, finish}
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [diff, setDiff] = useState(null)          // {files:[...]} for modal
  const [busy, setBusy] = useState(false)
  const [playbook, setPlaybook] = useState([])
  const [planOnly, setPlanOnly] = useState(false)
  const [deployAvail, setDeployAvail] = useState(false)
  const scroller = useRef(null)

  const loadTasks = () => api.get('/portal/builder/tasks').then(({ data }) => setTasks(data.tasks || [])).catch(() => {})
  const loadTask = (id) => api.get(`/portal/builder/tasks/${id}`).then(({ data }) => setTask(data)).catch(() => {})

  useEffect(() => {
    api.get('/portal/builder/status').then(({ data }) => setStatus(data)).catch(() => setStatus({ enabled: false, master_switch: false }))
    api.get('/portal/builder/playbook').then(({ data }) => setPlaybook(data.groups || [])).catch(() => {})
    api.get('/portal/builder/deploy/status').then(({ data }) => setDeployAvail(!!data.available)).catch(() => {})
    loadTasks()
  }, [])
  useEffect(() => { if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight }, [messages, streaming])

  const openTask = async (id) => {
    setTaskId(id)
    try {
      const { data } = await api.get(`/portal/builder/tasks/${id}`)
      setTask(data)
      setMessages((data.messages || []).map(m => ({ role: m.role, content: m.content, tools: m.tool_trace || [] })))
    } catch { toast.error('Could not load task') }
  }
  const newTask = () => { setTaskId(null); setTask(null); setMessages([]) }

  const send = async (preset) => {
    const text = (typeof preset === 'string' ? preset : input).trim()
    if (!text || streaming) return
    if (typeof preset !== 'string') setInput('')
    setMessages(m => [...m, { role: 'user', content: text }, { role: 'assistant', content: '', tools: [] }])
    setStreaming(true)
    let curTid = taskId
    try {
      const token = localStorage.getItem('token')
      const resp = await fetch('/api/portal/builder/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ message: text, task_id: taskId, plan_only: planOnly }),
      })
      if (!resp.ok || !resp.body) {
        const d = await resp.json().catch(() => ({})); throw new Error(d.detail || `HTTP ${resp.status}`)
      }
      const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf = ''
      const patch = (fn) => setMessages(m => { const n = [...m]; n[n.length - 1] = fn(n[n.length - 1]); return n })
      while (true) {
        const { done, value } = await reader.read(); if (done) break
        buf += dec.decode(value, { stream: true }); let i
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, i); buf = buf.slice(i + 2)
          if (!chunk.startsWith('data: ')) continue
          let ev; try { ev = JSON.parse(chunk.slice(6)) } catch { continue }
          if (ev.type === 'task') { curTid = ev.id; if (!taskId) setTaskId(ev.id) }
          else if (ev.type === 'text') patch(a => ({ ...a, content: a.content + ev.text }))
          else if (ev.type === 'plan') patch(a => ({ ...a, plan: ev.plan }))
          else if (ev.type === 'tool') patch(a => ({ ...a, tools: [...(a.tools || []), ev.summary] }))
          else if (ev.type === 'gates') patch(a => ({ ...a, gates: ev.results }))
          else if (ev.type === 'await') patch(a => ({ ...a, awaitQ: ev.question }))
          else if (ev.type === 'finish') patch(a => ({ ...a, finish: ev }))
          else if (ev.type === 'escalation') patch(a => ({ ...a, escalation: ev }))
          else if (ev.type === 'applied') patch(a => ({ ...a, applied: ev.result }))
          else if (ev.type === 'error') patch(a => ({ ...a, content: (a.content || '') + `\n\n⚠️ ${ev.error}` }))
        }
      }
    } catch (err) {
      setMessages(m => { const n = [...m]; n[n.length - 1] = { ...n[n.length - 1], content: `⚠️ ${err.message}` }; return n })
    } finally {
      setStreaming(false)
      loadTasks(); if (curTid) loadTask(curTid)
    }
  }

  const doApply = async () => {
    if (!task) return; setBusy(true)
    try {
      const { data } = await api.post(`/portal/builder/tasks/${task.id}/apply`)
      toast.success(data.note || 'Applied'); await loadTask(task.id); loadTasks()
      if (data.restart_needed) toast('Backend restart needed to load the change', { icon: '🔁' })
    } catch (err) { toast.error(err.response?.data?.detail || 'Apply failed') }
    finally { setBusy(false) }
  }
  const doRollback = async () => {
    if (!task || !confirm('Roll this task back to its exact pre-change state?')) return; setBusy(true)
    try {
      const { data } = await api.post(`/portal/builder/tasks/${task.id}/rollback`)
      toast.success('Rolled back'); await loadTask(task.id); loadTasks()
      if (data.restart_needed) toast('Backend restart needed', { icon: '🔁' })
    } catch (err) { toast.error(err.response?.data?.detail || 'Rollback failed') }
    finally { setBusy(false) }
  }
  const doRestart = async () => {
    if (!confirm('Restart the backend now to load applied changes?')) return
    try { const { data } = await api.post('/portal/builder/restart'); toast(data.detail || 'Restarting…', { icon: '🔁' }) }
    catch (err) { toast.error(err.response?.data?.detail || 'Restart failed') }
  }
  const doDeploy = async () => {
    if (!confirm('Run the configured remote deploy (build + ship to the server)?')) return
    setBusy(true)
    try { const { data } = await api.post('/portal/builder/deploy'); data.ok ? toast.success('Deploy finished') : toast.error(data.detail?.slice(0, 120) || 'Deploy failed') }
    catch (err) { toast.error(err.response?.data?.detail || 'Deploy failed') }
    finally { setBusy(false) }
  }
  const openDiff = async () => {
    if (!task) return
    try { const { data } = await api.get(`/portal/builder/tasks/${task.id}/diff`); setDiff(data) }
    catch { toast.error('Could not load diff') }
  }
  const deleteTask = async (id, e) => {
    e.stopPropagation()
    if (!confirm('Delete this build task?')) return
    try { await api.delete(`/portal/builder/tasks/${id}`); if (id === taskId) newTask(); loadTasks() }
    catch (err) { toast.error(err.response?.data?.detail || 'Delete failed') }
  }

  // Not configured / switched off states
  if (status && !status.master_switch) {
    return (
      <div className="card p-6 max-w-2xl">
        <div className="flex items-center gap-2 mb-2"><ShieldAlert size={18} className="text-amber-500" /><h3 className="font-semibold text-gray-800">Builder Agent is switched OFF</h3></div>
        <p className="text-sm text-gray-600 mb-3">The write-capable Builder Agent is behind a master kill-switch and ships disabled. To enable it, set <code className="px-1 bg-gray-100 rounded">BUILDER_AGENT_ENABLED=1</code> in <code className="px-1 bg-gray-100 rounded">backend/.env</code> and restart the backend.</p>
        <p className="text-xs text-gray-500">When on, the agent can create/edit/delete application code (everything except secrets) and — once a change passes every gate (compile, tests, build, lint, behavior-contract) — apply it. Set <code className="px-1 bg-gray-100 rounded">BUILDER_AUTO_APPLY=1</code> to skip the human Apply click.</p>
      </div>
    )
  }
  if (status && !status.has_key) {
    return <div className="card p-6 max-w-2xl text-sm text-gray-600">The Builder Agent needs an Anthropic API key. Set <code className="px-1 bg-gray-100 rounded">ANTHROPIC_API_KEY</code> in <code className="px-1 bg-gray-100 rounded">backend/.env</code> and restart.</div>
  }

  const canApply = task && task.gates_ok && ['ready', 'failed'].includes(task.status) && task.status !== 'applied'
  const canRollback = task && (task.files_changed || []).length > 0 && task.status !== 'rolled_back'

  return (
    <div>
      <div className="card p-3 mb-3 border-amber-200 bg-amber-50/60 flex items-start gap-2">
        <AlertTriangle size={15} className="text-amber-500 mt-0.5 shrink-0" />
        <div className="text-xs text-amber-800">
          <span className="font-semibold">Write-capable agent.</span> It edits real application code. Every change must pass all gates before it can go live, every change is reversible, and secrets/account data are walled off.
          {status?.auto_apply && <span className="font-semibold"> Auto-apply is ON — vetted changes go live without a click.</span>}
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-[220px_1fr] gap-4" style={{ height: 'calc(100vh - 290px)' }}>
        {/* Task list */}
        <div className="card p-2 flex flex-col min-h-0">
          <button onClick={newTask} className="btn-primary w-full flex items-center justify-center gap-1.5 text-sm mb-2"><Plus size={14} /> New task</button>
          <div className="overflow-y-auto min-h-0 space-y-0.5">
            {tasks.map(t => (
              <div key={t.id} onClick={() => openTask(t.id)}
                className={`group px-2 py-2 rounded-lg cursor-pointer text-sm ${taskId === t.id ? 'bg-primary/10' : 'hover:bg-gray-50'}`}>
                <div className="flex items-center justify-between gap-1">
                  <span className="truncate text-gray-700">{t.title || 'Untitled'}</span>
                  <button onClick={(e) => deleteTask(t.id, e)} className="opacity-0 group-hover:opacity-100 text-gray-400 hover:text-red-500 shrink-0"><Trash2 size={13} /></button>
                </div>
                <span className={`${BUILDER_STATUS_STYLE[t.status] || 'badge-gray'} mt-0.5 inline-block`}>{t.status}</span>
              </div>
            ))}
            {!tasks.length && <div className="px-2 py-2 text-xs text-gray-400">No build tasks yet.</div>}
          </div>
        </div>

        {/* Main */}
        <div className="card p-0 flex flex-col min-h-0 overflow-hidden">
          <div className="px-4 py-2.5 border-b border-gray-100 flex items-center gap-2 text-xs text-gray-500">
            <Hammer size={14} className="text-primary" />
            Autonomous builder · edits code · gated + reversible
            {status?.model && <span className="ml-auto font-mono text-gray-400">{status.model}</span>}
          </div>

          {/* Task status / actions panel */}
          {task && (
            <div className="px-4 py-3 border-b border-gray-100 bg-gray-50/50">
              <div className="flex items-center gap-2 flex-wrap mb-2">
                <span className={BUILDER_STATUS_STYLE[task.status] || 'badge-gray'}>{task.status}</span>
                {task.gates_ok && <span className="badge-green">gates passed</span>}
                {(task.files_changed || []).length > 0 && <span className="text-xs text-gray-500">{task.files_changed.length} file{task.files_changed.length === 1 ? '' : 's'} changed</span>}
                {task.commit_sha && <span className="font-mono text-xs text-gray-400">{task.commit_sha}</span>}
                <div className="ml-auto flex items-center gap-1.5">
                  {(task.files_changed || []).length > 0 && <button onClick={openDiff} className="btn-ghost text-xs flex items-center gap-1"><GitCompare size={12} /> Diff</button>}
                  {canApply && <button onClick={doApply} disabled={busy} className="text-xs px-2.5 py-1.5 rounded-lg bg-emerald-600 text-white hover:bg-emerald-700 flex items-center gap-1 disabled:opacity-50"><Rocket size={12} /> Apply</button>}
                  {canRollback && <button onClick={doRollback} disabled={busy} className="text-xs px-2.5 py-1.5 rounded-lg border border-gray-200 hover:bg-gray-100 flex items-center gap-1 disabled:opacity-50"><RotateCcw size={12} /> Roll back</button>}
                  {task.status === 'applied' && <button onClick={doRestart} className="btn-ghost text-xs flex items-center gap-1"><Power size={12} /> Restart</button>}
                  {task.status === 'applied' && deployAvail && <button onClick={doDeploy} disabled={busy} className="text-xs px-2.5 py-1.5 rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 flex items-center gap-1 disabled:opacity-50"><Rocket size={12} /> Deploy</button>}
                </div>
              </div>
              {(task.files_changed || []).length > 0 && (
                <div className="flex flex-wrap gap-1 mb-2">
                  {task.files_changed.map((f, j) => (
                    <span key={j} className="font-mono text-[10px] bg-white border border-gray-200 rounded px-1.5 py-0.5 text-gray-600">{typeof f === 'string' ? f : f.path}</span>
                  ))}
                </div>
              )}
              {task.gate_results && <GateResults gates={task.gate_results} />}
            </div>
          )}

          {/* Conversation */}
          <div ref={scroller} className="flex-1 overflow-y-auto p-4 space-y-4 min-h-0">
            {!messages.length && (
              <div className="py-6">
                <div className="text-center text-gray-400 text-sm mb-5">
                  <Hammer size={28} className="mx-auto mb-3 text-primary/40" />
                  Describe a change, fix, or new feature. The agent asks questions if it needs to, writes the code, runs the gates, and shows you the diff.
                </div>
                <div className="max-w-3xl mx-auto space-y-4">
                  {playbook.map(g => (
                    <div key={g.group}>
                      <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-gray-400 font-semibold mb-2">
                        <Lightbulb size={12} /> {g.group}
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {g.items.map(it => (
                          <button key={it.label} onClick={() => send(it.prompt)} title={it.prompt}
                            className="text-left text-xs px-3 py-2 rounded-xl border border-gray-200 bg-white hover:border-primary/50 hover:bg-primary/5 text-gray-700 transition-colors max-w-xs">
                            {it.label}
                          </button>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} className={m.role === 'user' ? 'flex justify-end' : ''}>
                {m.role === 'user' ? (
                  <div className="bg-primary text-white rounded-2xl rounded-br-sm px-3.5 py-2 text-sm max-w-[80%] whitespace-pre-wrap">{m.content}</div>
                ) : (
                  <div className="max-w-[90%] space-y-2">
                    {m.plan && (
                      <div className="text-xs bg-blue-50 border border-blue-100 rounded-lg px-3 py-2">
                        <div className="font-semibold text-blue-700 mb-0.5">Plan</div>
                        <div className="text-blue-900 whitespace-pre-wrap">{m.plan}</div>
                      </div>
                    )}
                    {!!(m.tools || []).length && (
                      <div className="space-y-1">
                        {m.tools.map((t, j) => (
                          <div key={j} className="inline-flex items-center gap-1.5 text-[11px] text-gray-500 bg-gray-50 border border-gray-100 rounded-full px-2 py-0.5 mr-1 font-mono">
                            <Wrench size={10} /> {t}
                          </div>
                        ))}
                      </div>
                    )}
                    {m.gates && (
                      <div className={`rounded-lg border px-3 py-2 ${m.gates.ok ? 'border-emerald-200 bg-emerald-50/50' : 'border-red-200 bg-red-50/50'}`}>
                        <div className="text-xs font-semibold mb-1 flex items-center gap-1.5">
                          {m.gates.ok ? <CheckCircle2 size={13} className="text-emerald-500" /> : <XCircle size={13} className="text-red-500" />}
                          Gates {m.gates.ok ? 'passed' : 'failed'}
                        </div>
                        <GateResults gates={m.gates.gates} />
                      </div>
                    )}
                    {m.awaitQ && (
                      <div className="text-sm bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 text-amber-900">
                        <span className="font-semibold">Question: </span>{m.awaitQ}
                      </div>
                    )}
                    {m.content
                      ? <div className="bg-gray-50 rounded-2xl rounded-bl-sm px-3.5 py-2"><Markdown content={m.content} /></div>
                      : (streaming && i === messages.length - 1 && !m.plan && !(m.tools || []).length && <div className="text-gray-400 text-sm flex items-center gap-2"><RefreshCw size={13} className="animate-spin" /> working…</div>)}
                    {m.finish && (
                      <div className="text-xs text-gray-500 flex items-center gap-1.5">
                        {m.finish.gates_passed ? <CheckCircle2 size={13} className="text-emerald-500" /> : <AlertTriangle size={13} className="text-amber-500" />}
                        {m.finish.gates_passed ? 'Build complete — ready to apply.' : 'Finished, but gates did not pass.'}
                      </div>
                    )}
                    {m.escalation && (
                      <div className="text-xs bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 text-amber-900 flex items-start gap-1.5">
                        <ShieldAlert size={13} className="shrink-0 mt-0.5" /> {m.escalation.message} <span className="font-mono">({(m.escalation.files || []).join(', ')})</span>
                      </div>
                    )}
                    {m.applied && (
                      <div className="text-xs bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2 text-emerald-900 flex items-center gap-1.5">
                        <Rocket size={13} /> Auto-applied{m.applied.commit_sha ? ` (${m.applied.commit_sha})` : ''}. Health watchdog armed{m.applied.restart_needed ? ' · backend restart needed' : ''}.
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>

          {/* Input */}
          <div className="border-t border-gray-100 p-3">
            <div className="flex items-end gap-2">
              <textarea className="input flex-1 resize-none h-[42px] max-h-32 py-2"
                placeholder={planOnly ? 'Describe the change — the agent will only PLAN it (no edits)…' : 'Describe the change… (Enter to send, Shift+Enter for newline)'}
                value={input} onChange={e => setInput(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
                disabled={streaming} />
              <button onClick={send} disabled={streaming || !input.trim()} className="btn-primary h-[42px] px-3 flex items-center gap-1.5 disabled:opacity-40">
                {streaming ? <RefreshCw size={15} className="animate-spin" /> : <Send size={15} />}
              </button>
            </div>
            <label className="flex items-center gap-1.5 text-xs text-gray-500 mt-2 cursor-pointer select-none">
              <input type="checkbox" checked={planOnly} onChange={e => setPlanOnly(e.target.checked)} className="rounded" />
              Plan only (dry run) — the agent produces a plan and diff-free proposal without editing any files
            </label>
          </div>
        </div>
      </div>

      {/* Diff modal */}
      {diff && createPortal((
        <div className="fixed inset-0 bg-black/50 z-50 overflow-y-auto" onClick={() => setDiff(null)}>
          <div className="min-h-full flex items-center justify-center p-4">
            <div className="bg-white rounded-xl shadow-2xl w-full max-w-4xl my-4" onClick={e => e.stopPropagation()}>
              <div className="border-b border-gray-100 px-5 py-3 flex items-center justify-between">
                <h3 className="font-semibold text-gray-800 flex items-center gap-2"><GitCompare size={16} /> Changes</h3>
                <button onClick={() => setDiff(null)} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
              </div>
              <div className="px-5 py-4 space-y-4 max-h-[75vh] overflow-y-auto">
                {!diff.files?.length && <div className="text-sm text-gray-400">No changes.</div>}
                {(diff.files || []).map((f, j) => (
                  <div key={j}>
                    <div className="flex items-center gap-2 mb-1">
                      <span className="font-mono text-xs font-medium text-gray-800">{f.path}</span>
                      <span className="badge-gray">{f.action}</span>
                    </div>
                    <pre className="bg-gray-900 text-gray-100 rounded-lg p-3 text-[11px] overflow-x-auto leading-relaxed">
                      {(f.diff || '').split('\n').map((ln, k) => (
                        <div key={k} className={ln.startsWith('+') && !ln.startsWith('+++') ? 'text-emerald-300' : ln.startsWith('-') && !ln.startsWith('---') ? 'text-red-300' : ln.startsWith('@@') ? 'text-cyan-300' : 'text-gray-400'}>{ln || ' '}</div>
                      ))}
                    </pre>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      ), document.body)}
    </div>
  )
}

// ── "What's New" / changelog tab ──────────────────────────────────────────────
const FLAG_LABEL = {
  'behavior-contract': 'behavior contract', 'data-model': 'data model',
  ingestion: 'ingestion', matching: 'matching',
}
function relTime(iso) {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (isNaN(then)) return iso
  const s = Math.floor((Date.now() - then) / 1000)
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60); if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60); if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24); if (d < 30) return `${d}d ago`
  return new Date(iso).toLocaleDateString()
}
const SEEN_KEY = 'portal_changelog_seen'

function ChangelogTab() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const seenRef = useRef(localStorage.getItem(SEEN_KEY) || '')

  const load = () => {
    setLoading(true)
    api.get('/portal/changelog').then(({ data }) => {
      setData(data)
      const newest = data.commits?.[0]?.date
      if (newest) localStorage.setItem(SEEN_KEY, newest)   // mark seen for next visit
    }).catch(err => toast.error(errMsg(err))).finally(() => setLoading(false))
  }
  useEffect(() => { load() }, [])

  if (loading || !data) return <div className="text-gray-400 text-sm flex items-center gap-2"><RefreshCw size={14} className="animate-spin" /> Loading commit history…</div>
  if (!data.available) return <div className="card p-6 text-sm text-gray-500">{data.reason || 'Commit history is not available on this deployment.'}</div>

  const seen = seenRef.current
  const isNew = (c) => seen && c.date > seen

  return (
    <div>
      <div className="flex items-center justify-between mb-3 gap-3 flex-wrap">
        <p className="text-sm text-gray-500">
          The live code on this deployment — newest first. This is the portal answering
          “does it self-update?”: it reads <span className="font-mono text-xs">git log</span> on the running checkout.
        </p>
        <button onClick={load} className="btn-ghost flex items-center gap-1.5 text-xs"><RefreshCw size={13} /> Refresh</button>
      </div>

      {data.flagged_count > 0 && (
        <div className="card p-3 mb-3 border-amber-200 bg-amber-50/60 flex items-start gap-2">
          <AlertTriangle size={15} className="text-amber-500 mt-0.5 shrink-0" />
          <div className="text-xs text-amber-800">
            <span className="font-semibold">{data.flagged_count}</span> of the last {data.count} commits touched
            load-bearing code (behavior contract, data model, ingestion, or matching). Re-read the behavior
            contract before trusting your mental model of those areas.
          </div>
        </div>
      )}

      <div className="card p-0 overflow-hidden">
        {data.commits.map((c, i) => (
          <div key={c.sha} className={`flex items-start gap-3 px-4 py-3 ${i ? 'border-t border-gray-50' : ''} ${isNew(c) ? 'bg-primary/5' : ''}`}>
            <GitCommit size={15} className="text-gray-400 mt-0.5 shrink-0" />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm text-gray-800 font-medium">{c.subject}</span>
                {isNew(c) && <span className="badge-blue">new</span>}
                {c.flags.map(f => (
                  <span key={f} className="inline-flex items-center gap-1 text-[10px] font-semibold px-1.5 py-0.5 rounded border border-amber-200 bg-amber-50 text-amber-700">
                    <AlertTriangle size={9} /> {FLAG_LABEL[f] || f}
                  </span>
                ))}
              </div>
              <div className="text-xs text-gray-400 mt-0.5 flex items-center gap-2 flex-wrap">
                <span className="font-mono">{c.short_sha}</span>
                <span>· {c.author}</span>
                <span className="flex items-center gap-1"><Clock size={10} /> {relTime(c.date)}</span>
                <span>· {c.files_changed} file{c.files_changed === 1 ? '' : 's'}</span>
              </div>
            </div>
          </div>
        ))}
        {!data.commits.length && <div className="px-4 py-6 text-center text-gray-400 text-sm">No commits.</div>}
      </div>
    </div>
  )
}

// ── Data Explorer tab (read-only SQL) ─────────────────────────────────────────
const SQL_SAMPLES = [
  'SELECT partner, recon_status, COUNT(*) FROM transactions GROUP BY partner, recon_status ORDER BY 3 DESC',
  "SELECT recon_date, COUNT(*) FROM transactions WHERE recon_status='amount_mismatch' GROUP BY recon_date ORDER BY 1 DESC",
  'SELECT name FROM sqlite_master WHERE type=\'table\'',
]
function DataExplorerTab() {
  const [q, setQ] = useState('')
  const [res, setRes] = useState(null)
  const [err, setErr] = useState(null)
  const [running, setRunning] = useState(false)

  const run = async () => {
    if (!q.trim()) return
    setRunning(true); setErr(null); setRes(null)
    try { const { data } = await api.post('/portal/sql', { query: q }); setRes(data) }
    catch (e) { setErr(e.response?.data?.detail || 'Query failed') }
    finally { setRunning(false) }
  }

  return (
    <div>
      <p className="text-sm text-gray-500 mb-3">Run a read-only <span className="font-mono text-xs">SELECT</span> against the live database. Same guard rails as the agent: SELECT-only, single statement, 500-row cap, <span className="font-mono text-xs">users</span>/<span className="font-mono text-xs">api_keys</span> blocked, read-only transaction. Every query is audited.</p>
      <textarea className="input font-mono text-sm h-28 resize-none w-full" placeholder="SELECT …" value={q}
        onChange={e => setQ(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run() } }} />
      <div className="flex items-center gap-2 mt-2 flex-wrap">
        <button onClick={run} disabled={running || !q.trim()} className="btn-primary text-sm flex items-center gap-1.5 disabled:opacity-40">
          {running ? <RefreshCw size={14} className="animate-spin" /> : <Terminal size={14} />} Run <span className="text-[10px] opacity-70">Ctrl+↵</span>
        </button>
        {SQL_SAMPLES.map((s, i) => (
          <button key={i} onClick={() => setQ(s)} className="text-[11px] text-gray-500 bg-gray-50 border border-gray-200 rounded px-2 py-1 hover:bg-gray-100 font-mono truncate max-w-xs">{s.slice(0, 42)}…</button>
        ))}
      </div>
      {err && <div className="mt-3 text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{err}</div>}
      {res && (
        <div className="mt-3">
          <div className="text-xs text-gray-500 mb-1">{res.row_count} row{res.row_count === 1 ? '' : 's'}{res.truncated ? ' (capped at 500)' : ''}</div>
          <div className="card p-0 overflow-auto max-h-[55vh]">
            <table className="w-full text-xs">
              <thead className="bg-gray-50 border-b border-gray-100 sticky top-0">
                <tr>{(res.columns || []).map((c, i) => <th key={i} className="text-left px-3 py-2 font-semibold text-gray-600 whitespace-nowrap">{c}</th>)}</tr>
              </thead>
              <tbody>
                {(res.rows || []).map((r, i) => (
                  <tr key={i} className="border-b border-gray-50 hover:bg-gray-50">
                    {r.map((v, j) => <td key={j} className="px-3 py-1.5 font-mono text-gray-700 whitespace-nowrap max-w-xs truncate">{v === null ? <span className="text-gray-300">null</span> : String(v)}</td>)}
                  </tr>
                ))}
                {!res.rows?.length && <tr><td colSpan={(res.columns || []).length || 1} className="px-3 py-6 text-center text-gray-400">No rows.</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Metrics tab ───────────────────────────────────────────────────────────────
function MetricsTab() {
  const [m, setM] = useState(null)
  useEffect(() => { api.get('/portal/metrics').then(({ data }) => setM(data)).catch(err => toast.error(errMsg(err))) }, [])
  if (!m) return <div className="text-gray-400 text-sm flex items-center gap-2"><RefreshCw size={14} className="animate-spin" /> Loading metrics…</div>
  const B = m.builder || {}, R = m.requests || {}
  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-sm font-semibold text-gray-700 mb-2">Builder Agent</h3>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <Stat label="Tasks" value={B.tasks_total ?? 0} />
          <Stat label="Applied" value={B.applies ?? 0} />
          <Stat label="Rollbacks" value={B.rollbacks ?? 0} />
          <Stat label="Auto-rollbacks" value={B.auto_rollbacks ?? 0} ok={(B.auto_rollbacks ?? 0) === 0 ? undefined : false} />
        </div>
        {B.tasks_by_status && Object.keys(B.tasks_by_status).length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-2">
            {Object.entries(B.tasks_by_status).map(([k, v]) => <span key={k} className={`${BUILDER_STATUS_STYLE[k] || 'badge-gray'}`}>{k}: {v}</span>)}
          </div>
        )}
      </div>
      <div>
        <h3 className="text-sm font-semibold text-gray-700 mb-2">Requests &amp; Agent</h3>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <Stat label="Requests" value={R.total ?? 0} />
          <Stat label="Open" value={R.by_status?.open ?? 0} />
          <Stat label="Approved" value={R.by_status?.approved ?? 0} />
          <Stat label="Agent queries" value={m.agent?.queries ?? 0} />
        </div>
      </div>
    </div>
  )
}

// ── Onboarding tab ────────────────────────────────────────────────────────────
function OnboardingTab() {
  const [content, setContent] = useState(null)
  const [err, setErr] = useState(false)
  useEffect(() => {
    api.get('/portal/docs/onboarding.md').then(({ data }) => setContent(data.content)).catch(() => setErr(true))
  }, [])
  if (err) return <div className="card p-6 text-sm text-gray-500">Onboarding guide not found. It lives at <span className="font-mono">docs/onboarding.md</span>.</div>
  if (content == null) return <div className="text-gray-400 text-sm flex items-center gap-2"><RefreshCw size={14} className="animate-spin" /> Loading…</div>
  return <div className="card p-6 max-w-3xl"><Markdown content={content} /></div>
}

// ── Page shell ────────────────────────────────────────────────────────────────
export default function DeveloperPortal() {
  const [tab, setTab] = useState('agent')
  const [meta, setMeta] = useState(null)

  useEffect(() => {
    api.get('/portal/meta').then(({ data }) => setMeta(data)).catch(() => {})
  }, [])

  return (
    <div>
      <div className="flex items-start justify-between mb-1 gap-4 flex-wrap">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <Code2 size={20} className="text-primary" />
            <h1 className="text-xl font-bold text-gray-800">Developer Portal</h1>
          </div>
          <p className="text-sm text-gray-500">Live codebase docs, database schema, API surface, and system health — read-only.</p>
        </div>
        {meta && (
          <div className="card px-3 py-2 text-xs text-gray-500 flex items-center gap-3">
            <span className="flex items-center gap-1.5"><KeyRound size={12} /> v{meta.version}</span>
            {meta.git?.short_sha && (
              <span className="flex items-center gap-1.5 font-mono" title={meta.git.last_commit_subject || ''}>
                <GitBranch size={12} /> {meta.git.branch || 'detached'}@{meta.git.short_sha}
              </span>
            )}
          </div>
        )}
      </div>

      <div className="flex gap-1 border-b border-gray-200 mb-5 mt-4 overflow-x-auto">
        {TABS.filter(t => !t.perm || isAdmin() || hasPermission(t.perm)).map(({ id, label, icon: Icon }) => (
          <button key={id} onClick={() => setTab(id)}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors whitespace-nowrap
              ${tab === id ? 'border-primary text-primary' : 'border-transparent text-gray-500 hover:text-gray-700'}`}>
            <Icon size={15} /> {label}
          </button>
        ))}
      </div>

      {tab === 'agent' && <AgentTab />}
      {tab === 'builder' && <BuilderTab />}
      {tab === 'requests' && <RequestsTab />}
      {tab === 'scheduled' && <ScheduledTab />}
      {tab === 'changelog' && <ChangelogTab />}
      {tab === 'onboarding' && <OnboardingTab />}
      {tab === 'docs' && <DocsTab />}
      {tab === 'schema' && <SchemaTab />}
      {tab === 'explorer' && <DataExplorerTab />}
      {tab === 'api' && <ApiTab />}
      {tab === 'metrics' && <MetricsTab />}
      {tab === 'health' && <HealthTab />}
      {tab === 'audit' && <AuditTab />}
    </div>
  )
}
