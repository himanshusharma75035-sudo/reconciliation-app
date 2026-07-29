import React, { useState, useEffect } from 'react'
import {
  TrendingUp, AlertTriangle, FileText, CheckSquare, CalendarClock, Shield,
  Download, Mail, RefreshCw, Play, Copy,
} from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'
import { isCoreLedgerPartner } from '../productRegistry'
import { hasPermission } from '../utils/permissions'

// Fallback only — used until /admin/partners-public loads. Kept in sync with the seeded
// core partners (was missing levin / digikhata / indonepal).
const FALLBACK_PARTNERS = [
  { slug: 'fino', display_name: 'Fino' }, { slug: 'airtel', display_name: 'Airtel' },
  { slug: 'axis', display_name: 'Axis' }, { slug: 'levin', display_name: 'Levin' },
  { slug: 'aeps', display_name: 'AePS' }, { slug: 'pg', display_name: 'PG' },
  { slug: 'qr', display_name: 'QR' }, { slug: 'digikhata', display_name: 'Digikhata' },
  { slug: 'indonepal', display_name: 'Indo-Nepal' },
]
const inr = n => '₹' + Number(n || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })
const isAdmin = () => { try { return JSON.parse(localStorage.getItem('user') || '{}').role === 'admin' } catch { return false } }

// Shared System/AI badge — AI content is advisory (de-identified, verify before acting).
const AiBadge = () => (
  <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-violet-100 text-violet-700" title="AI-generated from de-identified aggregates — advisory">🤖 AI</span>
)
const likeClass = l => l === 'high' ? 'bg-red-50 text-red-600' : l === 'medium' ? 'bg-amber-50 text-amber-700' : 'bg-gray-100 text-gray-600'
const trendClass = t => t === 'worsening' ? 'text-red-600' : t === 'improving' ? 'text-green-600' : 'text-gray-500'

export default function Insights() {
  const [tab, setTab] = useState('trend')
  const [partners, setPartners] = useState([])
  useEffect(() => { api.get('/admin/partners-public').then(({ data }) => { const core = (data || []).filter(isCoreLedgerPartner); setPartners(core.length ? core : FALLBACK_PARTNERS) }).catch(() => setPartners(FALLBACK_PARTNERS)) }, [])

  const TABS = [
    { key: 'trend', label: 'Trend', icon: TrendingUp },
    { key: 'escalation', label: 'Escalation', icon: AlertTriangle },
    { key: 'certificate', label: 'Certificate', icon: FileText },
    { key: 'sanity', label: 'Pre-Recon Check', icon: CheckSquare },
    { key: 'carry', label: 'Carry-Forward', icon: CalendarClock },
    ...(isAdmin() ? [{ key: 'sessions', label: 'Session Log', icon: Shield }] : []),
  ]
  return (
    <div>
      <div className="flex items-center gap-2 mb-1"><TrendingUp size={20} className="text-primary" /><h1 className="text-xl font-bold text-gray-800">Insights & Controls</h1></div>
      <p className="text-sm text-gray-500 mb-5">Trends, escalations, certificates, and data-quality controls for manager review.</p>
      <div className="flex gap-2 mb-5 flex-wrap">
        {TABS.map(({ key, label, icon: Icon }) => (
          <button key={key} onClick={() => setTab(key)} className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${tab === key ? 'bg-primary text-white' : 'bg-white border border-gray-200 text-gray-600 hover:bg-gray-50'}`}>
            <Icon size={15} />{label}
          </button>
        ))}
      </div>
      {tab === 'trend' && <Trend partners={partners} />}
      {tab === 'escalation' && <Escalation partners={partners} />}
      {tab === 'certificate' && <Certificate partners={partners} />}
      {tab === 'sanity' && <Sanity partners={partners} />}
      {tab === 'carry' && <CarryForward partners={partners} />}
      {tab === 'sessions' && <Sessions />}
    </div>
  )
}

function Trend({ partners }) {
  const [partner, setPartner] = useState('')
  const [months, setMonths] = useState(3)
  const [data, setData] = useState(null)
  const [ai, setAi] = useState(null)
  const [aiLoading, setAiLoading] = useState(false)
  const load = () => api.get('/insights/trend', { params: { partner: partner || undefined, months } }).then(({ data }) => setData(data)).catch(() => toast.error('Failed'))
  useEffect(() => { load(); setAi(null) }, [partner, months])
  const explain = () => {
    setAiLoading(true)
    api.get('/insights/trend-narrative-ai', { params: { partner: partner || undefined, months } })
      .then(({ data }) => { if (data.error) toast(data.error, { icon: '🤖' }); setAi(data) })
      .catch(() => toast.error('AI explain failed')).finally(() => setAiLoading(false))
  }
  return (
    <div className="max-w-5xl">
      <div className="flex items-center gap-3 mb-4">
        <select className="select w-44" value={partner} onChange={e => setPartner(e.target.value)}><option value="">All partners</option>{partners.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}</select>
        <select className="select w-28" value={months} onChange={e => setMonths(+e.target.value)}>{[3, 6, 12].map(m => <option key={m} value={m}>{m} months</option>)}</select>
        <button onClick={load} className="btn-ghost text-xs flex items-center gap-1"><RefreshCw size={12} /> Refresh</button>
        <button onClick={explain} disabled={aiLoading} className="btn-ghost text-xs flex items-center gap-1 text-violet-600 hover:border-violet-200">🤖 {aiLoading ? 'Thinking…' : 'Explain trend'}</button>
      </div>
      {ai && (ai.narrative || (ai.highlights || []).length > 0) && (
        <div className="card mb-4 border-violet-100 bg-violet-50/20">
          <div className="flex items-center gap-2 mb-1"><AiBadge /><span className="text-xs font-semibold text-gray-700">AI trend summary</span></div>
          {ai.narrative && <p className="text-sm text-gray-700 mb-2">{ai.narrative}</p>}
          {(ai.highlights || []).length > 0 && (
            <ul className="space-y-1">
              {ai.highlights.map((h, i) => (
                <li key={i} className="text-xs text-gray-600 flex items-start gap-2">
                  <span className={`font-semibold capitalize ${trendClass(h.trend)}`}>{h.trend}</span>
                  <span className="capitalize font-medium">{h.product}</span>
                  <span className="text-gray-400">— {h.detail}</span>
                </li>
              ))}
            </ul>
          )}
          <p className="text-[10px] text-gray-400 mt-2">{ai.disclaimer}</p>
        </div>
      )}
      {data && (
        <div className="card overflow-x-auto">
          <table className="w-full text-xs">
            <thead><tr className="border-b text-gray-500"><th className="table-th text-left">Product</th>{data.periods.map(p => <th key={p} className="table-th text-right">{p}</th>)}</tr></thead>
            <tbody>{data.partners.map(row => (
              <tr key={row.partner} className="border-b border-gray-50 hover:bg-gray-50">
                <td className="table-td capitalize font-medium">{row.partner}</td>
                {row.by_month.map((m, i) => (
                  <td key={i} className="table-td text-right">
                    <div className={`font-semibold ${m.match_rate >= 90 ? 'text-green-600' : m.match_rate >= 50 ? 'text-amber-600' : 'text-red-500'}`}>{m.match_rate}%</div>
                    <div className="text-[10px] text-gray-400">{m.matched}/{m.total} · {m.open} open</div>
                  </td>
                ))}
              </tr>
            ))}</tbody>
          </table>
          {data.partners.length === 0 && <p className="text-xs text-gray-400 text-center py-6">No data in range.</p>}
        </div>
      )}
    </div>
  )
}

function Escalation({ partners }) {
  const [partner, setPartner] = useState('')
  const [data, setData] = useState(null)
  const [draft, setDraft] = useState(null)
  const [rc, setRc] = useState(null)
  const [rcLoading, setRcLoading] = useState(false)
  const load = () => api.get('/insights/escalation', { params: { partner: partner || undefined } }).then(({ data }) => setData(data)).catch(() => toast.error('Failed'))
  useEffect(() => { load(); setRc(null) }, [partner])
  const makeDraft = () => { if (!partner) return toast.error('Pick a partner'); api.get('/insights/escalation/email-draft', { params: { partner } }).then(({ data }) => setDraft(data)).catch(() => toast.error('Failed')) }
  const rootCause = () => {
    setRcLoading(true)
    api.get('/insights/escalation-rootcause-ai', { params: { partner: partner || undefined } })
      .then(({ data }) => { if (data.error) toast(data.error, { icon: '🤖' }); setRc(data) })
      .catch(() => toast.error('AI root-cause failed')).finally(() => setRcLoading(false))
  }
  return (
    <div className="max-w-5xl">
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <select className="select w-44" value={partner} onChange={e => setPartner(e.target.value)}><option value="">All partners</option>{partners.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}</select>
        <button onClick={load} className="btn-ghost text-xs flex items-center gap-1"><RefreshCw size={12} /> Refresh</button>
        <button onClick={makeDraft} disabled={!partner} className="btn-primary text-xs flex items-center gap-1 disabled:opacity-50"><Mail size={13} /> Draft email</button>
        <button onClick={rootCause} disabled={rcLoading} className="btn-ghost text-xs flex items-center gap-1 text-violet-600 hover:border-violet-200">🤖 {rcLoading ? 'Thinking…' : 'AI root-cause'}</button>
      </div>
      {data && <p className="text-sm text-gray-600 mb-2"><strong className="text-red-600">{data.count}</strong> item(s) open beyond D+7, totalling <strong>{inr(data.total_amount)}</strong></p>}
      {rc && ((rc.hypotheses || []).length > 0 || rc.note) && (
        <div className="card mb-4 border-violet-100 bg-violet-50/20">
          <div className="flex items-center gap-2 mb-2"><AiBadge /><span className="text-xs font-semibold text-gray-700">AI root-cause hypotheses</span></div>
          {(rc.hypotheses || []).length > 0 && (
            <ul className="space-y-1.5 mb-2">
              {rc.hypotheses.map((h, i) => (
                <li key={i} className="text-xs text-gray-700 flex items-start gap-2">
                  <span className={`px-2 py-0.5 rounded-full font-medium capitalize ${likeClass(h.likelihood)}`}>{h.likelihood}</span>
                  <span><strong>{h.title}</strong>{h.rationale ? <span className="text-gray-400"> — {h.rationale}</span> : null}</span>
                </li>
              ))}
            </ul>
          )}
          {rc.note && (
            <div className="mt-2">
              <div className="flex items-center justify-between mb-1"><span className="text-[11px] font-semibold text-violet-700">Suggested escalation note</span>
                <button onClick={() => { navigator.clipboard?.writeText(rc.note); toast.success('Copied') }} className="btn-ghost text-[11px] flex items-center gap-1"><Copy size={11} /> Copy</button></div>
              <pre className="text-[11px] text-gray-600 whitespace-pre-wrap font-sans">{rc.note}</pre>
            </div>
          )}
          <p className="text-[10px] text-gray-400 mt-2">{rc.disclaimer}</p>
        </div>
      )}
      {draft && (
        <div className="card mb-4 bg-blue-50/40">
          <div className="flex items-center justify-between mb-1"><span className="text-xs font-semibold text-blue-700">Email draft</span>
            <button onClick={() => { navigator.clipboard?.writeText(`Subject: ${draft.subject}\n\n${draft.body}`); toast.success('Copied') }} className="btn-ghost text-[11px] flex items-center gap-1"><Copy size={11} /> Copy</button></div>
          <div className="text-xs font-semibold text-gray-700 mb-1">{draft.subject}</div>
          <pre className="text-[11px] text-gray-600 whitespace-pre-wrap font-sans">{draft.body}</pre>
        </div>
      )}
      {data && data.items.length > 0 && (
        <div className="card overflow-x-auto"><table className="w-full text-xs">
          <thead><tr className="border-b text-gray-500"><th className="table-th">Partner</th><th className="table-th">Date</th><th className="table-th text-right">Age</th><th className="table-th">Eko TID</th><th className="table-th">UTR</th><th className="table-th text-right">Amount</th></tr></thead>
          <tbody>{data.items.map(it => (
            <tr key={it.id} className="border-b border-gray-50 hover:bg-red-50/30">
              <td className="table-td capitalize">{it.partner}</td><td className="table-td font-mono">{it.recon_date}</td>
              <td className="table-td text-right font-bold text-red-600">{it.age_days}</td><td className="table-td font-mono">{it.eko_tid || '—'}</td>
              <td className="table-td font-mono">{it.utr || '—'}</td><td className="table-td text-right tabular-nums">{inr(it.amount)}</td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
    </div>
  )
}

function Certificate({ partners }) {
  const [partner, setPartner] = useState(partners[0]?.slug || 'fino')
  const [month, setMonth] = useState(new Date().toISOString().slice(0, 7))
  const dl = () => {
    if (!partner) return toast.error('Pick a partner')
    api.get(`/insights/certificate?partner=${partner}&month=${month}`, { responseType: 'blob' })
      .then(r => { const u = URL.createObjectURL(r.data); const a = document.createElement('a'); a.href = u; a.download = `recon_certificate_${partner}_${month}.pdf`; a.click(); URL.revokeObjectURL(u) })
      .catch(() => toast.error('Generation failed (is reportlab installed?)'))
  }
  return (
    <div className="card max-w-lg">
      <h2 className="font-semibold text-gray-700 mb-1">Monthly Reconciliation Certificate</h2>
      <p className="text-xs text-gray-400 mb-4">A formal one-page PDF (opening, transactions, matched, unmatched, closing + sign-off) for filing.</p>
      <div className="grid grid-cols-2 gap-3 mb-4">
        <div><label className="text-xs text-gray-500 block mb-1">Partner</label><select className="select" value={partner} onChange={e => setPartner(e.target.value)}>{partners.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}</select></div>
        <div><label className="text-xs text-gray-500 block mb-1">Month</label><input type="month" className="input" value={month} onChange={e => setMonth(e.target.value)} /></div>
      </div>
      {hasPermission('reports') && <button onClick={dl} className="btn-primary flex items-center gap-2"><Download size={15} /> Download Certificate PDF</button>}
    </div>
  )
}

function Sanity({ partners }) {
  const [partner, setPartner] = useState(partners[0]?.slug || 'fino')
  const [date, setDate] = useState('')
  const [res, setRes] = useState(null)
  const run = () => { if (!partner || !date) return toast.error('Partner + date required'); api.get('/insights/pre-recon-check', { params: { partner, recon_date: date } }).then(({ data }) => setRes(data)).catch(() => toast.error('Failed')) }
  return (
    <div className="card max-w-lg">
      <h2 className="font-semibold text-gray-700 mb-1">Pre-Recon Sanity Check</h2>
      <p className="text-xs text-gray-400 mb-4">Compare bank vs internal row counts before running matching — catch a missing file early.</p>
      <div className="grid grid-cols-2 gap-3 mb-4">
        <div><label className="text-xs text-gray-500 block mb-1">Partner</label><select className="select" value={partner} onChange={e => setPartner(e.target.value)}>{partners.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}</select></div>
        <div><label className="text-xs text-gray-500 block mb-1">Recon Date</label><input type="date" className="input" value={date} onChange={e => setDate(e.target.value)} /></div>
      </div>
      <button onClick={run} className="btn-primary flex items-center gap-2"><CheckSquare size={15} /> Check</button>
      {res && (
        <div className={`mt-4 rounded-lg p-3 ${res.proceed_ok ? 'bg-green-50' : 'bg-amber-50'}`}>
          <div className="grid grid-cols-3 gap-2 text-center mb-2">
            {[['Bank', res.bank], ['Internal', res.internal], ['Diff', res.difference]].map(([l, v]) => (
              <div key={l}><div className="text-lg font-bold text-gray-800">{v}</div><div className="text-[11px] text-gray-500">{l}</div></div>
            ))}
          </div>
          <div className={`text-xs font-medium ${res.proceed_ok ? 'text-green-700' : 'text-amber-700'}`}>{res.warning || 'Counts look consistent — OK to proceed.'}</div>
        </div>
      )}
    </div>
  )
}

function CarryForward({ partners }) {
  const [partner, setPartner] = useState(partners[0]?.slug || 'fino')
  const [date, setDate] = useState('')
  const [busy, setBusy] = useState(false)
  const run = async () => {
    if (!partner || !date) return toast.error('Partner + date required')
    setBusy(true)
    try { const { data } = await api.post(`/insights/run-carry-forward?partner=${partner}&recon_date=${date}`); toast.success(`Carry-forward (D+${data.carry_days}): ${data.matched} matched`) }
    catch (e) { toast.error(e.response?.data?.detail || 'Failed') } finally { setBusy(false) }
  }
  return (
    <div className="card max-w-lg">
      <h2 className="font-semibold text-gray-700 mb-1">Settlement Carry-Forward (D+N)</h2>
      <p className="text-xs text-gray-400 mb-4">Match prior-day unmatched bank rows (with UTR) against today's internal rows. Uses each partner's configured carry-forward days (set in Configuration).</p>
      <div className="grid grid-cols-2 gap-3 mb-4">
        <div><label className="text-xs text-gray-500 block mb-1">Partner</label><select className="select" value={partner} onChange={e => setPartner(e.target.value)}>{partners.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}</select></div>
        <div><label className="text-xs text-gray-500 block mb-1">Recon Date</label><input type="date" className="input" value={date} onChange={e => setDate(e.target.value)} /></div>
      </div>
      {hasPermission('run_recon') && <button onClick={run} disabled={busy} className="btn-primary flex items-center gap-2"><Play size={15} /> Run Carry-Forward</button>}
    </div>
  )
}

function Sessions() {
  const [data, setData] = useState(null)
  const [event, setEvent] = useState('')
  const load = () => api.get('/auth/session-logs', { params: { event: event || undefined } }).then(({ data }) => setData(data)).catch(() => toast.error('Failed'))
  useEffect(() => { load() }, [event])
  return (
    <div className="max-w-5xl">
      <div className="flex items-center gap-3 mb-3">
        <select className="select w-44" value={event} onChange={e => setEvent(e.target.value)}>
          <option value="">All events</option>{['login', 'logout', 'login_failed', 'expired'].map(e => <option key={e} value={e}>{e}</option>)}
        </select>
        <button onClick={load} className="btn-ghost text-xs flex items-center gap-1"><RefreshCw size={12} /> Refresh</button>
        {data && <span className="text-xs text-gray-400">{data.total} events</span>}
      </div>
      {data && (
        <div className="card overflow-x-auto"><table className="w-full text-xs">
          <thead><tr className="border-b text-gray-500"><th className="table-th">Time</th><th className="table-th">User</th><th className="table-th">Event</th><th className="table-th">IP</th><th className="table-th text-left">Detail</th></tr></thead>
          <tbody>{data.logs.map((l, i) => (
            <tr key={i} className="border-b border-gray-50 hover:bg-gray-50">
              <td className="table-td font-mono">{l.created_at?.slice(0, 19)}</td><td className="table-td">{l.username}</td>
              <td className="table-td"><span className={`px-2 py-0.5 rounded-full ${l.event === 'login_failed' ? 'bg-red-50 text-red-600' : l.event === 'login' ? 'bg-green-50 text-green-700' : 'bg-gray-100 text-gray-600'}`}>{l.event}</span></td>
              <td className="table-td font-mono">{l.ip_address || '—'}</td><td className="table-td text-left text-gray-400">{l.detail || ''}</td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
    </div>
  )
}
