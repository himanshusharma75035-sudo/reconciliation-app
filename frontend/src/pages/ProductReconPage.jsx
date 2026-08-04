import React, { useState, useEffect } from 'react'
import { useParams, Link } from 'react-router-dom'
import {
  Upload as UploadIcon, FileSpreadsheet, Landmark, ArrowLeft, Play, RefreshCw,
  ExternalLink, AlertTriangle, CheckCircle2, Download, Tag, GitMerge, Layers,
  ArrowLeftRight, UserPlus,
} from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'
import Upload from './Upload'
import AepsSettlement from './AepsSettlement'
import QRSettlement from './QRSettlement'
import ActionModal from '../components/ActionModal'
import { PRODUCT_BY_ID } from '../productRegistry'
import { useSrcCodes } from '../srcCodes'

import { inr, today } from '../utils/formatters'
import { STATUS_COLOR } from '../utils/statusColors'
import { CORE_RECON_STATUS_OPTS } from '../filterModel'

const PARTNER_LABEL = {
  dmt: 'All Banks', fino: 'Fino', airtel: 'Airtel', axis: 'Axis', levin: 'Levin', aeps: 'AePS',
  pg: 'PG', qr: 'QR', digikhata: 'Digikhata', indonepal: 'Indo-Nepal',
}
const OVERRIDE_TARGETS = ['unmatched', 'failed', 'src_assigned', 'adhoc_settlement', 'human_override']

function downloadBlob(data, name) {
  const u = URL.createObjectURL(data); const a = document.createElement('a')
  a.href = u; a.download = name; a.click(); URL.revokeObjectURL(u)
}

export default function ProductReconPage() {
  const { slug } = useParams()
  const meta = PRODUCT_BY_ID[slug]
  const [tab, setTab] = useState('upload')

  if (!meta) return (
    <div className="card max-w-lg"><p className="text-sm text-gray-500">Unknown product “{slug}”. <Link to="/upload" className="text-primary">Back to products</Link></p></div>
  )

  const tabs = [
    { key: 'upload', label: 'Upload', icon: UploadIcon },
    { key: 'recon',  label: 'Reconciliation', icon: FileSpreadsheet },
    ...(meta.settlement ? [{ key: 'settlement', label: 'Settlement', icon: Landmark }] : []),
  ]

  return (
    <div>
      <Link to="/upload" className="text-xs text-gray-400 hover:text-primary flex items-center gap-1 mb-2">
        <ArrowLeft size={13} /> All products
      </Link>
      <div className="flex items-center gap-2 mb-1">
        <span className="text-2xl">{meta.icon}</span>
        <h1 className="text-xl font-bold text-gray-800">{meta.label} Reconciliation</h1>
      </div>
      <p className="text-sm text-gray-500 mb-5">
        Upload data, reconcile{meta.settlement ? ', and verify settlement' : ''} for {meta.label} — all in one window.
      </p>

      <div className="flex gap-2 mb-5">
        {tabs.map(({ key, label, icon: Icon }) => (
          <button key={key} onClick={() => setTab(key)}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${tab === key ? 'bg-primary text-white' : 'bg-white border border-gray-200 text-gray-600 hover:bg-gray-50'}`}>
            <Icon size={15} />{label}
          </button>
        ))}
      </div>

      {tab === 'upload' && <Upload forcedProduct={slug} embedded />}
      {tab === 'recon'  && <ProductReconTab slug={slug} partners={meta.partners} />}
      {tab === 'settlement' && meta.settlement === 'aeps' && <AepsSettlement embedded />}
      {tab === 'settlement' && meta.settlement === 'qr'   && <QRSettlement embedded />}
    </div>
  )
}

// ── Self-contained per-product reconciliation tab ───────────────────────────
function ProductReconTab({ slug, partners }) {
  const [partner, setPartner] = useState(partners[0])
  const [dateMode, setDateMode] = useState('single')  // 'single' | 'range'
  const [date, setDate]       = useState(today())
  const [fromDate, setFromDate] = useState(today())
  const [toDate, setToDate]   = useState(today())
  const [running, setRunning] = useState(false)
  const [busy, setBusy]       = useState(false)
  const [result, setResult]   = useState(null)
  const [status, setStatus]   = useState('all')      // default: show the true status mix
  const [side, setSide]       = useState('')         // '' = both sides
  const [rows, setRows]       = useState([])
  const [total, setTotal]     = useState(0)
  const [page, setPage]       = useState(1)
  const PAGE_SIZE = 100
  const [loading, setLoading] = useState(false)
  const srcCodes = useSrcCodes()
  const [selected, setSelected] = useState(() => new Set())
  const [assignedFilter, setAssignedFilter] = useState('')   // '' | 'me' | 'unassigned' | username

  // Per-column keyword filters (client-side, over the loaded page).
  // Each box accepts one or many keywords (comma/space separated) — a row matches
  // a column if ANY keyword is contained in it, and must match every filled box.
  const [colF, setColF] = useState({ eko_tid: '', tracking_number: '', match_id: '', recon_status: '', side: '', amount: '', bank_description: '', csp: '' })
  // Recon ID filter goes to the server (so it returns the whole match across pages);
  // debounce the keystrokes into reconIdQuery.
  const [reconIdQuery, setReconIdQuery] = useState('')
  useEffect(() => {
    const t = setTimeout(() => setReconIdQuery((colF.match_id || '').trim()), 350)
    return () => clearTimeout(t)
  }, [colF.match_id])
  const _kw = (v) => (v || '').split(/[\s,]+/).map(s => s.trim().toLowerCase()).filter(Boolean)
  const _hit = (cell, q) => { const ks = _kw(q); return !ks.length || ks.some(k => String(cell ?? '').toLowerCase().includes(k)) }
  const colFActive = Object.values(colF).some(v => (v || '').trim())

  const toggleRow = (id) => setSelected(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const toggleAll = () => setSelected(s => s.size === rows.length ? new Set() : new Set(rows.map(r => r.id)))
  const clearSel  = () => setSelected(new Set())

  const STATUS_OPTS = CORE_RECON_STATUS_OPTS

  // date params for the /reports/* exports (they accept recon_date OR from/to)
  const dateParams = () => dateMode === 'single'
    ? { recon_date: date }
    : { from_date: fromDate, to_date: toDate }

  // date params for the /workflow/* endpoints (they take date_from / date_to only)
  const workflowDateParams = () => dateMode === 'single'
    ? { date_from: date, date_to: date }
    : { date_from: fromDate, date_to: toDate }

  const fetchRows = () => {
    setLoading(true)
    api.get('/recon/open-items', { params: {
      partner,
      ...(dateMode === 'single' ? { recon_date: date } : { date_from: fromDate, date_to: toDate }),
      recon_status: status === '' ? undefined : status,
      side: side || undefined,
      assigned: assignedFilter || undefined,
      // Recon ID is looked up server-side so BOTH sides of a match load even when
      // the counterpart sits on another page (the other column filters stay client-side).
      match_id: reconIdQuery || undefined,
      page, page_size: PAGE_SIZE,
    } })
      .then(({ data }) => { setRows(data.items || []); setTotal(data.total || 0) })
      .catch(() => { setRows([]); setTotal(0) })
      .finally(() => setLoading(false))
  }
  useEffect(() => { setPage(1) }, [partner, dateMode, date, fromDate, toDate, status, side, assignedFilter, reconIdQuery])
  useEffect(() => { fetchRows() }, [partner, dateMode, date, fromDate, toDate, status, side, assignedFilter, reconIdQuery, page])

  const runRecon = async () => {
    setRunning(true); setResult(null)
    // "All Banks" (partner === slug) runs recon for each bank in turn, then aggregates.
    const targets = (partner === slug && partners.length > 1) ? partners : [partner]
    try {
      const agg = { matched: 0, unmatched_bank: 0, unmatched_internal: 0 }
      for (const p of targets) {
        const { data } = dateMode === 'single'
          ? (await api.post('/recon/run', { partner: p, recon_date: date }))
          : (await api.post('/recon/run-range', { partner: p, date_from: fromDate, date_to: toDate }))
        agg.matched            += data.matched || data.total_matched || 0
        agg.unmatched_bank     += data.unmatched_bank || 0
        agg.unmatched_internal += data.unmatched_internal || 0
      }
      setResult(agg)
      const tot = agg.matched + agg.unmatched_bank
      const rate = tot > 0 ? Math.round(agg.matched / tot * 100) : 0
      toast.success(`Reconciled ${targets.length > 1 ? targets.length + ' banks' : ''} — ${agg.matched} matched, ${agg.unmatched_bank} open (${rate}%)`)
      fetchRows()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Recon failed')
    } finally { setRunning(false) }
  }

  const runExtra = async (path, label) => {
    setBusy(true)
    try {
      const body = path === '/recon/run-interbank-match' ? {} : { partner, recon_date: date }
      const { data } = await api.post(path, body)
      toast.success(`${label}: ${JSON.stringify(data).slice(0, 80)}`)
      fetchRows()
    } catch (e) { toast.error(e.response?.data?.detail || `${label} failed`) }
    finally { setBusy(false) }
  }

  const doExport = async (kind) => {
    const p = new URLSearchParams({ partner })
    // EOD is an as-of-date report: it requires recon_date even in range mode
    const dp = kind === 'eod'
      ? { recon_date: dateMode === 'single' ? date : toDate }
      : dateParams()
    Object.entries(dp).forEach(([k, v]) => { if (v) p.append(k, v) })
    const MAP = {
      summary:    { url: '/reports/summary/export',     file: `summary_${partner}.xlsx` },
      open:       { url: '/reports/open-items/export',  file: `open_items_${partner}.xlsx` },
      ageing:     { url: '/reports/ageing/export',      file: `ageing_${partner}.xlsx` },
      matched:    { url: '/reports/export-matched',     file: `matched_${partner}.xlsx` },
      eod:        { url: '/reports/eod-summary/export', file: `eod_${partner}.xlsx` },
    }
    const m = MAP[kind]
    setBusy(true)
    try {
      const resp = await api.get(`${m.url}?${p}`, { responseType: 'blob' })
      downloadBlob(resp.data, m.file)
    } catch (e) { toast.error('Export failed — check date/partner') }
    finally { setBusy(false) }
  }

  // ── Manual actions — branded modal instead of window.prompt (audit F2) ─────
  const [modal, setModal] = useState(null)        // { config, action }
  const [modalBusy, setModalBusy] = useState(false)

  const overrideRow = (row) => setModal({
    config: {
      title: 'Override status', danger: true, confirmLabel: 'Override',
      description: `Transaction ${row.eko_tid || row.id} — current status: ${row.recon_status}`,
      fields: [
        { name: 'status', label: 'New status', type: 'select', options: OVERRIDE_TARGETS, required: true, default: 'human_override' },
        { name: 'remark', label: 'Reason', required: true, minLength: 10, placeholder: 'Why is this status being overridden? (min 10 chars)' },
      ],
    },
    action: async (v) => {
      const { data } = await api.post('/recon/override-status', { transaction_id: row.id, new_status: v.status, remark: v.remark })
      if (data.queued) toast(data.message, { icon: '🕐' })
      else toast.success('Status overridden')
    },
  })
  const assignSrcRow = (row) => setModal({
    config: {
      title: row.src_code ? 'Edit SRC code' : 'Assign SRC code', confirmLabel: 'Apply',
      description: `Transaction ${row.eko_tid || row.id}${row.src_code ? ` — SRC: ${row.src_code} · pick "No SRC" to remove` : ''}`,
      fields: [
        // "No SRC" (only when already tagged) removes the tag — the row reverts to its prior state.
        { name: 'code', label: 'SRC code', type: 'select',
          options: [...((row.src_code || row.recon_status === 'src_assigned') ? [{ value: '__none__', label: 'No SRC (remove tag)' }] : []), ...srcCodes],
          required: true, default: row.src_code || (srcCodes[0] || '') },
        { name: 'note', label: 'Note', placeholder: 'optional', default: row.src_note || '' },
      ],
    },
    action: async (v) => {
      if (v.code === '__none__') {
        await api.post('/recon/remove-src', { transaction_id: row.id }); toast.success('SRC removed')
      } else {
        await api.post('/recon/assign-src', { transaction_id: row.id, src_code: v.code, src_note: v.note || '' })
        toast.success('SRC assigned')
      }
    },
  })
  const bulkSrc = () => {
    const ids = [...selected]; if (!ids.length) return
    setModal({
      config: {
        title: `Bulk SRC — ${ids.length} rows`, confirmLabel: 'Apply to all',
        description: 'Pick a code to tag every selected row, or "No SRC" to remove the tag from each.',
        fields: [
          { name: 'code', label: 'SRC code', type: 'select',
            options: [{ value: '__none__', label: 'No SRC (remove tag)' }, ...srcCodes],
            required: true, default: srcCodes[0] || '' },
          { name: 'note', label: 'Note', placeholder: 'optional' },
        ],
      },
      action: async (v) => {
        if (v.code === '__none__') {
          const { data } = await api.post('/recon/remove-src-bulk', { transaction_ids: ids })
          toast.success(`SRC removed from ${data.reverted ?? ids.length} rows`); clearSel()
        } else {
          const { data } = await api.post('/recon/assign-src-bulk', { transaction_ids: ids, src_code: v.code, src_note: v.note || '' })
          toast.success(`SRC assigned to ${data.updated ?? ids.length} rows`); clearSel()
        }
      },
    })
  }
  const removeSrcRow = async (row) => {
    if (!window.confirm(`Remove SRC from ${row.eko_tid || row.id}?\n\nIt reverts to the status it held before the tag.`)) return
    try {
      await api.post('/recon/remove-src', { transaction_id: row.id })
      toast.success('SRC removed'); fetchRows()
    } catch (e) { toast.error(e.response?.data?.detail || 'Remove failed') }
  }
  const bulkRemoveSrc = async () => {
    const ids = [...selected]; if (!ids.length) return
    if (!window.confirm(`Remove SRC from ${ids.length} selected row(s)?\n\nEach reverts to its status before the tag.`)) return
    try {
      const { data } = await api.post('/recon/remove-src-bulk', { transaction_ids: ids })
      toast.success(`SRC removed from ${data.reverted ?? ids.length} rows`); clearSel(); fetchRows()
    } catch (e) { toast.error(e.response?.data?.detail || 'Bulk remove failed') }
  }
  const bulkDelete = async () => {
    const ids = [...selected]; if (!ids.length) return
    if (!window.confirm(`Delete ${ids.length} selected row(s)?\n\nMatched counterparts are safely reverted to unmatched, and the rows go to the Recycle Bin (restorable for 30 days).`)) return
    try {
      const fd = new FormData()
      fd.append('module', 'core'); fd.append('table', 'txn'); fd.append('row_ids', ids.join(','))
      const { data } = await api.post('/upload/delete-rows', fd)
      toast.success(`Deleted ${data.deleted} row(s)${data.counterparts_unmatched ? ` · ${data.counterparts_unmatched} counterpart(s) reverted` : ''} — recoverable from the Recycle Bin`, { duration: 6000 })
      clearSel(); fetchRows()
    } catch (e) { toast.error(e.response?.data?.detail || 'Delete failed (needs the Clear/Delete Data permission)') }
  }
  const bulkOverride = () => {
    const ids = [...selected]; if (!ids.length) return
    setModal({
      config: {
        title: `Bulk override — ${ids.length} rows`, danger: true, confirmLabel: 'Override all',
        fields: [
          { name: 'status', label: 'New status', type: 'select', options: OVERRIDE_TARGETS, required: true, default: 'human_override' },
          { name: 'remark', label: 'Reason', required: true, minLength: 10, placeholder: 'One reason applies to all selected rows' },
        ],
      },
      action: async (v) => {
        const { data } = await api.post('/recon/bulk-override-status', { transaction_ids: ids, new_status: v.status, remark: v.remark })
        if (data.queued) toast(data.message, { icon: '🕐' })
        else toast.success(`Overrode ${data.updated ?? ids.length} rows`)
        clearSel()
      },
    })
  }
  const runModalAction = async (values) => {
    if (!modal) return
    setModalBusy(true)
    try { await modal.action(values); setModal(null); fetchRows() }
    catch (e) { toast.error(e.response?.data?.detail || 'Action failed') }
    finally { setModalBusy(false) }
  }

  // ── Exception workflow: owners + reason codes (Tier 1) ─────────────────────
  const [assignees, setAssignees] = useState([])
  const [reasons, setReasons] = useState([])
  useEffect(() => {
    api.get('/workflow/assignees')
      .then(({ data }) => { setAssignees(data.assignees || []); setReasons(data.reasons || []) })
      .catch(() => {})
  }, [])
  const assignRow = (row) => setModal({
    config: {
      title: 'Assign owner', confirmLabel: 'Assign',
      description: `Item ${row.eko_tid || row.id}${row.assigned_to ? ` — currently @${row.assigned_to}` : ' — currently unassigned'}`,
      fields: [
        { name: 'user', label: 'Owner', type: 'select', required: true, options: assignees, default: row.assigned_to || undefined },
        { name: 'reason', label: 'Reason code', type: 'select', options: ['', ...reasons], default: row.exception_reason || '' },
      ],
    },
    action: async (v) => {
      await api.post('/workflow/assign', { transaction_ids: [row.id], assigned_to: v.user, reason: v.reason || null })
      toast.success(`Assigned to ${v.user}`)
    },
  })
  const bulkAssign = () => {
    const ids = [...selected]; if (!ids.length) return
    setModal({
      config: {
        title: `Assign ${ids.length} item${ids.length > 1 ? 's' : ''} to an owner`,
        confirmLabel: 'Assign',
        fields: [
          { name: 'user', label: 'Owner', type: 'select', required: true, options: assignees },
          { name: 'reason', label: 'Reason code', type: 'select', options: ['', ...reasons] },
        ],
      },
      action: async (v) => {
        const { data } = await api.post('/workflow/assign', { transaction_ids: ids, assigned_to: v.user, reason: v.reason || null })
        toast.success(`Assigned ${data.updated} items to ${v.user}`); clearSel()
      },
    })
  }

  // ── Suggested matches (Tier 1) ──────────────────────────────────────────────
  const [suggestions, setSuggestions] = useState(null)   // null = hidden
  const [sugLoading, setSugLoading] = useState(false)
  const [aiLoading, setAiLoading] = useState(false)
  const loadSuggestions = () => {
    setSugLoading(true)
    api.get('/workflow/suggested-matches', { params: { partner, ...workflowDateParams() } })
      .then(({ data }) => setSuggestions(data))
      .catch((e) => toast.error(e.response?.data?.detail || 'Could not load suggestions'))
      .finally(() => setSugLoading(false))
  }
  // AI-assisted suggestions — de-identified payload, human-approved, tagged "AI".
  // Merges into the same window as the System (heuristic) suggestions.
  const loadAiSuggestions = () => {
    setAiLoading(true)
    api.get('/workflow/suggested-matches-ai', { params: { partner, ...workflowDateParams() } })
      .then(({ data }) => {
        if (data.error) toast(data.error, { icon: '🤖' })
        else if ((data.suggestions || []).length === 0) toast('AI found no new confident pairs', { icon: '🤖' })
        else toast.success(`AI suggested ${data.suggestions.length} match${data.suggestions.length === 1 ? '' : 'es'}`)
        if (data.note) toast(data.note, { icon: 'ℹ️' })
        setSuggestions(prev => {
          const existing = prev?.suggestions || []
          const seen = new Set(existing.map(s => `${s.bank.id}|${s.internal.id}`))
          const add = (data.suggestions || []).filter(s => !seen.has(`${s.bank.id}|${s.internal.id}`))
          return {
            bank_unmatched: data.bank_unmatched ?? prev?.bank_unmatched ?? 0,
            internal_unmatched: data.internal_unmatched ?? prev?.internal_unmatched ?? 0,
            suggestions: [...existing, ...add].sort((a, b) => b.score - a.score),
          }
        })
      })
      .catch((e) => toast.error(e.response?.data?.detail || 'AI suggestions failed'))
      .finally(() => setAiLoading(false))
  }
  const confirmSuggestion = async (s) => {
    try {
      const { data } = await api.post('/recon/manual-match', { bank_txn_id: s.bank.id, internal_txn_id: s.internal.id })
      if (data.queued) toast(data.message, { icon: '🕐' })
      else toast.success('Matched')
      setSuggestions(prev => prev ? { ...prev, suggestions: prev.suggestions.filter(x => x !== s) } : prev)
      fetchRows()
    } catch (e) { toast.error(e.response?.data?.detail || 'Match failed') }
  }

  // ── Fee verification (Tier 1) ───────────────────────────────────────────────
  const [feeResult, setFeeResult] = useState(null)
  const [feeLoading, setFeeLoading] = useState(false)
  const verifyFees = () => {
    setFeeLoading(true)
    api.get('/workflow/verify-fees', { params: { partner, ...workflowDateParams() } })
      .then(({ data }) => setFeeResult(data))
      .catch((e) => toast.error(e.response?.data?.detail || 'Fee verification failed'))
      .finally(() => setFeeLoading(false))
  }

  const rate = result ? (() => {
    const tot = (result.matched || 0) + (result.unmatched_bank || 0)
    return tot > 0 ? Math.round((result.matched || 0) / tot * 100) : 0
  })() : null

  return (
    <div className="max-w-6xl">
      {/* Controls */}
      <div className="card mb-4">
        <div className="flex items-end gap-3 flex-wrap">
          {partners.length > 1 && (
            <div>
              <label className="block text-xs text-gray-500 mb-1">Bank / Partner</label>
              <select className="select text-sm py-1.5" value={partner} onChange={e => setPartner(e.target.value)}>
                {partners.length > 1 && <option value={slug}>All Banks (consolidated)</option>}
                {partners.map(p => <option key={p} value={p}>{PARTNER_LABEL[p] || p}</option>)}
              </select>
            </div>
          )}
          <div>
            <label className="block text-xs text-gray-500 mb-1">Date mode</label>
            <div className="flex gap-1">
              {['single', 'range'].map(m => (
                <button key={m} onClick={() => setDateMode(m)}
                  className={`px-3 py-1.5 rounded text-xs font-medium ${dateMode === m ? 'bg-primary text-white' : 'bg-gray-100 text-gray-600'}`}>
                  {m === 'single' ? 'Single date' : 'Date range'}
                </button>
              ))}
            </div>
          </div>
          {dateMode === 'single' ? (
            <div>
              <label className="block text-xs text-gray-500 mb-1">Recon date</label>
              <input type="date" className="select text-sm py-1.5" value={date} onChange={e => setDate(e.target.value)} />
            </div>
          ) : (
            <>
              <div>
                <label className="block text-xs text-gray-500 mb-1">From</label>
                <input type="date" className="select text-sm py-1.5" value={fromDate} onChange={e => setFromDate(e.target.value)} />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">To</label>
                <input type="date" className="select text-sm py-1.5" value={toDate} onChange={e => setToDate(e.target.value)} />
              </div>
            </>
          )}
          <button onClick={runRecon} disabled={running} className="btn-primary flex items-center gap-2">
            <Play size={15} /> {running ? 'Running…' : 'Run Reconciliation'}
          </button>
          <button onClick={fetchRows} className="btn-ghost flex items-center gap-1.5"><RefreshCw size={14} /> Refresh</button>
        </div>

        {/* Secondary matching passes + Tier-1 tools */}
        <div className="flex items-center gap-2 mt-3 flex-wrap border-t border-gray-100 pt-3">
          <span className="text-xs text-gray-400">More matching:</span>
          <button onClick={() => runExtra('/recon/run-internal-match', 'Internal match')} disabled={busy} className="btn-ghost text-xs flex items-center gap-1"><Layers size={12} /> Internal Match</button>
          <button onClick={() => runExtra('/recon/run-interbank-match', 'Interbank match')} disabled={busy} className="btn-ghost text-xs flex items-center gap-1"><ArrowLeftRight size={12} /> Interbank Match</button>
          <span className="text-gray-200 mx-1">|</span>
          <button onClick={loadSuggestions} disabled={sugLoading} className="btn-ghost text-xs flex items-center gap-1 text-indigo-600 hover:border-indigo-200">
            ✨ {sugLoading ? 'Finding…' : 'Suggested Matches'}
          </button>
          <button onClick={loadAiSuggestions} disabled={aiLoading} className="btn-ghost text-xs flex items-center gap-1 text-violet-600 hover:border-violet-200">
            🤖 {aiLoading ? 'Thinking…' : 'AI Suggestions'}
          </button>
          <button onClick={verifyFees} disabled={feeLoading} className="btn-ghost text-xs flex items-center gap-1 text-emerald-700 hover:border-emerald-200">
            % {feeLoading ? 'Checking…' : 'Verify Fees'}
          </button>
        </div>
      </div>

      {/* ── Suggested matches panel ── */}
      {suggestions && (
        <div className="card mb-4 border-indigo-100">
          <div className="flex items-center justify-between mb-2">
            <h4 className="font-semibold text-gray-800 text-sm">✨ Suggested matches
              <span className="text-xs text-gray-400 font-normal ml-2">
                {suggestions.suggestions.length} candidates · {suggestions.bank_unmatched} bank / {suggestions.internal_unmatched} internal unmatched
              </span>
            </h4>
            <button onClick={() => setSuggestions(null)} className="text-xs text-gray-400 hover:text-gray-600">✕ close</button>
          </div>
          {suggestions.suggestions.length === 0 ? (
            <p className="text-xs text-gray-400 py-2">No confident candidates found for this range.</p>
          ) : (
            <div className="space-y-1.5 max-h-72 overflow-y-auto">
              {suggestions.suggestions.map((s, i) => (
                <div key={i} className="flex items-center gap-3 text-xs border border-gray-100 rounded-lg px-3 py-2 flex-wrap">
                  <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${s.source === 'ai' ? 'bg-violet-100 text-violet-700' : 'bg-slate-100 text-slate-600'}`} title={s.source === 'ai' ? 'Suggested by AI (de-identified) — confirm before matching' : 'Suggested by the rule engine'}>{s.source === 'ai' ? '🤖 AI' : 'System'}</span>
                  <span className={`font-bold px-2 py-0.5 rounded-full ${s.score >= 90 ? 'bg-green-50 text-green-700' : s.score >= 70 ? 'bg-amber-50 text-amber-700' : 'bg-gray-100 text-gray-600'}`}>{s.score}</span>
                  <span className="font-mono">{s.bank.eko_tid || s.bank.tracking_number || s.bank.id.slice(0, 8)}</span>
                  <span className="text-gray-300">↔</span>
                  <span className="font-mono">{s.internal.eko_tid || s.internal.tracking_number || s.internal.id.slice(0, 8)}</span>
                  <span className="tabular-nums text-gray-700">{inr(s.bank.amount)}</span>
                  <span className="text-gray-400">{s.why.join(' · ')}</span>
                  <button onClick={() => confirmSuggestion(s)} className="ml-auto px-3 py-1 rounded text-xs font-medium bg-indigo-600 text-white hover:bg-indigo-700">Confirm match</button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── Fee verification panel ── */}
      {feeResult && (
        <div className="card mb-4 border-emerald-100">
          <div className="flex items-center justify-between mb-2">
            <h4 className="font-semibold text-gray-800 text-sm">% Fee verification
              <span className="text-xs text-gray-400 font-normal ml-2">{feeResult.rule.label}</span>
            </h4>
            <button onClick={() => setFeeResult(null)} className="text-xs text-gray-400 hover:text-gray-600">✕ close</button>
          </div>
          <div className="flex items-center gap-5 text-xs mb-2 flex-wrap">
            <span>Checked: <strong>{feeResult.checked}</strong></span>
            <span className="text-green-600">OK: <strong>{feeResult.ok}</strong></span>
            <span className={feeResult.mismatch_count > 0 ? 'text-red-500' : 'text-gray-400'}>Mismatches: <strong>{feeResult.mismatch_count}</strong></span>
            <span>Fee expected: <strong>{inr(feeResult.total_fee_expected)}</strong></span>
            <span>Fee charged: <strong>{inr(feeResult.total_fee_charged)}</strong></span>
            <span className={Math.abs(feeResult.overcharge) > 1 ? 'text-red-600 font-semibold' : 'text-gray-500'}>
              {feeResult.overcharge > 0 ? 'Overcharged' : 'Undercharged'}: {inr(Math.abs(feeResult.overcharge))}
            </span>
          </div>
          {feeResult.mismatches.length > 0 && (
            <div className="overflow-x-auto max-h-56 overflow-y-auto"><table className="w-full text-xs">
              <thead><tr className="border-b text-gray-500">
                <th className="table-th">Eko TID</th><th className="table-th">Date</th><th className="table-th text-right">Amount</th>
                <th className="table-th text-right">Net (actual)</th><th className="table-th text-right">Net (expected)</th><th className="table-th text-right">Diff</th>
              </tr></thead>
              <tbody>{feeResult.mismatches.map((m, i) => (
                <tr key={i} className="border-b border-gray-50">
                  <td className="table-td font-mono">{m.eko_tid || m.id.slice(0, 8)}</td>
                  <td className="table-td font-mono">{m.recon_date}</td>
                  <td className="table-td text-right tabular-nums">{inr(m.amount)}</td>
                  <td className="table-td text-right tabular-nums">{inr(m.net_amount)}</td>
                  <td className="table-td text-right tabular-nums">{inr(m.expected_net)}</td>
                  <td className={`table-td text-right tabular-nums font-semibold ${m.diff < 0 ? 'text-red-600' : 'text-amber-600'}`}>{inr(m.diff)}</td>
                </tr>
              ))}</tbody>
            </table></div>
          )}
        </div>
      )}

      {/* Result summary */}
      {result && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
          {[
            { l: 'Matched', v: result.matched || 0, c: 'text-green-600', i: CheckCircle2 },
            { l: 'Open (bank)', v: result.unmatched_bank || 0, c: 'text-red-500', i: AlertTriangle },
            { l: 'Open (internal)', v: result.unmatched_internal || 0, c: 'text-amber-600', i: AlertTriangle },
            { l: 'Match rate', v: rate != null ? rate + '%' : '—', c: rate >= 95 ? 'text-green-600' : rate >= 85 ? 'text-amber-600' : 'text-red-500', i: FileSpreadsheet },
          ].map(({ l, v, c, i: I }) => (
            <div key={l} className="bg-white rounded-lg border border-gray-100 p-3"><I size={15} className={c} /><div className={`text-lg font-bold mt-1 ${c}`}>{v}</div><div className="text-[11px] text-gray-500">{l}</div></div>
          ))}
        </div>
      )}

      {/* Export bar */}
      <div className="card mb-4">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs font-medium text-gray-500 flex items-center gap-1"><Download size={13} /> Export ({dateMode === 'single' ? date : `${fromDate} → ${toDate}`}):</span>
          {[
            { k: 'summary', l: 'Summary' },
            { k: 'open',    l: 'Open Items' },
            { k: 'ageing',  l: 'Ageing' },
            { k: 'matched', l: 'Matched Pairs' },
            { k: 'eod',     l: 'EOD' },
          ].map(b => (
            <button key={b.k} onClick={() => doExport(b.k)} disabled={busy}
              className="px-3 py-1 rounded text-xs font-medium bg-gray-100 text-gray-700 hover:bg-primary hover:text-white transition-colors">
              {b.l}
            </button>
          ))}
          <Link to="/reports" className="text-xs text-primary flex items-center gap-1 ml-auto">All reports <ExternalLink size={11} /></Link>
        </div>
      </div>

      {/* Items table */}
      <div className="card">
        {selected.size > 0 && (
          <div className="flex items-center gap-3 mb-3 p-2 rounded-lg bg-primary/5 border border-primary/20">
            <span className="text-xs font-medium text-primary">{selected.size} selected</span>
            <button onClick={bulkAssign} className="px-3 py-1 rounded text-xs font-medium bg-indigo-600 text-white hover:bg-indigo-700">Assign owner</button>
            <button onClick={bulkSrc} className="px-3 py-1 rounded text-xs font-medium bg-blue-600 text-white hover:bg-blue-700">Bulk assign SRC</button>
            <button onClick={bulkRemoveSrc} className="px-3 py-1 rounded text-xs font-medium bg-gray-100 text-gray-600 hover:bg-gray-200 border border-gray-200">Bulk remove SRC</button>
            <button onClick={bulkOverride} className="px-3 py-1 rounded text-xs font-medium bg-amber-600 text-white hover:bg-amber-700">Bulk override status</button>
            <button onClick={bulkDelete} className="px-3 py-1 rounded text-xs font-medium bg-red-600 text-white hover:bg-red-700">Delete selected</button>
            <button onClick={clearSel} className="text-xs text-gray-500 hover:text-gray-700 ml-auto">Clear</button>
          </div>
        )}
        <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
          <div className="flex items-center gap-2">
            <select className="select text-xs py-1 w-40" value={status} onChange={e => setStatus(e.target.value)}>
              {STATUS_OPTS.map(o => <option key={o.v} value={o.v}>{o.l}</option>)}
            </select>
            <select className="select text-xs py-1 w-28" value={side} onChange={e => setSide(e.target.value)}>
              <option value="">Both sides</option>
              <option value="bank">Bank</option>
              <option value="internal">Internal</option>
            </select>
            <select className="select text-xs py-1 w-32" value={assignedFilter} onChange={e => setAssignedFilter(e.target.value)}>
              <option value="">Any owner</option>
              <option value="me">Assigned to me</option>
              <option value="unassigned">Unassigned</option>
              {assignees.map(a => <option key={a} value={a}>@{a}</option>)}
            </select>
            <span className="text-xs text-gray-400">{total.toLocaleString()} rows</span>
          </div>
          <div className="flex gap-3 text-xs">
            <Link to={`/open-items?partner=${partner}`} className="text-primary flex items-center gap-1">Open Items <ExternalLink size={11} /></Link>
            <Link to="/mismatches" className="text-primary flex items-center gap-1">Mismatches <ExternalLink size={11} /></Link>
            <Link to="/manual-match" className="text-primary flex items-center gap-1">Manual Match <ExternalLink size={11} /></Link>
          </div>
        </div>
        {(() => { const visibleRows = rows.filter(r =>
          _hit(r.side, colF.side) && _hit(r.eko_tid, colF.eko_tid) && _hit(r.tracking_number, colF.tracking_number) &&
          _hit(r.amount, colF.amount) && _hit(r.match_id, colF.match_id) && _hit(r.recon_status, colF.recon_status) &&
          _hit(r.bank_description, colF.bank_description) &&
          (_hit(r.csp_code, colF.csp) || _hit(r.csp_name, colF.csp))); return (
        loading ? <p className="text-xs text-gray-400 py-4 text-center">Loading…</p> : (
          <div className="overflow-x-auto"><table className="w-full text-xs">
            <thead>
            <tr className="border-b text-gray-500">
              <th className="table-th w-8"><input type="checkbox" checked={visibleRows.length > 0 && visibleRows.every(r => selected.has(r.id))} onChange={() => setSelected(s => { const all = visibleRows.every(r => s.has(r.id)); const n = new Set(s); visibleRows.forEach(r => all ? n.delete(r.id) : n.add(r.id)); return n })} /></th>
              <th className="table-th">Side</th><th className="table-th">Eko TID</th><th className="table-th">Tracking No</th>
              <th className="table-th text-right">Amount</th><th className="table-th">Txn Date</th><th className="table-th">Recon ID</th><th className="table-th">Status</th><th className="table-th">Description</th><th className="table-th">CSP</th><th className="table-th">Actions</th>
            </tr>
            <tr className="border-b border-gray-100 bg-gray-50/40">
              <th className="px-1 py-1"></th>
              <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="Bank/Internal…" value={colF.side} onChange={e => setColF(f => ({ ...f, side: e.target.value }))} /></th>
              <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="Eko TID…" value={colF.eko_tid} onChange={e => setColF(f => ({ ...f, eko_tid: e.target.value }))} /></th>
              <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="Tracking No…" value={colF.tracking_number} onChange={e => setColF(f => ({ ...f, tracking_number: e.target.value }))} /></th>
              <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="amt…" value={colF.amount} onChange={e => setColF(f => ({ ...f, amount: e.target.value }))} /></th>
              <th className="px-1 py-1"></th>
              <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="recon id…" value={colF.match_id} onChange={e => setColF(f => ({ ...f, match_id: e.target.value }))} /></th>
              <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="status…" value={colF.recon_status} onChange={e => setColF(f => ({ ...f, recon_status: e.target.value }))} /></th>
              <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="narration…" value={colF.bank_description} onChange={e => setColF(f => ({ ...f, bank_description: e.target.value }))} /></th>
              <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="CSP code/name…" value={colF.csp} onChange={e => setColF(f => ({ ...f, csp: e.target.value }))} /></th>
              <th className="px-1 py-1">{colFActive && <button onClick={() => setColF({ eko_tid: '', tracking_number: '', match_id: '', recon_status: '', side: '', amount: '', bank_description: '', csp: '' })} className="text-[10px] text-gray-400 hover:text-gray-600">clear</button>}</th>
            </tr>
            </thead>
            <tbody>{visibleRows.length === 0 ? <tr><td colSpan="11" className="text-center text-gray-400 py-4">{rows.length ? 'No rows match the column filters.' : 'No rows — upload data and run reconciliation.'}</td></tr> : visibleRows.map((r, i) => (
              <tr key={i} className={`border-b border-gray-50 hover:bg-gray-50 ${selected.has(r.id) ? 'bg-primary/5' : ''}`}>
                <td className="table-td"><input type="checkbox" checked={selected.has(r.id)} onChange={() => toggleRow(r.id)} /></td>
                <td className="table-td capitalize">{r.side}</td>
                <td className="table-td font-mono">{r.eko_tid || '—'}</td>
                <td className="table-td font-mono">{r.tracking_number || '—'}</td>
                <td className="table-td text-right tabular-nums">{inr(r.amount)}</td>
                <td className="table-td font-mono">{r.transaction_date || r.recon_date || '—'}</td>
                <td className="table-td font-mono text-primary">{r.match_id || '—'}</td>
                <td className="table-td">
                  <span className={`px-2 py-0.5 rounded-full capitalize ${STATUS_COLOR[r.recon_status] || 'bg-gray-100 text-gray-600'}`}>{(r.recon_status || '').replace(/_/g, ' ')}</span>
                  {r.assigned_to && <div className="text-[10px] text-indigo-500 mt-0.5">@{r.assigned_to}{r.exception_reason ? ` · ${r.exception_reason}` : ''}</div>}
                </td>
                <td className="table-td max-w-[16rem]">
                  {r.side === 'bank' && r.bank_description
                    ? <span className="text-xs text-gray-600 block truncate" title={r.bank_description}>{r.bank_description}</span>
                    : <span className="text-gray-300">—</span>}
                </td>
                <td className="table-td max-w-[14rem]">
                  {r.side === 'internal' && (r.csp_code || r.csp_name)
                    ? <div className="leading-tight">
                        <span className="block font-mono text-gray-700 truncate" title={r.csp_code || ''}>{r.csp_code || '—'}</span>
                        {r.csp_name && <span className="block text-[10px] text-gray-500 truncate" title={r.csp_name}>{r.csp_name}</span>}
                      </div>
                    : <span className="text-gray-300">—</span>}
                </td>
                <td className="table-td"><div className="flex gap-1">
                  <button title="Assign owner" onClick={() => assignRow(r)} className="p-1 rounded hover:bg-indigo-50 text-indigo-600"><UserPlus size={12} /></button>
                  <button title="Override status" onClick={() => overrideRow(r)} className="p-1 rounded hover:bg-amber-50 text-amber-600"><Tag size={12} /></button>
                  <button title="Assign SRC" onClick={() => assignSrcRow(r)} className="p-1 rounded hover:bg-blue-50 text-blue-600"><GitMerge size={12} /></button>
                  {(r.recon_status === 'src_assigned' || r.src_code) &&
                    <button title="Remove SRC" onClick={() => removeSrcRow(r)} className="p-1 rounded hover:bg-gray-100 text-gray-500 text-[11px] font-bold leading-none">✕</button>}
                </div></td>
              </tr>
            ))}</tbody>
          </table></div>
        )) })()}
        {total > PAGE_SIZE && (
          <div className="flex items-center justify-between mt-3 text-xs">
            <button disabled={page <= 1} onClick={() => setPage(p => p - 1)}
              className="btn-ghost text-xs disabled:opacity-40">← Prev</button>
            <span className="text-gray-400">Page {page} of {Math.max(1, Math.ceil(total / PAGE_SIZE))} · {total.toLocaleString()} rows</span>
            <button disabled={page >= Math.ceil(total / PAGE_SIZE)} onClick={() => setPage(p => p + 1)}
              className="btn-ghost text-xs disabled:opacity-40">Next →</button>
          </div>
        )}
      </div>

      <ActionModal open={!!modal} config={modal?.config} busy={modalBusy}
        onClose={() => setModal(null)} onSubmit={runModalAction} />
    </div>
  )
}
