import React, { useState, useEffect, useMemo } from 'react'
import { ShieldCheck, Percent, Activity, RefreshCw, CheckCircle2, XCircle, Plus, Trash2, Pencil, Lightbulb, Search } from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'
import ActionModal from '../components/ActionModal'
import { inr } from '../utils/formatters'
import { isCoreLedgerPartner } from '../productRegistry'
import { hasPermission, isAdmin } from '../utils/permissions'

// ─────────────────────────────────────────────────────────────────────────────
// Workflow — Tier 1 operations layer:
//   Approvals  : maker-checker queue (+ on/off toggle, admin)
//   Fee Rules  : expected fee/commission per partner + verification settings
//   Anomalies  : duplicate references & volume spikes (rule-based scan)
// ─────────────────────────────────────────────────────────────────────────────

const TABS = [
  { key: 'approvals',   label: 'Approvals',        icon: ShieldCheck },
  { key: 'fees',        label: 'Fee Rules',        icon: Percent },
  { key: 'anomalies',   label: 'Anomalies',        icon: Activity },
  { key: 'suggestions', label: 'Rule Suggestions', icon: Lightbulb },
]

export default function Workflow() {
  const [tab, setTab] = useState('approvals')
  return (
    <div>
      <div className="flex items-center gap-2 mb-1">
        <ShieldCheck size={20} className="text-primary" />
        <h1 className="text-xl font-bold text-gray-800">Workflow</h1>
      </div>
      <p className="text-sm text-gray-500 mb-5">Maker-checker approvals · fee verification rules · anomaly scan</p>

      <div className="flex gap-2 mb-5">
        {TABS.map(({ key, label, icon: Icon }) => (
          <button key={key} onClick={() => setTab(key)}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${tab === key ? 'bg-primary text-white' : 'bg-white border border-gray-200 text-gray-600 hover:bg-gray-50'}`}>
            <Icon size={15} />{label}
          </button>
        ))}
      </div>

      {tab === 'approvals'   && <ApprovalsTab />}
      {tab === 'fees'        && <FeeRulesTab />}
      {tab === 'anomalies'   && <AnomaliesTab />}
      {tab === 'suggestions' && <RuleSuggestionsTab />}
    </div>
  )
}

// ═══════════════════════════════ Approvals ═══════════════════════════════════

function ApprovalsTab() {
  const [status, setStatus] = useState('pending')
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(false)
  const [mcEnabled, setMcEnabled] = useState(false)
  const [modal, setModal] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = () => {
    setLoading(true)
    api.get('/workflow/approvals', { params: { status } })
      .then(({ data }) => setRows(data)).catch(() => toast.error('Failed to load approvals'))
      .finally(() => setLoading(false))
    api.get('/workflow/settings').then(({ data }) => setMcEnabled(!!data.maker_checker_enabled)).catch(() => {})
  }
  useEffect(() => { load() }, [status])

  const toggleMc = async () => {
    try {
      const { data } = await api.put('/workflow/settings/maker-checker', { enabled: !mcEnabled })
      setMcEnabled(!!data.maker_checker_enabled)
      toast.success(`Maker-checker ${data.maker_checker_enabled ? 'enabled' : 'disabled'}`)
    } catch (e) { toast.error(e.response?.data?.detail || 'Only admins can change this') }
  }

  const review = (row, kind) => setModal({
    config: {
      title: kind === 'approve' ? 'Approve request' : 'Reject request',
      danger: kind === 'reject',
      confirmLabel: kind === 'approve' ? 'Approve & execute' : 'Reject',
      description: `${row.summary} — requested by ${row.requested_by}`,
      fields: [{ name: 'note', label: 'Note', required: kind === 'reject', placeholder: kind === 'reject' ? 'Why is this rejected? (required)' : 'optional' }],
    },
    action: async (v) => {
      await api.post(`/workflow/approvals/${row.id}/${kind}`, { note: v.note || '' })
      toast.success(kind === 'approve' ? 'Approved & executed' : 'Rejected')
    },
  })
  const runModal = async (v) => {
    setBusy(true)
    try { await modal.action(v); setModal(null); load() }
    catch (e) { toast.error(e.response?.data?.detail || 'Action failed') }
    finally { setBusy(false) }
  }

  return (
    <div className="max-w-5xl">
      <div className="card mb-4 flex items-center justify-between flex-wrap gap-3">
        <div>
          <div className="font-semibold text-gray-800 text-sm">Maker-checker (dual control)</div>
          <p className="text-xs text-gray-400 mt-0.5">
            When on, manual matches / overrides by non-admin users wait here until a different user approves them.
          </p>
        </div>
        <button onClick={toggleMc} disabled={!isAdmin()}
          title={isAdmin() ? '' : 'Only admins can change dual control'}
          className={`px-4 py-2 rounded-lg text-sm font-medium disabled:opacity-60 disabled:cursor-not-allowed ${mcEnabled ? 'bg-green-600 text-white' : 'bg-gray-200 text-gray-600'}`}>
          {mcEnabled ? 'ON — actions need approval' : 'OFF — actions execute directly'}
        </button>
      </div>

      <div className="card">
        <div className="flex items-center gap-2 mb-3">
          {['pending', 'approved', 'rejected', 'all'].map(s => (
            <button key={s} onClick={() => setStatus(s)}
              className={`px-3 py-1 rounded text-xs font-medium capitalize ${status === s ? 'bg-primary text-white' : 'bg-gray-100 text-gray-600'}`}>{s}</button>
          ))}
          <button onClick={load} className="btn-ghost text-xs flex items-center gap-1 ml-auto"><RefreshCw size={12} /> Refresh</button>
        </div>
        {loading ? <p className="text-xs text-gray-400 py-4 text-center">Loading…</p> : rows.length === 0 ? (
          <p className="text-sm text-gray-400 py-6 text-center">No {status === 'all' ? '' : status} requests.</p>
        ) : (
          <div className="space-y-2">
            {rows.map(r => (
              <div key={r.id} className="border border-gray-100 rounded-lg p-3 flex items-center gap-3 flex-wrap">
                <span className={`text-[11px] px-2 py-0.5 rounded-full font-medium ${r.status === 'pending' ? 'bg-amber-50 text-amber-700' : r.status === 'approved' ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-600'}`}>{r.status}</span>
                <span className="text-[11px] px-2 py-0.5 rounded bg-gray-100 text-gray-600">{r.action_type}</span>
                <div className="flex-1 min-w-[220px]">
                  <div className="text-sm text-gray-800">{r.summary}</div>
                  <div className="text-[11px] text-gray-400">
                    by {r.requested_by} · {r.requested_at?.slice(0, 16).replace('T', ' ')}
                    {r.reviewed_by && <> → {r.status} by {r.reviewed_by}{r.review_note ? ` (“${r.review_note}”)` : ''}</>}
                  </div>
                </div>
                {r.status === 'pending' && hasPermission('approver') && (
                  <div className="flex gap-2">
                    <button onClick={() => review(r, 'approve')} className="px-3 py-1 rounded text-xs font-medium bg-green-600 text-white hover:bg-green-700 flex items-center gap-1"><CheckCircle2 size={12} /> Approve</button>
                    <button onClick={() => review(r, 'reject')} className="px-3 py-1 rounded text-xs font-medium bg-red-600 text-white hover:bg-red-700 flex items-center gap-1"><XCircle size={12} /> Reject</button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <ActionModal open={!!modal} config={modal?.config} busy={busy} onClose={() => setModal(null)} onSubmit={runModal} />
    </div>
  )
}

// ═══════════════════════════════ Fee rules ═══════════════════════════════════

function FeeRulesTab() {
  const [rules, setRules] = useState([])
  const [partners, setPartners] = useState([])
  const [modal, setModal] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = () => {
    api.get('/workflow/fee-rules').then(({ data }) => setRules(data)).catch(() => toast.error('Failed to load fee rules'))
    api.get('/admin/partners-public').then(({ data }) => setPartners((data || []).filter(isCoreLedgerPartner))).catch(() => {})
  }
  useEffect(() => { load() }, [])

  const openForm = (rule = null) => setModal({
    config: {
      title: rule ? `Edit fee rule — ${rule.partner}` : 'New fee rule',
      confirmLabel: rule ? 'Save' : 'Create',
      fields: [
        { name: 'partner', label: 'Partner', type: 'select', required: true,
          options: partners.map(p => ({ value: p.slug, label: p.display_name })), default: rule?.partner },
        { name: 'label', label: 'Label', required: true, placeholder: 'e.g. PayU MDR 1.1% + GST', default: rule?.label },
        { name: 'fee_type', label: 'Fee type', type: 'select', options: ['percent', 'flat'], default: rule?.fee_type || 'percent' },
        { name: 'fee_value', label: 'Fee value (% or ₹)', required: true, default: rule != null ? String(rule.fee_value) : '' },
        { name: 'gst_percent', label: 'GST % on fee', default: rule != null ? String(rule.gst_percent) : '18' },
        { name: 'tolerance', label: 'Tolerance (₹)', default: rule != null ? String(rule.tolerance) : '0.5' },
      ],
    },
    action: async (v) => {
      const body = { partner: v.partner, label: v.label, fee_type: v.fee_type,
        fee_value: parseFloat(v.fee_value || 0), gst_percent: parseFloat(v.gst_percent || 18),
        tolerance: parseFloat(v.tolerance || 0.5), is_active: true }
      if (rule) await api.put(`/workflow/fee-rules/${rule.id}`, body)
      else      await api.post('/workflow/fee-rules', body)
      toast.success(rule ? 'Rule updated' : 'Rule created')
    },
  })
  const runModal = async (v) => {
    setBusy(true)
    try { await modal.action(v); setModal(null); load() }
    catch (e) { toast.error(e.response?.data?.detail || 'Save failed') }
    finally { setBusy(false) }
  }
  const remove = (rule) => setModal({
    config: { title: 'Delete fee rule', danger: true, confirmLabel: 'Delete',
      description: `${rule.partner} — ${rule.label}`, fields: [] },
    action: async () => { await api.delete(`/workflow/fee-rules/${rule.id}`); toast.success('Deleted') },
  })

  return (
    <div className="max-w-4xl">
      <div className="card">
        <div className="flex items-center justify-between mb-3">
          <p className="text-xs text-gray-400">Expected fee per partner — used by “Verify Fees” in each product window to flag wrong deductions.</p>
          {hasPermission('logic_builder') && <button onClick={() => openForm(null)} className="btn-primary text-xs flex items-center gap-1"><Plus size={13} /> Add rule</button>}
        </div>
        {rules.length === 0 ? <p className="text-sm text-gray-400 py-6 text-center">No fee rules yet — add one to start verifying charges.</p> : (
          <table className="w-full text-xs">
            <thead><tr className="border-b text-gray-500">
              <th className="table-th">Partner</th><th className="table-th">Label</th><th className="table-th">Fee</th>
              <th className="table-th">GST %</th><th className="table-th">Tolerance</th><th className="table-th">Actions</th>
            </tr></thead>
            <tbody>{rules.map(r => (
              <tr key={r.id} className="border-b border-gray-50">
                <td className="table-td font-medium">{r.partner}</td>
                <td className="table-td">{r.label}</td>
                <td className="table-td">{r.fee_type === 'percent' ? `${r.fee_value}%` : inr(r.fee_value)}</td>
                <td className="table-td">{r.gst_percent}%</td>
                <td className="table-td">{inr(r.tolerance)}</td>
                <td className="table-td"><div className="flex gap-1">
                  {hasPermission('logic_builder') && <>
                    <button onClick={() => openForm(r)} className="p-1 rounded hover:bg-blue-50 text-blue-600"><Pencil size={12} /></button>
                    <button onClick={() => remove(r)} className="p-1 rounded hover:bg-red-50 text-red-500"><Trash2 size={12} /></button>
                  </>}
                </div></td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </div>
      <ActionModal open={!!modal} config={modal?.config} busy={busy} onClose={() => setModal(null)} onSubmit={runModal} />
    </div>
  )
}

// ═══════════════════════════════ Anomalies ═══════════════════════════════════

function AnomalyTable({ title, icon, items = [], columns, searchKey, empty, accent = 'red' }) {
  const [query, setQuery] = useState('')
  const [showAll, setShowAll] = useState(false)
  const TOP = 15

  const filtered = useMemo(() => {
    if (!query.trim()) return items
    const q = query.trim().toLowerCase()
    return items.filter(it => String(it[searchKey] || '').toLowerCase().includes(q))
  }, [items, query, searchKey])
  const visible = showAll ? filtered : filtered.slice(0, TOP)

  return (
    <div className="card p-0 overflow-hidden">
      <div className={`flex items-center justify-between px-4 py-3 border-b ${items.length ? `bg-${accent}-50/40 border-${accent}-100` : 'bg-gray-50/50 border-gray-100'}`}>
        <h3 className="font-semibold text-gray-800 text-sm flex items-center gap-2">
          <span>{icon}</span>{title}
          <span className={`text-[11px] px-2 py-0.5 rounded-full font-bold ${items.length ? 'bg-red-100 text-red-700' : 'bg-green-100 text-green-700'}`}>
            {items.length ? `${items.length} found` : 'clear ✓'}
          </span>
        </h3>
        {items.length > 5 && (
          <div className="relative">
            <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-300" />
            <input className="input text-xs py-1 pl-6 w-44" placeholder="Search…" value={query} onChange={e => setQuery(e.target.value)} />
          </div>
        )}
      </div>
      {items.length === 0 ? (
        <p className="text-xs text-gray-400 px-4 py-4">{empty}</p>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead><tr className="border-b border-gray-100 bg-gray-50/50 text-gray-400">
                {columns.map(c => <th key={c.label} className={`py-2 px-4 text-xs font-semibold uppercase tracking-wide ${c.right ? 'text-right' : 'text-left'}`}>{c.label}</th>)}
              </tr></thead>
              <tbody>
                {visible.map((it, i) => (
                  <tr key={i} className="border-b border-gray-50 hover:bg-gray-50/60">
                    {columns.map(c => <td key={c.label} className={`py-2 px-4 ${c.right ? 'text-right tabular-nums' : ''}`}>{c.render(it)}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {filtered.length > TOP && (
            <div className="px-4 py-2 border-t border-gray-50 text-center">
              <button onClick={() => setShowAll(s => !s)} className="text-xs text-primary hover:underline">
                {showAll ? `Show top ${TOP} only` : `Show all ${filtered.length}`}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function AnomaliesTab() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)

  const scan = () => {
    setLoading(true)
    api.get('/workflow/anomalies').then(({ data }) => setData(data))
      .catch(() => toast.error('Anomaly scan failed')).finally(() => setLoading(false))
  }
  useEffect(() => { scan() }, [])

  const counts = {
    utr: data?.duplicate_utr?.length || 0,
    tid: data?.duplicate_tid?.length || 0,
    vol: data?.volume_spikes?.length || 0,
  }

  return (
    <div className="max-w-5xl space-y-4">
      {/* Summary strip */}
      <div className="grid grid-cols-3 gap-3">
        {[
          { l: 'Duplicate UTRs', v: counts.utr, i: '🔁' },
          { l: 'Duplicate TIDs', v: counts.tid, i: '🆔' },
          { l: 'Volume anomalies', v: counts.vol, i: '📈' },
        ].map(c => (
          <div key={c.l} className="bg-white rounded-lg border border-gray-100 p-3 flex items-center gap-3">
            <span className="text-xl">{c.i}</span>
            <div>
              <div className={`text-lg font-bold ${c.v ? 'text-red-600' : 'text-green-600'}`}>{c.v}</div>
              <div className="text-[11px] text-gray-500">{c.l}</div>
            </div>
          </div>
        ))}
        <div className="col-span-3 flex justify-end -mt-1">
          <button onClick={scan} disabled={loading} className="btn-ghost text-xs flex items-center gap-1">
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} /> {loading ? 'Scanning…' : 'Re-scan'}
          </button>
        </div>
      </div>

      <AnomalyTable
        title="Duplicate UTRs (bank side)" icon="🔁" items={data?.duplicate_utr || []}
        searchKey="utr_number" empty="No duplicate UTRs found — every bank UTR is unique."
        columns={[
          { label: 'UTR Number', render: it => <span className="font-mono">{it.utr_number}</span> },
          { label: 'Occurrences', right: true, render: it => <span className="font-bold text-red-600">{it.count}×</span> },
        ]}
      />
      <AnomalyTable
        title="Duplicate Eko TIDs" icon="🆔" items={data?.duplicate_tid || []}
        searchKey="eko_tid" empty="No duplicate TIDs found."
        columns={[
          { label: 'Eko TID', render: it => <span className="font-mono">{it.eko_tid}</span> },
          { label: 'Side', render: it => <span className="capitalize px-2 py-0.5 rounded-full bg-gray-100 text-gray-600">{it.side}</span> },
          { label: 'Occurrences', right: true, render: it => <span className="font-bold text-red-600">{it.count}×</span> },
        ]}
      />
      <AnomalyTable
        title="Volume anomalies (vs trailing 7-day average)" icon="📈" items={data?.volume_spikes || []}
        searchKey="partner" empty="No volume spikes or drops detected." accent="amber"
        columns={[
          { label: 'Partner', render: it => <span className="capitalize font-medium">{it.partner}</span> },
          { label: 'Date', render: it => <span className="font-mono">{it.date}</span> },
          { label: 'Txns', right: true, render: it => it.count.toLocaleString() },
          { label: '7-day avg', right: true, render: it => it.trailing_avg },
          { label: 'Signal', render: it => <span className={`px-2 py-0.5 rounded-full font-medium ${it.direction === 'spike' ? 'bg-amber-50 text-amber-700' : 'bg-red-50 text-red-600'}`}>{it.direction === 'spike' ? '▲ spike' : '▼ drop'}</span> },
        ]}
      />
    </div>
  )
}

// ═══════════════════════════════ Rule suggestions (Tier 2) ═══════════════════

function RuleSuggestionsTab() {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(false)

  const load = () => {
    setLoading(true)
    api.get('/workflow/rule-suggestions', { params: { min_hits: 2 } })
      .then(({ data }) => setRows(data)).catch(() => toast.error('Failed to load suggestions'))
      .finally(() => setLoading(false))
  }
  useEffect(() => { load() }, [])

  const act = async (row, kind) => {
    try {
      const { data } = await api.post(`/workflow/rule-suggestions/${row.id}/${kind}`)
      toast.success(kind === 'accept' ? `Rule created: ${data.created_rule}` : 'Dismissed')
      load()
    } catch (e) { toast.error(e.response?.data?.detail || 'Action failed') }
  }

  return (
    <div className="max-w-4xl">
      <div className="card">
        <div className="flex items-center justify-between mb-3">
          <p className="text-xs text-gray-400">
            Patterns learned from your team's manual matches. When the same field combination keeps
            working, promote it to a real matching rule — future recon runs apply it automatically.
          </p>
          <button onClick={load} disabled={loading} className="btn-ghost text-xs flex items-center gap-1"><RefreshCw size={12} /> Refresh</button>
        </div>
        {rows.length === 0 ? (
          <p className="text-sm text-gray-400 py-6 text-center">
            Nothing learned yet — patterns appear here after the same kind of manual match happens at least twice.
          </p>
        ) : (
          <div className="space-y-2">
            {rows.map(r => (
              <div key={r.id} className="border border-gray-100 rounded-lg p-3 flex items-center gap-3 flex-wrap">
                <Lightbulb size={15} className="text-amber-500" />
                <div className="flex-1 min-w-[240px]">
                  <div className="text-sm text-gray-800">
                    <span className="capitalize font-medium">{r.partner}</span>: match on{' '}
                    {r.fields.map(f => <span key={f} className="font-mono text-xs bg-indigo-50 text-indigo-700 px-1.5 py-0.5 rounded mx-0.5">{f}</span>)}
                  </div>
                  <div className="text-[11px] text-gray-400 mt-0.5">seen {r.hit_count}× in manual matches · last {r.last_seen?.slice(0, 10)}</div>
                </div>
                <div className="flex gap-2">
                  <button onClick={() => act(r, 'accept')} className="px-3 py-1 rounded text-xs font-medium bg-indigo-600 text-white hover:bg-indigo-700">Create rule</button>
                  <button onClick={() => act(r, 'dismiss')} className="px-3 py-1 rounded text-xs font-medium bg-gray-100 text-gray-600 hover:bg-gray-200">Dismiss</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
