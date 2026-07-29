import React, { useState, useEffect, useCallback } from 'react'
import { Upload, Play, RefreshCw, X, ChevronLeft, ChevronRight, ArrowLeftRight, ChevronDown, Check, AlertTriangle, Download, Trash2, Tag } from 'lucide-react'
import api from '../utils/api'
import toast from 'react-hot-toast'
import { hasPermission } from '../utils/permissions'

// ── Money / date helpers ────────────────────────────────────────────────────────

function fmtINR(n) {
  if (n == null || isNaN(n)) return '₹0'
  const abs = Math.abs(Number(n))
  return `₹${abs.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}
function today() { return new Date().toISOString().split('T')[0] }

// A bank "Ref No./Cheque No." is frequently a placeholder like "- / -" (cash-deposit credits
// carry no 20-digit ref). Never show the placeholder — callers fall back to the description.
const _REF_PLACEHOLDER = new Set(['', '-', '- / -', '-/-', '- /-', '-/ -', 'n/a', 'na'])
function refOrBlank(s) { const t = (s || '').trim(); return _REF_PLACEHOLDER.has(t.toLowerCase()) ? '' : t }

// ── Plain-language status metadata, per process ─────────────────────────────────
// label = human wording the team reads; tone = colour; hint = hover tooltip that
// explains what the status actually means (incl. the common "file not uploaded" cases).
const META = {
  p01: {
    matched:   { label: 'Matched',   tone: 'green', hint: 'Every wallet withdrawal for this operator matched a bank settlement of the same amount, on its business date (even if the bank posted it a day or two later).' },
    unmatched: { label: 'Unmatched', tone: 'red',   hint: 'A withdrawal with no matching settlement (not settled yet, or the KO Limits file for that date isn’t uploaded), or a bank settlement with no withdrawal.' },
    // legacy statuses fold into the two-state model (for any pre-migration rows)
    CREDITED:  { label: 'Matched',   tone: 'green', hint: '' },
    PENDING:   { label: 'Unmatched', tone: 'red',   hint: '' },
    PARTIAL:   { label: 'Unmatched', tone: 'red',   hint: '' },
    EXCESS:    { label: 'Unmatched', tone: 'red',   hint: '' },
  },
  p02: {
    Matched:        { label: 'Matched',        tone: 'green',  hint: 'Bank line and transaction-report line agree on the 20-digit reference.' },
    Partial:        { label: 'Amount differs', tone: 'orange', hint: 'Reference matched but the bank and report amounts differ by more than ₹1.' },
    Unmatched:      { label: 'Unmatched',      tone: 'red',    hint: 'No transaction-report line carries this reference (report file may be missing for this date).' },
    Reversal:       { label: 'Reversal',       tone: 'purple', hint: 'The same reference appears as both a debit and a credit — the two legs are bracketed together.' },
    Manual_Matched: { label: 'Manual',         tone: 'amber',  hint: 'Resolved by hand; persists across re-runs.' },
  },
  p03: {
    Matched:             { label: 'Matched',        tone: 'green',  hint: 'Money paid out matches a bank credit on CSP + amount (within ±2 days).' },
    Unmatched_TxnReport: { label: 'No bank credit', tone: 'red',    hint: 'Money was paid OUT but no matching bank credit was found (checked ±2 days).' },
    Unmatched_Bank:      { label: 'No txn record',  tone: 'orange', hint: 'A bank credit came IN with no matching transaction-report entry.' },
    Manual_Matched:      { label: 'Manual',         tone: 'amber',  hint: 'Resolved by hand; persists across re-runs.' },
  },
  p04: {
    DEPOSIT:    { label: 'Deposit needed',  tone: 'red',    hint: 'Wallet is short of the expected balance — deposit the action amount in the SBI portal.' },
    WITHDRAWAL: { label: 'Withdraw needed', tone: 'amber',  hint: 'Wallet holds more than expected — withdraw the action amount in the SBI portal.' },
    NONE:       { label: 'Balanced',        tone: 'green',  hint: 'Wallet balance already matches the expected value — no action.' },
  },
}
const TONE = {
  green:  'bg-green-100 text-green-700',
  amber:  'bg-amber-100 text-amber-700',
  orange: 'bg-orange-100 text-orange-700',
  red:    'bg-red-100 text-red-600',
  purple: 'bg-purple-100 text-purple-700',
  gray:   'bg-gray-100 text-gray-600',
}
// Per-process accent (static class strings — Tailwind can't see interpolated names)
const ACCENT = {
  p01: { wrap: 'bg-blue-50 border-blue-100',     btn: '#1d4ed8' },
  p02: { wrap: 'bg-green-50 border-green-100',    btn: '#15803d' },
  p03: { wrap: 'bg-purple-50 border-purple-100',  btn: '#7c3aed' },
  p04: { wrap: 'bg-amber-50 border-amber-100',    btn: '#d97706' },
}

function StatusBadge({ process, status }) {
  const m = (META[process] || {})[status] || { label: status || '—', tone: 'gray', hint: '' }
  return <span title={m.hint} className={`text-[11px] px-2 py-0.5 rounded-full font-medium whitespace-nowrap ${TONE[m.tone]}`}>{m.label}</span>
}

function SrcBadge({ row }) {
  if (!row.src_code) return null
  return <span title={row.src_note || ''} className="ml-1 text-[10px] px-1.5 py-0.5 rounded-full font-semibold bg-yellow-100 text-yellow-800 align-middle">{row.src_code}</span>
}

// One side of a paired match: amount + supporting lines, or a greyed empty state.
function Side({ amount, accent = 'text-gray-800', lines = [], empty = false, emptyLabel = '— no match —' }) {
  if (empty) return <span className="text-[11px] text-gray-300 italic">{emptyLabel}</span>
  return (
    <div className="leading-tight">
      <div className={`font-semibold tabular-nums ${accent}`}>{amount != null ? fmtINR(amount) : '—'}</div>
      {lines.filter(Boolean).map((l, i) => <div key={i} className="text-[11px] text-gray-400 truncate max-w-[230px]" title={typeof l === 'string' ? l : ''}>{l}</div>)}
    </div>
  )
}

// Amount difference between the two sides of a pair.
function Delta({ a, b }) {
  if (a == null || b == null) return <span className="text-gray-300 text-xs">—</span>
  const d = (a || 0) - (b || 0)
  if (Math.abs(d) < 0.01) return <span className="text-green-600 text-xs tabular-nums">₹0</span>
  return <span className="text-red-500 font-semibold tabular-nums text-xs" title="Bank − Report">{d > 0 ? '+' : '−'}{fmtINR(d)}</span>
}

// Clickable status summary cards (also act as a status filter).
function SummaryCards({ process, summary, active, onPick, money }) {
  const entries = Object.entries(summary || {})
  if (!entries.length && !(money && money.length)) return null
  return (
    <div className="flex gap-2.5 mb-4 flex-wrap items-stretch">
      {entries.map(([k, v]) => {
        const isOn = active === k
        return (
          <button key={k} onClick={() => onPick(isOn ? '' : k)}
            className={`px-3 py-2 rounded-lg border bg-white text-center transition-all min-w-[96px] ${isOn ? 'border-gray-800 ring-1 ring-gray-300' : 'border-gray-200 hover:border-gray-300'}`}>
            <div className="text-lg font-bold text-gray-800 leading-none tabular-nums">{(v || 0).toLocaleString('en-IN')}</div>
            <div className="mt-1.5"><StatusBadge process={process} status={k} /></div>
          </button>
        )
      })}
      {money && money.length > 0 && (
        <div className="flex gap-2.5 ml-auto">
          {money.map(mo => (
            <div key={mo.label} className="px-3 py-2 rounded-lg border border-gray-100 bg-gray-50/60 text-right min-w-[110px]">
              <div className={`text-base font-bold leading-none tabular-nums ${mo.tone || 'text-gray-800'}`}>{mo.value}</div>
              <div className="text-[11px] text-gray-400 mt-1.5">{mo.label}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function Pager({ page, pageSize, total, onPage }) {
  const t = total || 0
  const pages = Math.max(1, Math.ceil(t / pageSize))
  if (t <= pageSize) return <div className="px-4 py-2.5 text-xs text-gray-400 text-center border-t border-gray-100">{t.toLocaleString('en-IN')} row{t === 1 ? '' : 's'}</div>
  return (
    <div className="flex items-center justify-between px-4 py-2.5 border-t border-gray-100 text-xs text-gray-500">
      <span className="tabular-nums">{((page - 1) * pageSize + 1).toLocaleString('en-IN')}–{Math.min(page * pageSize, t).toLocaleString('en-IN')} of {t.toLocaleString('en-IN')}</span>
      <div className="flex items-center gap-2">
        <button disabled={page <= 1} onClick={() => onPage(page - 1)} className="btn-ghost px-2 py-1 disabled:opacity-30"><ChevronLeft size={14} /></button>
        <span className="tabular-nums">Page {page} / {pages}</span>
        <button disabled={page >= pages} onClick={() => onPage(page + 1)} className="btn-ghost px-2 py-1 disabled:opacity-30"><ChevronRight size={14} /></button>
      </div>
    </div>
  )
}

// Process header — "Left ↔ Right" + one-line plain description.
function ProcessHeader({ process, left, right, desc }) {
  const a = ACCENT[process] || ACCENT.p01
  return (
    <div className={`card mb-4 p-3.5 border ${a.wrap}`}>
      <div className="flex items-center gap-2 text-sm font-semibold text-gray-800 flex-wrap">
        <span>{left}</span>
        <ArrowLeftRight size={15} className="text-gray-400 shrink-0" />
        <span>{right}</span>
      </div>
      <div className="text-xs text-gray-500 mt-1">{desc}</div>
    </div>
  )
}

// Default any SBI date picker to the LATEST day that actually has data (not today, which is
// usually empty — the source of "everything shows 0"). Falls back to today.
function useLatestSbiDate() {
  const [d, setD] = useState(today())
  useEffect(() => {
    let alive = true
    api.get('/sbi/readiness').then(({ data }) => { if (alive && data.latest) setD(data.latest) }).catch(() => {})
    return () => { alive = false }
  }, [])
  return [d, setD]
}

// Run all business dates at once — reconciles every day in the data (wipe-guarded, so a
// day missing its counterpart file is a harmless no-op). This is the safe, correct way to
// run; the per-process/per-date buttons below are for re-running a single day.
function RunAllDatesButton({ onDone }) {
  const [busy, setBusy] = useState(false)
  const run = async () => {
    setBusy(true)
    try {
      const { data } = await api.post('/sbi/run/all')   // no recon_date → all business dates
      toast.success(`Reconciled ${data.dates?.length || 0} date(s)`)
      onDone && onDone()
    } catch (e) { toast.error(e.response?.data?.detail || 'Run failed') }
    finally { setBusy(false) }
  }
  return (
    <button onClick={run} disabled={busy}
      className="btn-primary flex items-center gap-2 bg-rose-600 hover:bg-rose-700">
      <Play size={14} />{busy ? 'Reconciling all dates…' : 'Reconcile all dates'}
    </button>
  )
}

// Download the two operator reconciliation workbooks for the selected business date.
// These reproduce the files finance ops reconcile against by hand — bank statement fully
// annotated with the source product each row matched, plus the per-source match-status view.
function DownloadReports({ reconDate }) {
  const [busy, setBusy] = useState('')
  const dl = async (kind, path, label) => {
    setBusy(kind)
    try {
      const r = await api.get(path, { params: { recon_date: reconDate }, responseType: 'blob' })
      const used = r.headers['x-recon-date'] || reconDate || 'latest'
      const u = URL.createObjectURL(r.data)
      const a = document.createElement('a')
      a.href = u; a.download = `${label}_${used}.xlsx`; a.click(); URL.revokeObjectURL(u)
      toast.success(`Downloaded ${label.replace(/_/g, ' ')} for ${used}`)
    } catch (e) {
      toast.error(e.response?.status === 404 ? 'No SBI data to report on yet' : 'Download failed')
    } finally { setBusy('') }
  }
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <button onClick={() => dl('recon', '/sbi/reports/reconciliation', 'Reconciliation_Report')}
        disabled={!!busy} title="Bank statement annotated with the source product each row matched, + unmatched & duplicates"
        className="btn-ghost flex items-center gap-1.5 !px-3 !py-1.5 text-xs">
        <Download size={13} />{busy === 'recon' ? 'Building…' : 'Reconciliation Report'}
      </button>
      <button onClick={() => dl('src', '/sbi/reports/source-match', 'Source_Files_Match_Status')}
        disabled={!!busy} title="Per source file: matched / unmatched split by Success vs Failure"
        className="btn-ghost flex items-center gap-1.5 !px-3 !py-1.5 text-xs">
        <Download size={13} />{busy === 'src' ? 'Building…' : 'Source Match Report'}
      </button>
    </div>
  )
}

// Run + filter control row, shared shape.
function RunBar({ process, reconDate, setReconDate, onRun, running, children }) {
  const a = ACCENT[process] || ACCENT.p01
  return (
    <div className="flex items-end gap-3 mb-4 flex-wrap">
      <div>
        <label className="text-xs text-gray-400 block mb-1">Business date <span className="text-gray-300">(the day being reconciled)</span></label>
        <input type="date" className="input" value={reconDate} onChange={e => setReconDate(e.target.value)} />
      </div>
      <button onClick={onRun} disabled={running} className="btn-primary flex items-center gap-2" style={{ background: a.btn }}>
        <Play size={14} />{running ? 'Running…' : `Run ${process.toUpperCase()} for this day`}
      </button>
      <div className="w-px h-9 bg-gray-200 mx-1" />
      {children}
    </div>
  )
}

// Disposition reason codes for unmatched SBI rows — live catalog via useSrcCodes()
import { useSrcCodes } from '../srcCodes'
// Maker-checker: a queued action returns {queued:true, message}; toast "pending" not "done".
const mcQueued = (data) => {
  if (data?.queued) { toast(data.message || 'Pending approval by another user', { icon: '🕐' }); return true }
  return false
}

// ── Reusable modals ─────────────────────────────────────────────────────────────

function SrcModal({ process, procLabel, row, summary, onClose, onDone }) {
  const srcCodes = useSrcCodes()
  const [form, setForm] = useState({ src_code: '', src_note: '' })
  const [saving, setSaving] = useState(false)
  const submit = async () => {
    if (!form.src_code) { toast.error('Pick a reason code'); return }
    setSaving(true)
    try {
      await api.post('/sbi/assign-src', { process, result_id: row.id, src_code: form.src_code, src_note: form.src_note?.trim() || null })
      toast.success('SRC assigned'); onDone()
    } catch (e) { toast.error(e.response?.data?.detail || 'Assign SRC failed') }
    finally { setSaving(false) }
  }
  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-lg shadow-xl w-full max-w-md p-5" onClick={e => e.stopPropagation()}>
        <h3 className="font-semibold text-gray-800 mb-1">Assign SRC — {procLabel}</h3>
        <p className="text-xs text-gray-500 mb-3">{summary} Tags this unmatched row with a disposition reason for reporting. <strong>Persists across re-runs.</strong></p>
        <label className="text-xs text-gray-500 block mb-1">Reason code (required)</label>
        <select className="select w-full mb-3" value={form.src_code} onChange={e => setForm({ ...form, src_code: e.target.value })}>
          <option value="">Select a reason…</option>
          {srcCodes.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        <label className="text-xs text-gray-500 block mb-1">Note (optional)</label>
        <textarea className="input w-full mb-4" rows={3} value={form.src_note} onChange={e => setForm({ ...form, src_note: e.target.value })} placeholder="Optional context for this disposition" />
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="btn-ghost">Cancel</button>
          <button onClick={submit} disabled={saving} className="btn-primary">{saving ? 'Saving…' : 'Assign SRC'}</button>
        </div>
      </div>
    </div>
  )
}

function ManualMatchModal({ row, summary, onClose, onDone }) {
  const [form, setForm] = useState({ counterpart_ref: '', remark: '' })
  const [saving, setSaving] = useState(false)
  const submit = async () => {
    if ((form.remark || '').trim().length < 5) { toast.error('Remark (≥5 chars) required'); return }
    setSaving(true)
    try {
      const { data } = await api.post('/sbi/manual-match', { process: 'p02', result_id: row.id, counterpart_ref: form.counterpart_ref || null, remark: form.remark.trim() })
      if (!mcQueued(data)) toast.success('Manually matched'); onDone()
    } catch (e) { toast.error(e.response?.data?.detail || 'Manual match failed') }
    finally { setSaving(false) }
  }
  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-lg shadow-xl w-full max-w-md p-5" onClick={e => e.stopPropagation()}>
        <h3 className="font-semibold text-gray-800 mb-1">Manual match — P02 (Bank vs Transaction)</h3>
        <p className="text-xs text-gray-500 mb-3">{summary} Recorded with your remark and <strong>persists across re-runs</strong>.</p>
        <label className="text-xs text-gray-500 block mb-1">Counterpart reference (optional)</label>
        <input className="input w-full mb-3" value={form.counterpart_ref} onChange={e => setForm({ ...form, counterpart_ref: e.target.value })} placeholder="the transaction-report reference it matches" />
        <label className="text-xs text-gray-500 block mb-1">Remark (required, ≥5 chars)</label>
        <textarea className="input w-full mb-4" rows={3} value={form.remark} onChange={e => setForm({ ...form, remark: e.target.value })} placeholder="Why is this a match? (kept in the audit log)" />
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="btn-ghost">Cancel</button>
          <button onClick={submit} disabled={saving} className="btn-primary">{saving ? 'Saving…' : 'Confirm match'}</button>
        </div>
      </div>
    </div>
  )
}

// ── Upload card + status (unchanged behaviour) ──────────────────────────────────

function UploadCard({ label, desc, endpoint, extraFields = [], onDone, color = 'blue' }) {
  const [file, setFile] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [extra, setExtra] = useState({})
  const [dup, setDup] = useState('')   // 409 duplicate-file message; enables force re-upload
  const fileRef = React.useRef()
  const colorMap = {
    blue:   { border: 'border-blue-300',   bg: 'bg-blue-50',   btn: '#1d4ed8' },
    green:  { border: 'border-green-300',  bg: 'bg-green-50',  btn: '#15803d' },
    purple: { border: 'border-purple-300', bg: 'bg-purple-50', btn: '#7e22ce' },
    amber:  { border: 'border-amber-300',  bg: 'bg-amber-50',  btn: '#d97706' },
  }[color] || {}

  const handleUpload = async (force = false) => {
    if (!file) return toast.error('Select a file first')
    setUploading(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      Object.entries(extra).forEach(([k, v]) => { if (v) fd.append(k, v) })
      const url = force ? `${endpoint}${endpoint.includes('?') ? '&' : '?'}force=true` : endpoint
      const { data } = await api.post(url, fd)
      toast.success(`${label}: ${data.inserted} rows uploaded`)
      setFile(null); setDup('')
      if (onDone) onDone()
    } catch (err) {
      // 409 = this exact file was already uploaded. Don't clear the file — offer a
      // deliberate replace instead of silently doubling the data.
      if (err.response?.status === 409) {
        setDup(err.response?.data?.detail || 'This file was already uploaded.')
      } else {
        toast.error(err.response?.data?.detail || `${label} upload failed`)
      }
    } finally { setUploading(false) }
  }

  return (
    <div className={`rounded-xl border-2 ${colorMap.border || 'border-gray-200'} ${file ? colorMap.bg : 'bg-white'} p-4 transition-all`}>
      <div className="font-semibold text-gray-800 text-sm mb-1">{label}</div>
      <div className="text-xs text-gray-400 mb-3">{desc}</div>
      {extraFields.map(f => (
        <div key={f.name} className="mb-2">
          <label className="text-xs text-gray-500 block mb-1">{f.label}</label>
          <input type={f.type || 'text'} className="input text-xs" placeholder={f.placeholder}
            onChange={e => setExtra(x => ({ ...x, [f.name]: e.target.value }))} />
        </div>
      ))}
      <input ref={fileRef} type="file" accept=".xls,.xlsx,.csv" className="hidden"
        onChange={e => { if (e.target.files[0]) setFile(e.target.files[0]) }} />
      <div onClick={() => fileRef.current.click()}
        className="border-2 border-dashed border-gray-200 rounded-lg p-3 text-center cursor-pointer hover:border-gray-400 transition-colors mb-2">
        {file
          ? <div className="flex items-center justify-center gap-2 text-gray-600 text-xs">
              <span className="truncate max-w-[200px]">{file.name}</span>
              <button onClick={e => { e.stopPropagation(); setFile(null) }} className="text-gray-400 hover:text-red-500"><X size={12} /></button>
            </div>
          : <div className="text-gray-400 text-xs"><Upload size={14} className="mx-auto mb-1" />Click to select</div>
        }
      </div>
      <button onClick={() => handleUpload(false)} disabled={!file || uploading}
        className="btn-primary w-full text-sm" style={{ background: file ? colorMap.btn : undefined }}>
        {uploading ? 'Uploading…' : `Upload ${label} →`}
      </button>
      {dup && (
        <div className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-2.5 text-xs">
          <div className="text-amber-800">{dup}</div>
          <div className="flex items-center gap-2 mt-2">
            <button onClick={() => handleUpload(true)} disabled={uploading}
              className="px-2.5 py-1 rounded-md bg-amber-600 text-white font-medium hover:bg-amber-700 disabled:opacity-50">
              {uploading ? 'Replacing…' : 'Re-upload anyway (replace)'}
            </button>
            <button onClick={() => { setDup(''); setFile(null) }} className="text-gray-500 hover:text-gray-700">Cancel</button>
          </div>
          <div className="text-[11px] text-amber-600 mt-1.5">Only do this if you meant to re-apply the same file (e.g. restoring removed rows). It will not create duplicates for rows that already exist.</div>
        </div>
      )}
    </div>
  )
}

function UploadStatus() {
  const [status, setStatus] = useState(null)
  const [date, setDate] = useState(today())

  const load = async () => {
    try {
      const { data } = await api.get('/sbi/upload-status', { params: { upload_date: date } })
      setStatus(data)
    } catch { }
  }
  useEffect(() => { load() }, [date])

  const items = status ? [
    { label: 'Bank Statement', key: 'bank_statement', icon: '🏦' },
    { label: 'Transaction Reports', key: 'txn_reports', icon: '📋' },
    { label: 'KO Limits Config', key: 'ko_limits', icon: '⚙️' },
    { label: 'KO Cash Holding', key: 'ko_cash_holding', icon: '💰' },
    { label: 'Limit Failures', key: 'limit_failures', icon: '⚠️' },
    { label: 'CSP Master', key: 'csp_master', icon: '📊' },
  ] : []

  return (
    <div className="card mb-5">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-semibold text-gray-700 text-sm">Upload Status</h3>
        <div className="flex items-center gap-2">
          <input type="date" className="input text-xs w-36" value={date} onChange={e => setDate(e.target.value)} />
          <button onClick={load} className="btn-ghost flex items-center gap-1 text-xs"><RefreshCw size={12} />Refresh</button>
        </div>
      </div>
      <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
        {items.map(({ label, key, icon }) => (
          <div key={key} className={`text-center p-2 rounded-lg text-xs ${(status?.[key] || 0) > 0 ? 'bg-green-50 border border-green-200' : 'bg-gray-50 border border-gray-200'}`}>
            <div className="text-lg">{icon}</div>
            <div className={`font-bold ${(status?.[key] || 0) > 0 ? 'text-green-700' : 'text-gray-400'}`}>{(status?.[key] || 0).toLocaleString()}</div>
            <div className="text-gray-500 leading-tight">{label}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── P01 — Wallet Withdrawals ↔ Bank Settlements (per KO) ─────────────────────────

// P02-style line drill for P01: per KO, the individual withdrawal lines (data side)
// and EKOSETTLEMENT bank lines that make up that KO's totals.
function P01LinesView({ data }) {
  if (!data) return null
  if (!data.kos || data.kos.length === 0)
    return <div className="text-center py-10 text-gray-400 text-sm">No line detail for this date — run P01, or the source files use a different upload date.</div>
  return (
    <div className="space-y-3">
      {data.kos.map(ko => (
        <div key={ko.ko_id} className="card p-0 overflow-hidden">
          <div className="flex items-center justify-between px-4 py-2.5 bg-gray-50/70 border-b border-gray-100 flex-wrap gap-2">
            <div className="flex items-center gap-2.5">
              <span className="font-mono font-semibold text-sm">{ko.ko_id}</span>
              <StatusBadge process="p01" status={ko.status} /><SrcBadge row={ko} />
            </div>
            <div className="flex items-center gap-4 text-xs">
              <span className="text-gray-400">Wallet Out <span className="font-semibold text-gray-800 tabular-nums">{fmtINR(ko.wallet_withdrawn)}</span></span>
              <span className="text-gray-400">Bank Settled <span className="font-semibold text-green-700 tabular-nums">{fmtINR(ko.bank_settled)}</span></span>
              <span className="text-gray-400">Δ <Delta a={ko.bank_settled} b={ko.wallet_withdrawn} /></span>
            </div>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 md:divide-x divide-gray-100">
            <div className="p-3">
              <div className="text-[11px] uppercase tracking-wide text-gray-400 mb-1.5">Wallet Withdrawals (data) · {ko.withdrawals.length}</div>
              {ko.withdrawals.length === 0
                ? <div className="text-[11px] text-gray-300 italic">none</div>
                : ko.withdrawals.map((w, i) => (
                    <div key={i} className="flex items-center justify-between text-xs py-1 border-b border-gray-50 last:border-0">
                      <span className="tabular-nums font-medium">{fmtINR(w.amount)}</span>
                      <span className="text-gray-400 font-mono">{w.txn_date || w.datetime || '—'}</span>
                    </div>
                  ))}
            </div>
            <div className="p-3">
              <div className="text-[11px] uppercase tracking-wide text-gray-400 mb-1.5">Bank Settlements — EKOSETTLEMENT (bank) · {ko.settlements.length}</div>
              {ko.settlements.length === 0
                ? <div className="text-[11px] text-gray-300 italic">none</div>
                : ko.settlements.map((s, i) => (
                    <div key={i} className="flex items-center justify-between text-xs py-1 border-b border-gray-50 last:border-0 gap-2">
                      <span className="tabular-nums font-medium text-green-700 shrink-0">{fmtINR(s.amount)}</span>
                      <span className="text-gray-400 font-mono truncate" title={s.description || ''}>
                        {s.txn_date}{s.deduct_date && s.deduct_date !== s.txn_date ? ` · ded ${s.deduct_date}` : ''}
                      </span>
                    </div>
                  ))}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

function P01Tab({ reconDate, setReconDate }) {
  const [running, setRunning] = useState(false)
  const [data, setData] = useState(null)
  const [statusFilter, setStatusFilter] = useState('')
  const [txnDate, setTxnDate] = useState('')
  const [koSearch, setKoSearch] = useState('')
  const [viewMode, setViewMode] = useState('summary')   // 'summary' | 'lines'
  const [lines, setLines] = useState(null)

  const runRecon = async () => {
    setRunning(true)
    try {
      const { data: r } = await api.post('/sbi/run/p01', null, { params: { recon_date: reconDate } })
      toast.success(`P01 complete: ${r.summary.matched || 0} matched, ${r.summary.unmatched || 0} unmatched`)
      loadResults()
    } catch (err) { toast.error(err.response?.data?.detail || 'P01 failed') }
    finally { setRunning(false) }
  }

  const loadResults = useCallback(async () => {
    try {
      const { data: d } = await api.get('/sbi/p01/results', { params: {
        recon_date: reconDate,
        ...(statusFilter && { status: statusFilter }),
        ...(txnDate && { txn_date: txnDate }),
        ...(koSearch && { ko_id: koSearch }),
      } })
      setData(d)
    } catch { }
  }, [reconDate, statusFilter, txnDate, koSearch])
  useEffect(() => { loadResults() }, [loadResults])

  const loadLines = useCallback(async () => {
    try {
      const { data: d } = await api.get('/sbi/p01/lines', { params: {
        recon_date: reconDate,
        ...(statusFilter && { status: statusFilter }),
        ...(koSearch && { ko_id: koSearch }),
      } })
      setLines(d)
    } catch { }
  }, [reconDate, statusFilter, koSearch])
  useEffect(() => { if (viewMode === 'lines') loadLines() }, [viewMode, loadLines])

  // status counts for the cards (from the loaded rows — P01 returns the full set).
  // Seed both states so the two cards always render (a zero bucket still shows).
  const counts = { matched: 0, unmatched: 0 }
  ;(data?.rows || []).forEach(r => { counts[r.status] = (counts[r.status] || 0) + 1 })
  const g = data?.grand || {}

  return (
    <div>
      <ProcessHeader process="p01"
        left="Wallet Withdrawals (KO Limits)" right="Bank Settlements (EKOSETTLEMENT)"
        desc="Per KO ID: the total withdrawn from the CSP wallet vs the total settled into the bank. D+1 aware — the wallet deduction date can differ from the bank date." />

      <RunBar process="p01" reconDate={reconDate} setReconDate={setReconDate} onRun={runRecon} running={running}>
        <div>
          <label className="text-xs text-gray-400 block mb-1">Txn / Deduction Date</label>
          <input type="date" className="input" value={txnDate} onChange={e => setTxnDate(e.target.value)} />
        </div>
        <div>
          <label className="text-xs text-gray-400 block mb-1">KO ID</label>
          <input className="input w-32" placeholder="search KO…" value={koSearch} onChange={e => setKoSearch(e.target.value)} />
        </div>
        {(txnDate || koSearch || statusFilter) &&
          <button onClick={() => { setTxnDate(''); setKoSearch(''); setStatusFilter('') }} className="btn-ghost text-xs">Clear filters</button>}
      </RunBar>

      <div className="flex items-center gap-1 mb-4">
        {[['summary', 'Summary (per KO)'], ['lines', 'Lines (bank vs data)']].map(([m, l]) => (
          <button key={m} onClick={() => setViewMode(m)}
            className={`text-xs px-3 py-1.5 rounded-lg border transition-colors ${viewMode === m ? 'bg-primary text-white border-primary' : 'bg-white text-gray-500 border-gray-200 hover:border-gray-300'}`}>{l}</button>
        ))}
      </div>

      {viewMode === 'lines' ? <P01LinesView data={lines} /> : (data && (
        <>
          <SummaryCards process="p01" summary={counts} active={statusFilter} onPick={setStatusFilter}
            money={[
              { label: 'Wallet Out',   value: fmtINR(g.wallet_withdrawn), tone: 'text-gray-800' },
              { label: 'Bank Settled', value: fmtINR(g.bank_settled),     tone: 'text-green-700' },
              { label: 'Difference',   value: fmtINR(g.difference),       tone: Math.abs(g.difference || 0) < 1 ? 'text-green-600' : 'text-red-500' },
            ]} />

          <div className="card overflow-hidden p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-100 bg-gray-50/50 text-xs text-gray-400 uppercase tracking-wide">
                    <th className="text-left px-4 py-2.5">KO ID</th>
                    <th className="text-right px-4 py-2.5">Wallet Out</th>
                    <th className="text-center px-2 py-2.5"></th>
                    <th className="text-right px-4 py-2.5">Bank Settled</th>
                    <th className="text-right px-4 py-2.5">Δ</th>
                    <th className="text-center px-4 py-2.5">Status</th>
                    <th className="text-left px-4 py-2.5">Dates (deduct → bank)</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map(r => {
                    const dShift = r.deduct_date && r.bank_txn_date && r.deduct_date !== r.bank_txn_date
                    return (
                      <tr key={r.id} className="border-b border-gray-50 hover:bg-gray-50">
                        <td className="px-4 py-2.5 font-mono text-xs font-semibold">{r.ko_id}<SrcBadge row={r} /></td>
                        <td className="px-4 py-2.5 text-right">
                          <Side amount={r.wallet_withdrawn || 0} empty={!r.wallet_withdrawn} emptyLabel="— none —" />
                        </td>
                        <td className="px-2 py-2.5 text-center text-gray-300"><ArrowLeftRight size={13} className="inline" /></td>
                        <td className="px-4 py-2.5 text-right">
                          <Side amount={r.bank_settled || 0} accent="text-green-700" empty={!r.bank_settled} emptyLabel="— none —" />
                        </td>
                        <td className="px-4 py-2.5 text-right"><Delta a={r.bank_settled} b={r.wallet_withdrawn} /></td>
                        <td className="px-4 py-2.5 text-center"><StatusBadge process="p01" status={r.status} /></td>
                        <td className="px-4 py-2.5 text-xs font-mono text-gray-500">
                          {r.deduct_date || '—'}{dShift && <span className="text-amber-600"> → {r.bank_txn_date}</span>}
                          {dShift && <span className="ml-1 text-[10px] text-amber-600">D+1</span>}
                        </td>
                      </tr>
                    )
                  })}
                  {data.rows.length === 0 && <tr><td colSpan={7} className="text-center py-10 text-gray-400 text-sm">No results — run P01 first, or adjust filters.</td></tr>}
                </tbody>
              </table>
            </div>
          </div>
        </>
      ))}
    </div>
  )
}

// ── P02 — Bank Statement ↔ Transaction Report (by 20-digit ref) ─────────────────

function P02Tab({ reconDate, setReconDate }) {
  const [running, setRunning] = useState(false)
  const [data, setData] = useState(null)
  const [filter, setFilter] = useState('')
  const [refSearch, setRefSearch] = useState('')
  const [koSearch, setKoSearch] = useState('')
  const [page, setPage] = useState(1)
  const [mmModal, setMmModal] = useState(null)
  const [srcModal, setSrcModal] = useState(null)
  const PAGE_SIZE = 100

  const runRecon = async () => {
    setRunning(true)
    try {
      const { data: r } = await api.post('/sbi/run/p02', null, { params: { recon_date: reconDate } })
      toast.success(`P02: ${r.summary.Matched || 0} matched (incl ${r.summary.Reversal || 0} reversal legs), ${r.summary.Unmatched || 0} unmatched`)
      setPage(1); loadResults()
    } catch (err) { toast.error(err.response?.data?.detail || 'P02 failed') }
    finally { setRunning(false) }
  }

  const loadResults = useCallback(async () => {
    try {
      const { data: d } = await api.get('/sbi/p02/results', { params: {
        recon_date: reconDate, page, page_size: PAGE_SIZE,
        ...(filter && { match_status: filter }),
        ...(refSearch && { reference: refSearch }),
        ...(koSearch && { ko_id: koSearch }),
      } })
      setData(d)
    } catch { }
  }, [reconDate, filter, refSearch, koSearch, page])
  useEffect(() => { loadResults() }, [loadResults])

  // reset to page 1 whenever a filter changes
  useEffect(() => { setPage(1) }, [filter, refSearch, koSearch, reconDate])

  const rows = data?.rows || []
  // Visual pairing for reversals / duplicate refs: rows are ordered by reference_number,
  // so legs of the same real (numeric) ref are adjacent — bracket them with a left accent.
  const realRef = (s) => (s || '').match(/^\d{6,}$/) ? s : null
  const grp = rows.map((r, i) => {
    const ref = realRef(r.reference_number)
    const samePrev = ref && i > 0 && rows[i - 1].reference_number === ref
    const sameNext = ref && i < rows.length - 1 && rows[i + 1].reference_number === ref
    return { samePrev, sameNext, grouped: samePrev || sameNext }
  })

  const hasReport = (r) => ['Matched', 'Partial', 'Manual_Matched'].includes(r.match_status)

  return (
    <div>
      <ProcessHeader process="p02"
        left="Bank Statement line" right="Transaction Report line"
        desc="Each bank line is matched to a transaction-report line on the full 20-digit reference. A reference that appears as both a debit and a credit is a reversal (its two legs are bracketed)." />

      <RunBar process="p02" reconDate={reconDate} setReconDate={setReconDate} onRun={runRecon} running={running}>
        <div><input className="input w-44 text-sm" placeholder="reference…" value={refSearch} onChange={e => setRefSearch(e.target.value)} /></div>
        <div><input className="input w-28 text-sm" placeholder="KO ID…" value={koSearch} onChange={e => setKoSearch(e.target.value)} /></div>
        {(refSearch || koSearch || filter) &&
          <button onClick={() => { setRefSearch(''); setKoSearch(''); setFilter('') }} className="btn-ghost text-xs">Clear</button>}
      </RunBar>

      {data && (
        <>
          <SummaryCards process="p02" summary={data.summary} active={filter} onPick={setFilter}
            money={data.manual_matched_count > 0 ? [{ label: 'Manual', value: data.manual_matched_count, tone: 'text-amber-700' }] : null} />

          <div className="card overflow-hidden p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-gray-100 bg-gray-50/50 text-gray-400 uppercase tracking-wide">
                    <th className="text-left px-3 py-2">Status</th>
                    <th className="text-left px-3 py-2">Bank Statement</th>
                    <th className="text-center px-2 py-2"></th>
                    <th className="text-left px-3 py-2">Transaction Report</th>
                    <th className="text-right px-3 py-2">Δ</th>
                    <th className="text-center px-3 py-2">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => {
                    const grouped = grp[i].grouped
                    const drcr = r.bank_type === 'DR'
                    return (
                      <tr key={r.id}
                        className={`border-b border-gray-50 hover:bg-gray-50 ${grouped ? 'border-l-2 border-l-purple-300' : ''} ${grp[i].samePrev ? 'border-t-0' : ''}`}>
                        <td className="px-3 py-2 align-top">
                          <StatusBadge process="p02" status={r.match_status} /><SrcBadge row={r} />
                          {r.reversal_type && r.reversal_type !== 'No' && <div className="text-[10px] text-gray-400 mt-0.5">reversal leg ({drcr ? 'debit' : 'credit'})</div>}
                        </td>
                        {/* Bank side — always present */}
                        <td className="px-3 py-2 align-top">
                          <div className="leading-tight">
                            <div className="font-semibold tabular-nums">
                              <span className={drcr ? 'text-red-500' : 'text-green-600'}>{r.bank_type}</span> {fmtINR(r.bank_amount)}
                              {r.bank_txn_date && <span className="ml-2 text-[11px] font-normal text-gray-400">{r.bank_txn_date}</span>}
                            </div>
                            <div className="text-[11px] font-mono text-gray-500">{refOrBlank(r.reference_number) || '—'}</div>
                            {r.bank_description && <div className="text-[11px] text-gray-400 truncate max-w-[240px]" title={r.bank_description}>{r.bank_description}</div>}
                          </div>
                        </td>
                        <td className="px-2 py-2 text-center align-middle text-gray-300">{hasReport(r) ? <ArrowLeftRight size={13} className="inline" /> : '—'}</td>
                        {/* Report side — only when matched */}
                        <td className="px-3 py-2 align-top">
                          {hasReport(r)
                            ? <div className="leading-tight">
                                <div className="font-semibold tabular-nums text-gray-700">{r.report_amount != null ? fmtINR(r.report_amount) : '—'}</div>
                                <div className="text-[11px] text-gray-500">{r.report_txn_type || '—'}{r.report_status && <span className="ml-1 text-gray-400">· {r.report_status}</span>}</div>
                                {(r.report_journal || r.manual_counterpart) && <div className="text-[11px] font-mono text-gray-400">{r.manual_counterpart || `jrnl ${r.report_journal}`}</div>}
                              </div>
                            : <span className="text-[11px] text-gray-300 italic">{r.reversal_type && r.reversal_type !== 'No' ? '— reversal, no report —' : '— no report match —'}</span>}
                        </td>
                        <td className="px-3 py-2 text-right align-top">{hasReport(r) ? <Delta a={r.bank_amount} b={r.report_amount} /> : <span className="text-gray-300">—</span>}</td>
                        <td className="px-3 py-2 text-center whitespace-nowrap align-top">
                          {r.match_status === 'Manual_Matched'
                            ? <button onClick={() => undoManualMatch(r, loadResults)} title={r.manual_remark || ''} className="text-[11px] text-amber-700 hover:underline">↩ undo</button>
                            : ((r.match_status || '').startsWith('Unmatched') || r.match_status === 'Partial')
                              ? <span className="inline-flex items-center gap-2">
                                  {(r.match_status || '').startsWith('Unmatched') &&
                                    <button onClick={() => setMmModal(r)} className="text-[11px] text-primary hover:underline">match</button>}
                                  <button onClick={() => setSrcModal(r)} className="text-[11px] text-yellow-700 hover:underline">src</button>
                                </span>
                              : '—'}
                        </td>
                      </tr>
                    )
                  })}
                  {rows.length === 0 && <tr><td colSpan={6} className="text-center py-10 text-gray-400 text-sm">No results — run P02 first, or adjust filters.</td></tr>}
                </tbody>
              </table>
            </div>
            <Pager page={page} pageSize={PAGE_SIZE} total={data.total} onPage={setPage} />
          </div>
        </>
      )}

      {mmModal && <ManualMatchModal row={mmModal} onClose={() => setMmModal(null)} onDone={() => { setMmModal(null); loadResults() }}
        summary={<>Ref <span className="font-mono">{mmModal.reference_number}</span> · {mmModal.bank_type} · {fmtINR(mmModal.bank_amount)}.</>} />}
      {srcModal && <SrcModal process="p02" procLabel="P02 (Bank vs Transaction)" row={srcModal} onClose={() => setSrcModal(null)} onDone={() => { setSrcModal(null); loadResults() }}
        summary={<>Ref <span className="font-mono">{srcModal.reference_number}</span> · {srcModal.bank_type} · {fmtINR(srcModal.bank_amount)}.</>} />}
    </div>
  )

  async function undoManualMatch(row, reload) {
    if (!row.manual_match_id || !confirm('Undo this manual match?')) return
    try { const { data } = await api.delete(`/sbi/manual-match/${row.manual_match_id}`); if (!mcQueued(data)) toast.success('Reverted'); reload() }
    catch { toast.error('Undo failed') }
  }
}

// ── P03 — Money OUT (Txn Report) ↔ Money IN (Bank Credit) ────────────────────────

function P03Tab({ reconDate, setReconDate }) {
  const [running, setRunning] = useState(false)
  const [data, setData] = useState(null)
  const [filter, setFilter] = useState('')
  const [cspSearch, setCspSearch] = useState('')
  const [txnDate, setTxnDate] = useState('')
  const [page, setPage] = useState(1)
  const [srcModal, setSrcModal] = useState(null)
  const PAGE_SIZE = 100
  const PRIORITY_LABELS = { 1: 'same day', 2: 'bank +1 day', 3: 'txn +1 day', 4: 'bank −1 day' }

  const runRecon = async () => {
    setRunning(true)
    try {
      const { data: r } = await api.post('/sbi/run/p03', null, { params: { recon_date: reconDate } })
      toast.success(`P03: ${r.summary.Matched || 0} matched, ${r.summary.Unmatched_TxnReport || 0} without bank credit`)
      setPage(1); loadResults()
    } catch (err) { toast.error(err.response?.data?.detail || 'P03 failed') }
    finally { setRunning(false) }
  }

  const loadResults = useCallback(async () => {
    try {
      const { data: d } = await api.get('/sbi/p03/results', { params: {
        recon_date: reconDate, page, page_size: PAGE_SIZE,
        ...(filter && { match_status: filter }),
        ...(cspSearch && { csp_code: cspSearch }),
        ...(txnDate && { txn_date: txnDate }),
      } })
      setData(d)
    } catch { }
  }, [reconDate, filter, cspSearch, txnDate, page])
  useEffect(() => { loadResults() }, [loadResults])
  useEffect(() => { setPage(1) }, [filter, cspSearch, txnDate, reconDate])

  const rows = data?.rows || []
  const hasOut = (r) => r.match_status !== 'Unmatched_Bank'   // txn-report (money out) side present
  const hasIn  = (r) => r.match_status !== 'Unmatched_TxnReport' // bank-credit (money in) side present

  return (
    <div>
      <ProcessHeader process="p03"
        left="Money OUT — Transaction Report" right="Money IN — Bank Credit"
        desc="Money paid out to a CSP is matched to the bank credit received from that CSP, on CSP code + amount, allowing a ±2-day shift. Rows with only one side are orphans needing attention." />

      <RunBar process="p03" reconDate={reconDate} setReconDate={setReconDate} onRun={runRecon} running={running}>
        <div><input className="input w-32 text-sm" placeholder="CSP code…" value={cspSearch} onChange={e => setCspSearch(e.target.value)} /></div>
        <div><input type="date" className="input text-sm" value={txnDate} onChange={e => setTxnDate(e.target.value)} title="Txn / bank-credit date" /></div>
        {(cspSearch || txnDate || filter) &&
          <button onClick={() => { setCspSearch(''); setTxnDate(''); setFilter('') }} className="btn-ghost text-xs">Clear</button>}
      </RunBar>

      {data && (
        <>
          <SummaryCards process="p03" summary={data.summary} active={filter} onPick={setFilter}
            money={data.manual_matched_count > 0 ? [{ label: 'Manual', value: data.manual_matched_count, tone: 'text-amber-700' }] : null} />

          <div className="card overflow-hidden p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-gray-100 bg-gray-50/50 text-gray-400 uppercase tracking-wide">
                    <th className="text-left px-3 py-2">Status</th>
                    <th className="text-left px-3 py-2">Money OUT — Txn Report</th>
                    <th className="text-center px-2 py-2"></th>
                    <th className="text-left px-3 py-2">Money IN — Bank Credit</th>
                    <th className="text-center px-3 py-2">When</th>
                    <th className="text-center px-3 py-2">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map(r => (
                    <tr key={r.id} className="border-b border-gray-50 hover:bg-gray-50">
                      <td className="px-3 py-2 align-top"><StatusBadge process="p03" status={r.match_status} /><SrcBadge row={r} /></td>
                      <td className="px-3 py-2 align-top">
                        {hasOut(r)
                          ? <div className="leading-tight">
                              <div className="font-semibold tabular-nums">{r.txn_amount != null ? fmtINR(r.txn_amount) : '—'}</div>
                              <div className="text-[11px] font-mono text-gray-500">CSP {r.csp_code}{r.mode && r.mode !== 'Unknown' && <span className="text-gray-400"> · {r.mode}</span>}</div>
                              <div className="text-[11px] text-gray-400">{r.txn_date || ''}{refOrBlank(r.ref_number) ? ` · ${refOrBlank(r.ref_number)}` : ''}</div>
                            </div>
                          : <span className="text-[11px] text-gray-300 italic">— no txn record —</span>}
                      </td>
                      <td className="px-2 py-2 text-center align-middle text-gray-300">{r.match_status === 'Matched' ? <ArrowLeftRight size={13} className="inline" /> : '—'}</td>
                      <td className="px-3 py-2 align-top">
                        {hasIn(r)
                          ? <div className="leading-tight">
                              <div className="font-semibold tabular-nums text-green-700">{r.bank_amount != null ? fmtINR(r.bank_amount) : '—'}</div>
                              <div className="text-[11px] text-gray-400">{r.bank_credit_date || ''}{!hasOut(r) ? ` · CSP ${r.csp_code}` : ''}</div>
                            </div>
                          : <span className="text-[11px] text-gray-300 italic">— no bank credit (±2d) —</span>}
                      </td>
                      <td className="px-3 py-2 text-center text-gray-500">
                        {r.date_shift != null
                          ? <span title={PRIORITY_LABELS[r.match_priority] || ''}>{PRIORITY_LABELS[r.match_priority] || (r.date_shift > 0 ? `+${r.date_shift}d` : r.date_shift === 0 ? 'same day' : `${r.date_shift}d`)}</span>
                          : '—'}
                      </td>
                      <td className="px-3 py-2 text-center whitespace-nowrap align-top">
                        {(r.match_status || '').startsWith('Unmatched')
                          ? <button onClick={() => setSrcModal(r)} className="text-[11px] text-yellow-700 hover:underline">src</button>
                          : '—'}
                      </td>
                    </tr>
                  ))}
                  {rows.length === 0 && <tr><td colSpan={6} className="text-center py-10 text-gray-400 text-sm">No results — run P03 first, or adjust filters.</td></tr>}
                </tbody>
              </table>
            </div>
            <Pager page={page} pageSize={PAGE_SIZE} total={data.total} onPage={setPage} />
          </div>
        </>
      )}

      {srcModal && <SrcModal process="p03" procLabel="P03 (CSP-Txn-Bank)" row={srcModal} onClose={() => setSrcModal(null)} onDone={() => { setSrcModal(null); loadResults() }}
        summary={<>CSP <span className="font-mono">{srcModal.csp_code}</span> · {(META.p03[srcModal.match_status] || {}).label || srcModal.match_status}.</>} />}
    </div>
  )
}

// ── P04 — Wallet Balance (Limit Failure vs Cash Holding → action) ───────────────

function P04Tab({ reconDate, setReconDate }) {
  const [running, setRunning] = useState(false)
  const [data, setData] = useState(null)
  const [filter, setFilter] = useState('')
  const [cspSearch, setCspSearch] = useState('')

  const runRecon = async () => {
    setRunning(true)
    try {
      const { data: r } = await api.post('/sbi/run/p04', null, { params: { recon_date: reconDate } })
      toast.success(`P04: ${r.summary.DEPOSIT || 0} need deposit, ${r.summary.WITHDRAWAL || 0} need withdrawal`)
      loadResults()
    } catch (err) { toast.error(err.response?.data?.detail || 'P04 failed') }
    finally { setRunning(false) }
  }

  const loadResults = useCallback(async () => {
    try {
      const { data: d } = await api.get('/sbi/p04/results', { params: {
        recon_date: reconDate,
        ...(filter && { action_required: filter }),
        ...(cspSearch && { csp_code: cspSearch }),
      } })
      setData(d)
    } catch { }
  }, [reconDate, filter, cspSearch])
  useEffect(() => { loadResults() }, [loadResults])

  const markDone = async (id, done) => {
    try {
      await api.patch(`/sbi/p04/${id}/done`, null, { params: { done } })
      toast.success(done ? 'Marked as done' : 'Marked as pending')
      loadResults()
    } catch { toast.error('Update failed') }
  }

  const counts = data?.summary || {}

  return (
    <div>
      <ProcessHeader process="p04"
        left="Limit-Update Failure" right="KO Cash Holding balance"
        desc="For each CSP whose wallet-limit update failed, compare the current closing balance with what it should be, and flag the DEPOSIT or WITHDRAWAL needed to correct it in the SBI portal." />

      <RunBar process="p04" reconDate={reconDate} setReconDate={setReconDate} onRun={runRecon} running={running}>
        <div><input className="input w-32 text-sm" placeholder="CSP code…" value={cspSearch} onChange={e => setCspSearch(e.target.value)} /></div>
        {(cspSearch || filter) && <button onClick={() => { setCspSearch(''); setFilter('') }} className="btn-ghost text-xs">Clear</button>}
      </RunBar>

      {data && <SummaryCards process="p04" summary={counts} active={filter} onPick={setFilter} />}

      {data && data.rows.length === 0 && (
        <div className="text-center py-10 text-gray-400 text-sm">No results — upload the Cash Holding + Limit Failure files, then run P04.</div>
      )}

      {data && data.rows.length > 0 && (
        <div className="card overflow-hidden p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 bg-gray-50/50 text-xs text-gray-400 uppercase tracking-wide">
                  <th className="text-left px-4 py-2.5">CSP Code</th>
                  <th className="text-right px-4 py-2.5">Failed Op</th>
                  <th className="text-right px-4 py-2.5">Current Balance</th>
                  <th className="text-right px-4 py-2.5">Expected</th>
                  <th className="text-right px-4 py-2.5">Δ</th>
                  <th className="text-left px-4 py-2.5">Required Action</th>
                  <th className="text-center px-4 py-2.5">Done?</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map(r => (
                  <tr key={r.id} className={`border-b border-gray-50 hover:bg-gray-50 ${r.action_done ? 'opacity-50' : ''}`}>
                    <td className="px-4 py-2.5 font-mono font-semibold">{r.csp_code}<SrcBadge row={r} /></td>
                    <td className="px-4 py-2.5 text-right tabular-nums">{fmtINR(r.failed_amount)}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums">{fmtINR(r.closing_balance)}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-gray-500">{fmtINR(r.expected_balance)}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums font-semibold text-red-500">{fmtINR(r.difference)}</td>
                    <td className="px-4 py-2.5">
                      <StatusBadge process="p04" status={r.action_required} />
                      {r.action_required !== 'NONE' && <span className="ml-2 font-semibold tabular-nums">{fmtINR(r.action_amount)}</span>}
                    </td>
                    <td className="px-4 py-2.5 text-center">
                      <button onClick={() => markDone(r.id, !r.action_done)}
                        className={`text-xs px-2 py-0.5 rounded-full border font-medium transition-colors ${r.action_done ? 'bg-green-100 text-green-700 border-green-300' : 'bg-gray-100 text-gray-500 border-gray-300 hover:bg-green-50'}`}>
                        {r.action_done ? '✓ Done' : 'Mark Done'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="px-4 py-3 text-xs text-amber-700 bg-amber-50 border-t border-amber-100">
            After performing each action in the SBI Browser Portal, click "Mark Done" to track completion.
          </div>
        </div>
      )}
    </div>
  )
}

// ── Unified ledger — every bank & data entry + how each reconciled ──────────────

const U_TONE = {
  Matched: 'green', Reversal: 'purple', Partial: 'orange', 'Amount Mismatch': 'orange',
  Pending: 'amber', Excess: 'purple', Unmatched: 'red', 'Not reconciled': 'gray', Manual_Matched: 'amber',
}
function UStatus({ s }) {
  return <span className={`text-[11px] px-2 py-0.5 rounded-full font-medium whitespace-nowrap ${TONE[U_TONE[s] || 'gray']}`}>{s}</span>
}

// Manual match from the unified view — parameterised by the mapped result's process (p02/p03).
function UnifiedMatchModal({ entry, onClose, onDone }) {
  const [form, setForm] = useState({ counterpart_ref: '', remark: '' })
  const [saving, setSaving] = useState(false)
  const submit = async () => {
    if ((form.remark || '').trim().length < 5) { toast.error('Remark (≥5 chars) required'); return }
    setSaving(true)
    try {
      const { data } = await api.post('/sbi/manual-match', { process: entry.result_process, result_id: entry.result_id,
        counterpart_ref: form.counterpart_ref || null, remark: form.remark.trim() })
      if (!mcQueued(data)) toast.success('Manually matched'); onDone()
    } catch (e) { toast.error(e.response?.data?.detail || 'Manual match failed') }
    finally { setSaving(false) }
  }
  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-lg shadow-xl w-full max-w-md p-5" onClick={e => e.stopPropagation()}>
        <h3 className="font-semibold text-gray-800 mb-1">Manual match — {(entry.result_process || '').toUpperCase()}</h3>
        <p className="text-xs text-gray-500 mb-3">{entry.side} · <span className="font-mono">{entry.ref || entry.ko_csp}</span> · {fmtINR(entry.amount)}. Persists across re-runs.</p>
        <label className="text-xs text-gray-500 block mb-1">Counterpart reference (optional)</label>
        <input className="input w-full mb-3" value={form.counterpart_ref} onChange={e => setForm({ ...form, counterpart_ref: e.target.value })} placeholder="the entry it matches" />
        <label className="text-xs text-gray-500 block mb-1">Remark (required, ≥5 chars)</label>
        <textarea className="input w-full mb-4" rows={3} value={form.remark} onChange={e => setForm({ ...form, remark: e.target.value })} placeholder="Why is this a match? (audit log)" />
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="btn-ghost">Cancel</button>
          <button onClick={submit} disabled={saving} className="btn-primary">{saving ? 'Saving…' : 'Confirm match'}</button>
        </div>
      </div>
    </div>
  )
}

function UnifiedTab({ reconDate, setReconDate }) {
  const [data, setData] = useState(null)
  const [side, setSide] = useState('')
  const [statusF, setStatusF] = useState('')
  const [procF, setProcF] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [srcModal, setSrcModal] = useState(null)
  const [mmModal, setMmModal] = useState(null)
  const [sel, setSel] = useState(new Set())        // keys `${source}|${id}` — source picks the table
  const [deleting, setDeleting] = useState(false)
  const srcCodes = useSrcCodes()
  const [bulkOpen, setBulkOpen] = useState(false)  // bulk-SRC code picker
  const [bulkCode, setBulkCode] = useState('')
  const [bulkNote, setBulkNote] = useState('')
  const [bulkBusy, setBulkBusy] = useState(false)
  const canSrc = hasPermission('src_assign')
  const PAGE_SIZE = 100
  const canDelete = hasPermission('clear_data')

  // unified `source` label → /upload/delete-rows table for module 'sbi'
  const SRC_TABLE = { 'Bank Settlement': 'bank', 'Bank Statement': 'bank',
                      'Txn Report': 'txn', 'KO Withdrawal': 'ko_limits' }

  const load = useCallback(async () => {
    try {
      const { data: d } = await api.get('/sbi/unified', { params: {
        recon_date: reconDate, page, page_size: PAGE_SIZE,
        ...(side && { side }), ...(statusF && { status: statusF }),
        ...(procF && { process: procF }), ...(search && { search }),
      } })
      setData(d)
    } catch { }
  }, [reconDate, side, statusF, procF, search, page])
  useEffect(() => { load() }, [load])
  useEffect(() => { setPage(1) }, [reconDate, side, statusF, procF, search])
  useEffect(() => { setSel(new Set()) }, [reconDate, side, statusF, procF, search, page])

  const rows = data?.rows || []
  const rowKey = r => `${r.source}|${r.id}`
  const selectable = rows.filter(r => SRC_TABLE[r.source])
  const toggle = r => setSel(s => { const n = new Set(s); const k = rowKey(r); n.has(k) ? n.delete(k) : n.add(k); return n })
  const toggleAll = () => setSel(s => s.size >= selectable.length && selectable.length > 0
    ? new Set() : new Set(selectable.map(rowKey)))

  const deleteSelected = async () => {
    // partition the selected keys by source table — each table goes as one delete-rows call
    const byTable = {}
    for (const r of selectable) {
      if (!sel.has(rowKey(r))) continue
      const t = SRC_TABLE[r.source]
      ;(byTable[t] = byTable[t] || []).push(r.id)
    }
    const total = Object.values(byTable).reduce((n, a) => n + a.length, 0)
    if (!total) return
    if (!window.confirm(
      `Delete ${total} selected source entr${total === 1 ? 'y' : 'ies'}?\n\n` +
      `They go to the Recycle Bin (restorable), and the affected dates' P01–P04 results are re-run automatically so nothing points at deleted rows.`)) return
    setDeleting(true)
    try {
      let deleted = 0, rerun = new Set()
      for (const [table, ids] of Object.entries(byTable)) {
        const fd = new FormData()
        fd.append('module', 'sbi'); fd.append('table', table); fd.append('row_ids', ids.join(','))
        const { data: res } = await api.post('/upload/delete-rows', fd)
        deleted += res.deleted || 0
        for (const d of res.sbi_rerun_dates || []) rerun.add(d)
      }
      toast.success(`Deleted ${deleted} entr${deleted === 1 ? 'y' : 'ies'} → Recycle Bin` +
                    (rerun.size ? ` · re-ran ${[...rerun].join(', ')}` : ''))
      setSel(new Set())
      load()
    } catch (e) { toast.error(e.response?.data?.detail || 'Delete failed') }
    finally { setDeleting(false) }
  }

  // Bulk SRC: tag the OPEN (unmatched) rows currently shown (narrow with the
  // status / process / side / search filters first). SRC is a disposition for open
  // items, so matched rows are excluded. Each row carries its result process + id,
  // which is what the SBI overlay assign-src-bulk needs.
  const taggable = rows.filter(r => r.result_process && r.result_id && (r.status || '').startsWith('Unmatched'))
  const applyBulkSrc = async () => {
    if (!bulkCode) return toast.error('Pick a reason code')
    if (!taggable.length) return
    setBulkBusy(true)
    try {
      const items = taggable.map(r => ({ process: r.result_process, result_id: r.result_id }))
      const { data: res } = await api.post('/sbi/assign-src-bulk', { items, src_code: bulkCode, src_note: bulkNote.trim() })
      toast.success(`Tagged ${res.updated} row${res.updated === 1 ? '' : 's'}` + (res.skipped ? ` · ${res.skipped} skipped` : ''))
      setBulkOpen(false); setBulkCode(''); setBulkNote(''); load()
    } catch (e) { toast.error(e.response?.data?.detail || 'Bulk SRC failed') }
    finally { setBulkBusy(false) }
  }

  return (
    <div>
      <ProcessHeader process="p02"
        left="Every bank entry" right="Every data entry"
        desc="One row per source entry on both sides — matched or unmatched — tagged with which process (P01/P02/P03) reconciled it and its counterpart. SRC-tag or manually match right here." />

      <div className="flex items-end gap-3 mb-4 flex-wrap">
        <div><label className="text-xs text-gray-400 block mb-1">Recon / Upload Date</label>
          <input type="date" className="input" value={reconDate} onChange={e => setReconDate(e.target.value)} /></div>
        <div><label className="text-xs text-gray-400 block mb-1">Side</label>
          <select className="select text-sm" value={side} onChange={e => setSide(e.target.value)}>
            <option value="">Both sides</option><option value="bank">Bank</option><option value="data">Data</option></select></div>
        <div><label className="text-xs text-gray-400 block mb-1">Process</label>
          <select className="select text-sm" value={procF} onChange={e => setProcF(e.target.value)}>
            <option value="">All</option>{['p01', 'p02', 'p03'].map(p => <option key={p} value={p}>{p.toUpperCase()}</option>)}</select></div>
        <div><label className="text-xs text-gray-400 block mb-1">Search</label>
          <input className="input w-40 text-sm" placeholder="ref / KO / CSP…" value={search} onChange={e => setSearch(e.target.value)} /></div>
        {(side || statusF || procF || search) && <button onClick={() => { setSide(''); setStatusF(''); setProcF(''); setSearch('') }} className="btn-ghost text-xs">Clear</button>}
        {canSrc && taggable.length > 0 && (
          <button onClick={() => setBulkOpen(true)}
            className={`${canDelete && sel.size > 0 ? '' : 'ml-auto'} flex items-center gap-1.5 text-xs font-medium text-yellow-700 border border-yellow-200 rounded-lg px-3 py-2 hover:bg-yellow-50`}
            title="Tag all SRC-eligible rows currently shown — filter first to narrow the set">
            <Tag size={13} /> Bulk SRC ({taggable.length})
          </button>
        )}
        {canDelete && sel.size > 0 && (
          <button onClick={deleteSelected} disabled={deleting}
            className="flex items-center gap-1.5 text-xs font-medium text-red-600 border border-red-200 rounded-lg px-3 py-2 hover:bg-red-50 disabled:opacity-50"
            title="Recycle-binned (restorable); affected dates' results re-run automatically">
            <Trash2 size={13} />
            {deleting ? 'Deleting…' : `Delete selected (${sel.size})`}
          </button>
        )}
      </div>

      {data && (
        <>
          <div className="flex gap-2.5 mb-4 flex-wrap items-stretch">
            {Object.entries(data.status_counts || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => (
              <button key={k} onClick={() => setStatusF(statusF === k ? '' : k)}
                className={`px-3 py-2 rounded-lg border bg-white text-center min-w-[92px] ${statusF === k ? 'border-gray-800 ring-1 ring-gray-300' : 'border-gray-200 hover:border-gray-300'}`}>
                <div className="text-lg font-bold text-gray-800 leading-none tabular-nums">{v.toLocaleString('en-IN')}</div>
                <div className="mt-1.5"><UStatus s={k} /></div>
              </button>
            ))}
            <div className="ml-auto flex gap-2.5">
              {Object.entries(data.side_counts || {}).map(([k, v]) => (
                <div key={k} className="px-3 py-2 rounded-lg border border-gray-100 bg-gray-50/60 text-right min-w-[92px]">
                  <div className="text-base font-bold text-gray-800 tabular-nums leading-none">{v.toLocaleString('en-IN')}</div>
                  <div className="text-[11px] text-gray-400 mt-1.5 capitalize">{k} rows</div>
                </div>
              ))}
            </div>
          </div>

          <div className="card overflow-hidden p-0">
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead><tr className="border-b border-gray-100 bg-gray-50/50 text-gray-400 uppercase tracking-wide">
                  {canDelete && <th className="px-3 py-2 w-8">
                    <input type="checkbox" className="accent-primary cursor-pointer"
                      checked={selectable.length > 0 && sel.size >= selectable.length}
                      onChange={toggleAll} title="Select all on this page" />
                  </th>}
                  <th className="text-left px-3 py-2">Side</th>
                  <th className="text-left px-3 py-2">Source</th>
                  <th className="text-left px-3 py-2">Key</th>
                  <th className="text-right px-3 py-2">Amount</th>
                  <th className="text-left px-3 py-2">Date</th>
                  <th className="text-center px-3 py-2">Status</th>
                  <th className="text-center px-3 py-2">In</th>
                  <th className="text-left px-3 py-2">Counterpart</th>
                  <th className="text-center px-3 py-2">Action</th>
                </tr></thead>
                <tbody>
                  {rows.map(r => (
                    <tr key={rowKey(r)} className={`border-b border-gray-50 hover:bg-gray-50 ${sel.has(rowKey(r)) ? 'bg-red-50/40' : ''}`}>
                      {canDelete && <td className="px-3 py-2">
                        {SRC_TABLE[r.source]
                          ? <input type="checkbox" className="accent-primary cursor-pointer"
                              checked={sel.has(rowKey(r))} onChange={() => toggle(r)} />
                          : null}
                      </td>}
                      <td className="px-3 py-2"><span className={`text-[10px] px-1.5 py-0.5 rounded-full font-semibold ${r.side === 'bank' ? 'bg-blue-100 text-blue-700' : 'bg-green-100 text-green-700'}`}>{r.side}</span></td>
                      <td className="px-3 py-2 text-gray-600 whitespace-nowrap">{r.source}</td>
                      <td className="px-3 py-2">
                        <div className="font-mono text-gray-600">{refOrBlank(r.ref) || '—'}</div>
                        {r.ko_csp && <div className="text-[11px] text-gray-400 font-mono">{r.ko_csp}</div>}
                        {!refOrBlank(r.ref) && !r.ko_csp && r.narration &&
                          <div className="text-[11px] text-gray-400 truncate max-w-[260px]" title={r.narration}>{r.narration}</div>}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums font-medium whitespace-nowrap">{fmtINR(r.amount)}{r.drcr && <span className={`ml-1 text-[10px] ${r.drcr === 'DR' ? 'text-red-500' : 'text-green-600'}`}>{r.drcr}</span>}</td>
                      <td className="px-3 py-2 font-mono text-gray-500">{r.date || '—'}</td>
                      <td className="px-3 py-2 text-center"><UStatus s={r.status} /><SrcBadge row={r} /></td>
                      <td className="px-3 py-2 text-center whitespace-nowrap">
                        {r.process ? <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 font-medium">{r.process}</span> : '—'}
                        {r.also_p03 && r.process !== 'P03' && <span className="ml-1 text-[9px] text-gray-400">+P03</span>}
                      </td>
                      <td className="px-3 py-2 text-gray-500 max-w-[160px] truncate" title={r.counterpart || ''}>{r.counterpart || '—'}</td>
                      <td className="px-3 py-2 text-center whitespace-nowrap">
                        {r.result_process
                          ? <span className="inline-flex items-center gap-2">
                              {['p02', 'p03'].includes(r.result_process) && (r.status || '').startsWith('Unmatched') &&
                                <button onClick={() => setMmModal(r)} className="text-[11px] text-primary hover:underline">match</button>}
                              <button onClick={() => setSrcModal(r)} className="text-[11px] text-yellow-700 hover:underline">src</button>
                            </span>
                          : '—'}
                      </td>
                    </tr>
                  ))}
                  {rows.length === 0 && <tr><td colSpan={canDelete ? 10 : 9} className="text-center py-10 text-gray-400 text-sm">No entries — pick a date with SBI data.</td></tr>}
                </tbody>
              </table>
            </div>
            <Pager page={page} pageSize={PAGE_SIZE} total={data.total} onPage={setPage} />
          </div>
        </>
      )}

      {srcModal && <SrcModal process={srcModal.result_process} procLabel={`SBI ${(srcModal.result_process || '').toUpperCase()}`}
        row={{ ...srcModal, id: srcModal.result_id }} onClose={() => setSrcModal(null)} onDone={() => { setSrcModal(null); load() }}
        summary={<>{srcModal.side} · <span className="font-mono">{srcModal.ref || srcModal.ko_csp}</span> · {fmtINR(srcModal.amount)}.</>} />}
      {mmModal && <UnifiedMatchModal entry={mmModal} onClose={() => setMmModal(null)} onDone={() => { setMmModal(null); load() }} />}

      {bulkOpen && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={() => setBulkOpen(false)}>
          <div className="bg-white rounded-lg shadow-xl w-full max-w-md p-5" onClick={e => e.stopPropagation()}>
            <h3 className="font-semibold text-gray-800 mb-1">Bulk SRC — {taggable.length} row{taggable.length === 1 ? '' : 's'}</h3>
            <p className="text-xs text-gray-500 mb-3">Applies one reason code to every SRC-eligible row currently shown. Narrow with the status / process / side / search filters first. Persists across re-runs.</p>
            <label className="text-xs text-gray-500 block mb-1">Reason code (required)</label>
            <select className="select w-full mb-3" value={bulkCode} onChange={e => setBulkCode(e.target.value)}>
              <option value="">Select a reason…</option>
              {srcCodes.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
            <label className="text-xs text-gray-500 block mb-1">Note (optional)</label>
            <textarea className="input w-full mb-4" rows={2} value={bulkNote} onChange={e => setBulkNote(e.target.value)} placeholder="Optional context for this disposition" />
            <div className="flex justify-end gap-2">
              <button onClick={() => setBulkOpen(false)} className="btn-ghost">Cancel</button>
              <button onClick={applyBulkSrc} disabled={bulkBusy || !bulkCode} className="btn-primary">{bulkBusy ? 'Tagging…' : `Tag ${taggable.length}`}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Manual Match (two-sided pair-picker) ────────────────────────────────────────
// Bank open items on the left, internal open items (Txn Reports — each labelled with its
// source file — KO Withdrawals + KO Deposits) on the right. Pick a row on each side, queue
// the pair, submit all. Free-form: any bank↔internal pairing is allowed; an amount mismatch
// is shown as a warning, not blocked. Reuses /sbi/manual-match/* (survives re-runs/re-uploads).

function PairRow({ r, selected, disabled, onClick }) {
  const drcr = r.drcr
  return (
    <tr onClick={disabled ? undefined : onClick}
      className={`border-t border-gray-100 transition-colors ${disabled ? 'opacity-40 cursor-not-allowed'
        : selected ? 'bg-primary/10 cursor-pointer' : 'hover:bg-gray-50 cursor-pointer'}`}>
      <td className="px-3 py-2">
        <div className="text-[11px] font-semibold text-gray-700 whitespace-nowrap">{r.file || r.source}</div>
        <div className="text-[10px] text-gray-400">{r.date}</div>
      </td>
      <td className="px-3 py-2 font-mono text-[11px] text-gray-600 whitespace-nowrap">{r.ko_csp || '—'}</td>
      <td className="px-3 py-2 max-w-[190px]">
        <div className="font-mono text-[11px] text-gray-500 truncate" title={r.ref}>{r.ref || '—'}</div>
        {r.narration && <div className="text-[10px] text-gray-400 truncate" title={r.narration}>{r.narration}</div>}
      </td>
      <td className="px-3 py-2 text-right tabular-nums font-medium whitespace-nowrap">{fmtINR(r.amount)}
        {drcr && <span className={`ml-1 text-[10px] ${drcr === 'DR' ? 'text-red-500' : 'text-green-600'}`}>{drcr}</span>}</td>
    </tr>
  )
}

function PairPanel({ title, tone, items, selected, onSelect, queuedIds, subtitle, filterValue, filterOptions, onFilterChange }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white overflow-hidden">
      <div className={`px-4 py-2.5 border-b ${tone}`}>
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">{title} <span className="opacity-60">({items.length})</span></span>
          {filterOptions?.length > 0 && (
            <select className="input ml-auto text-xs py-1" value={filterValue} onChange={e => onFilterChange(e.target.value)}>
              <option value="">All files ({filterOptions.length})</option>
              {filterOptions.map(f => <option key={f} value={f}>{f}</option>)}
            </select>
          )}
        </div>
        {subtitle && <div className="text-[10px] font-normal opacity-70 mt-0.5">{subtitle}</div>}
      </div>
      <div className="max-h-[420px] overflow-auto">
        {items.length === 0
          ? <div className="text-center text-gray-400 text-sm py-10">No open items — load first</div>
          : <table className="w-full text-sm">
              <thead className="text-[10px] uppercase tracking-wide text-gray-400 bg-gray-50/60 sticky top-0">
                <tr><th className="px-3 py-2 text-left font-medium">Source / File</th>
                  <th className="px-3 py-2 text-left font-medium">KO / CSP</th>
                  <th className="px-3 py-2 text-left font-medium">Ref</th>
                  <th className="px-3 py-2 text-right font-medium">Amount</th></tr>
              </thead>
              <tbody>{items.map(r => (
                <PairRow key={r.id} r={r} selected={selected?.id === r.id}
                  disabled={queuedIds.has(r.id)} onClick={() => onSelect(r)} />
              ))}</tbody>
            </table>}
      </div>
    </div>
  )
}

function ManualPairTab({ reconDate }) {
  const canEdit = hasPermission('src_assign')
  const [from, setFrom] = useState(reconDate)
  const [to, setTo] = useState(reconDate)
  const [search, setSearch] = useState('')
  const [bankItems, setBankItems] = useState([])
  const [dataItems, setDataItems] = useState([])
  const [loading, setLoading] = useState(false)
  const [selBank, setSelBank] = useState(null)
  const [selData, setSelData] = useState(null)
  const [dataFile, setDataFile] = useState('')   // '' = all files (internal-side file filter)
  const [queue, setQueue] = useState([])
  const [remark, setRemark] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [pairs, setPairs] = useState([])

  useEffect(() => { setFrom(reconDate); setTo(reconDate) }, [reconDate])

  const load = async () => {
    if (!from) { toast.error('Pick a From date'); return }
    setLoading(true)
    try {
      const params = { date_from: from, date_to: to || from, search: search || undefined, page_size: 500 }
      const [b, d, p] = await Promise.all([
        api.get('/sbi/manual-match/open-items', { params: { ...params, side: 'bank' } }),
        api.get('/sbi/manual-match/open-items', { params: { ...params, side: 'data' } }),
        api.get('/sbi/manual-match/pair', { params: { date_from: from, date_to: to || from } }),
      ])
      setBankItems(b.data.items || [])
      setDataItems(d.data.items || [])
      setPairs(p.data.rows || [])
      setSelBank(null); setSelData(null); setDataFile('')
    } catch (e) { toast.error(e?.response?.data?.detail || 'Failed to load open items') }
    setLoading(false)
  }

  const queuedIds = new Set(queue.flatMap(q => [q.bank.id, q.data.id]))
  const diffOf = (b, d) => Math.abs(Number(b?.amount || 0) - Number(d?.amount || 0))

  // Internal-side file-wise filter (client-side — the full list is already loaded).
  const dataFiles = [...new Set(dataItems.map(r => r.file).filter(Boolean))].sort()
  const shownData = dataFile ? dataItems.filter(r => r.file === dataFile) : dataItems
  // Bank-side count breakdown — reconciles the picker's broad count with the report's
  // narrower "open": rows WITH a bank reference are the report's open items; the rest are
  // no-reference cash-deposit / informational lines the report doesn't count as open.
  const bankWithRef = bankItems.filter(r => r.ref).length
  const bankNoRef = bankItems.length - bankWithRef

  const addPair = () => {
    if (!selBank || !selData) { toast('Select one row on each side', { icon: 'ℹ️' }); return }
    if (queuedIds.has(selBank.id) || queuedIds.has(selData.id)) { toast('Row already queued', { icon: '⚠️' }); return }
    setQueue(q => [...q, { bank: selBank, data: selData }])
    setSelBank(null); setSelData(null)
  }
  const removeQ = i => setQueue(q => q.filter((_, idx) => idx !== i))

  const submit = async () => {
    if (!queue.length) return
    if (remark.trim().length < 5) { toast.error('A remark (≥5 characters) is required'); return }
    setSubmitting(true)
    try {
      const payload = { pairs: queue.map(q => ({ bank_id: q.bank.id, data_id: q.data.id, data_source: q.data.source })), remark: remark.trim() }
      const { data } = await api.post('/sbi/manual-match/pair', payload)
      if (data.queued) { toast(data.message || 'Queued for approval', { icon: '🕒' }); setQueue([]); setRemark('') }
      else {
        const msg = `${data.paired} paired${data.warned ? ` · ${data.warned} amount warning` : ''}${data.errors ? ` · ${data.errors} failed` : ''}`
        data.errors ? toast(msg, { icon: '⚠️' }) : toast.success(msg)
        setQueue([]); setRemark(''); await load()
      }
    } catch (e) { toast.error(e?.response?.data?.detail || 'Submit failed') }
    setSubmitting(false)
  }

  const unpair = async id => {
    try {
      const { data } = await api.delete(`/sbi/manual-match/pair/${id}`)
      if (data.queued) { toast(data.message || 'Queued for approval', { icon: '🕒' }); return }
      toast.success('Unmatched'); await load()
    } catch (e) { toast.error(e?.response?.data?.detail || 'Unmatch failed') }
  }

  return (
    <div className="space-y-5">
      <div className="rounded-xl border border-gray-200 bg-white p-4">
        <div className="text-sm font-semibold text-gray-800 mb-1">Manual Match — pick a bank row and an internal row, queue, submit</div>
        <p className="text-xs text-gray-400 mb-3">Internal side spans every file (Txn Reports — each shown with its source file — KO Withdrawals &amp; KO Deposits); use the file dropdown on that panel to narrow to one file. Use a date range to catch a D+1 settlement. Manual pairs persist across re-runs and re-uploads.</p>
        <div className="flex flex-wrap items-end gap-3">
          <div><label className="text-xs text-gray-400 block mb-1">From Date</label>
            <input type="date" className="input" value={from} onChange={e => setFrom(e.target.value)} /></div>
          <div><label className="text-xs text-gray-400 block mb-1">To Date</label>
            <input type="date" className="input" value={to} onChange={e => setTo(e.target.value)} /></div>
          <div className="flex-1 min-w-[180px]"><label className="text-xs text-gray-400 block mb-1">Search (Ref / KO / CSP / File)</label>
            <input className="input w-full" placeholder="optional…" value={search} onChange={e => setSearch(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && load()} /></div>
          <button onClick={load} disabled={loading} className="btn flex items-center gap-1.5">
            {loading ? <RefreshCw size={15} className="animate-spin" /> : <Play size={15} />} Load Open Items
          </button>
        </div>
      </div>

      {!canEdit && <div className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">You have read-only access — manual matching needs the <span className="font-semibold">src_assign</span> permission.</div>}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <PairPanel title="Bank Statement" tone="text-blue-700 bg-blue-50/60 border-blue-100" items={bankItems}
          subtitle={bankItems.length ? `Matches the reconciliation report's open bank items · ${bankWithRef} with a bank reference · ${bankNoRef} cash-deposit / no-reference` : undefined}
          selected={selBank} onSelect={setSelBank} queuedIds={queuedIds} />
        <PairPanel title="Internal / Data" tone="text-green-700 bg-green-50/60 border-green-100" items={shownData}
          filterValue={dataFile} filterOptions={dataFiles} onFilterChange={setDataFile}
          selected={selData} onSelect={setSelData} queuedIds={queuedIds} />
      </div>

      {/* selection → add-to-queue bar */}
      <div className="rounded-xl border border-gray-200 bg-gray-50/60 p-3 flex flex-wrap items-center gap-3">
        <div className="text-xs text-gray-500">Bank: {selBank ? <span className="font-mono text-gray-800">{selBank.file} · {fmtINR(selBank.amount)}</span> : <span className="text-gray-400">none</span>}</div>
        <ArrowLeftRight size={15} className="text-gray-400" />
        <div className="text-xs text-gray-500">Internal: {selData ? <span className="font-mono text-gray-800">{selData.file} · {fmtINR(selData.amount)}</span> : <span className="text-gray-400">none</span>}</div>
        {selBank && selData && diffOf(selBank, selData) > 0.01 &&
          <span className="text-[11px] text-amber-700 bg-amber-100 rounded-full px-2 py-0.5 flex items-center gap-1"><AlertTriangle size={11} />Amounts differ by {fmtINR(diffOf(selBank, selData))}</span>}
        <button onClick={addPair} disabled={!selBank || !selData || !canEdit} className="btn-ghost text-sm flex items-center gap-1.5 ml-auto"><Check size={14} /> Add pair to queue</button>
      </div>

      {/* queue */}
      {queue.length > 0 && (
        <div className="rounded-xl border border-gray-200 bg-white p-4">
          <div className="text-sm font-semibold text-gray-800 mb-2">Queued pairs ({queue.length})</div>
          <div className="space-y-1.5 mb-3">
            {queue.map((q, i) => {
              const warn = diffOf(q.bank, q.data) > 0.01
              return (
                <div key={i} className="flex items-center gap-2 text-xs bg-gray-50 rounded-lg px-3 py-2">
                  <span className="font-mono text-blue-700 whitespace-nowrap">{q.bank.file} · {fmtINR(q.bank.amount)}</span>
                  <ArrowLeftRight size={12} className="text-gray-400 shrink-0" />
                  <span className="font-mono text-green-700 whitespace-nowrap">{q.data.file} · {fmtINR(q.data.amount)}</span>
                  {warn && <span className="text-[10px] text-amber-700 bg-amber-100 rounded-full px-1.5 py-0.5 whitespace-nowrap">Δ {fmtINR(diffOf(q.bank, q.data))}</span>}
                  <button onClick={() => removeQ(i)} className="ml-auto text-gray-400 hover:text-red-500"><X size={14} /></button>
                </div>
              )
            })}
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex-1 min-w-[220px]"><label className="text-xs text-gray-400 block mb-1">Remark (required, ≥5 chars — kept in the audit trail)</label>
              <input className="input w-full" placeholder="why these are matched…" maxLength={500} value={remark} onChange={e => setRemark(e.target.value)} /></div>
            <button onClick={submit} disabled={submitting || !canEdit || remark.trim().length < 5}
              className="btn flex items-center gap-1.5">{submitting ? <RefreshCw size={15} className="animate-spin" /> : <Check size={15} />} Submit {queue.length} pair{queue.length > 1 ? 's' : ''}</button>
          </div>
        </div>
      )}

      {/* existing pairs */}
      {pairs.length > 0 && (
        <div className="rounded-xl border border-gray-200 bg-white overflow-hidden">
          <div className="px-4 py-2.5 text-sm font-semibold text-amber-700 bg-amber-50/60 border-b border-amber-100">Manual pairs in range ({pairs.length})</div>
          <div className="max-h-[300px] overflow-auto">
            <table className="w-full text-sm">
              <thead className="text-[10px] uppercase tracking-wide text-gray-400 bg-gray-50/60">
                <tr><th className="px-3 py-2 text-left font-medium">Bank</th><th className="px-3 py-2 text-left font-medium">Internal</th>
                  <th className="px-3 py-2 text-right font-medium">Δ</th><th className="px-3 py-2 text-left font-medium">Remark</th>
                  <th className="px-3 py-2 text-left font-medium">By</th><th className="px-3 py-2"></th></tr>
              </thead>
              <tbody>{pairs.map(p => (
                <tr key={p.id} className="border-t border-gray-100">
                  <td className="px-3 py-2 text-xs whitespace-nowrap"><span className="text-blue-700">{p.bank_source}</span> {fmtINR(p.bank_amount)} <span className="text-gray-400">· {p.bank_date}</span></td>
                  <td className="px-3 py-2 text-xs whitespace-nowrap"><span className="text-green-700">{p.data_source}</span> {fmtINR(p.data_amount)} <span className="text-gray-400">· {p.data_date}</span></td>
                  <td className="px-3 py-2 text-right text-xs tabular-nums">{p.amount_diff > 0.01 ? <span className="text-amber-600">{fmtINR(p.amount_diff)}</span> : <span className="text-gray-300">—</span>}</td>
                  <td className="px-3 py-2 text-xs text-gray-500 max-w-[200px] truncate" title={p.remark}>{p.remark}</td>
                  <td className="px-3 py-2 text-xs text-gray-400 whitespace-nowrap">{p.by}</td>
                  <td className="px-3 py-2 text-right">{canEdit && <button onClick={() => unpair(p.id)} title="Unmatch" className="text-gray-400 hover:text-red-500"><Trash2 size={14} /></button>}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Main Page ──────────────────────────────────────────────────────────────────

const PROCESSES = [
  { key: 'unified', label: '🔎 All Entries' },
  { key: 'pair', label: '🔗 Manual Match' },
  { key: 'p01', label: '💳 P01 · Wallet ↔ Settlement' },
  { key: 'p02', label: '📊 P02 · Bank ↔ Txn Report' },
  { key: 'p03', label: '🔄 P03 · Money Out ↔ In' },
  { key: 'p04', label: '⚖️ P04 · Wallet Balance' },
]

// Header quick actions: run all four processes for one date, and pull the
// Daily Recon Pack (overview + all processes + exceptions + logs) in one click.
function HeaderActions() {
  const [date, setDate] = useLatestSbiDate()   // the Daily Pack's date (defaults to latest data day)
  const dailyPack = async () => {
    try {
      const r = await api.get('/sbi/report', { params: { type: 'daily_pack', recon_date: date }, responseType: 'blob' })
      const used = r.headers['x-recon-date']
      if (used === 'none') { toast('No SBI recon results yet — Reconcile all dates first', { icon: 'ℹ️' }); return }
      const url = URL.createObjectURL(r.data)
      const a = document.createElement('a'); a.href = url; a.download = `sbi_daily_pack_${used}.xlsx`; a.click()
      URL.revokeObjectURL(url)
      if (used !== date) toast(`No data for ${date} — exported latest (${used})`, { icon: 'ℹ️' })
    } catch { toast.error('Report failed') }
  }
  return (
    <div className="flex items-end gap-2">
      <div>
        <label className="text-xs text-gray-400 block mb-1">Daily Pack date</label>
        <input type="date" className="input" value={date} onChange={e => setDate(e.target.value)} />
      </div>
      <button onClick={dailyPack} title="Overview + all four processes + exceptions + logs, one workbook"
        className="btn-ghost flex items-center gap-1.5 text-sm whitespace-nowrap">
        ⬇ Daily Pack
      </button>
    </div>
  )
}

// One "how much data do we have" stat tile. Totals are across ALL dates, so the data is
// always visible — never hidden behind a single-day filter.
function OverviewTile({ icon, label, value }) {
  const ok = Number(value || 0) > 0
  return (
    <div className={`rounded-xl border p-3 text-center transition-colors ${ok ? 'bg-white border-gray-200 shadow-sm' : 'bg-gray-50/70 border-gray-100'}`}>
      <div className="text-lg mb-0.5 opacity-80">{icon}</div>
      <div className={`text-lg font-bold tabular-nums leading-tight ${ok ? 'text-gray-800' : 'text-gray-300'}`}>{Number(value || 0).toLocaleString('en-IN')}</div>
      <div className="text-[11px] text-gray-500 leading-tight mt-0.5">{label}</div>
    </div>
  )
}

const rateColor = (r) => (r >= 85 ? '#059669' : r >= 50 ? '#d97706' : '#dc2626')

// Compact status for a day: match % / "report missing" / "not run".
function DayStatus({ r, size = 'sm' }) {
  const cls = size === 'lg' ? 'text-base font-bold' : 'text-xs font-semibold'
  if (r.p02_rate != null) return <span className={`${cls} tabular-nums`} style={{ color: rateColor(r.p02_rate) }}>{r.p02_rate}%</span>
  if (r.bank && !r.txn_report) return <span className="text-[11px] px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-100">report missing</span>
  return <span className="text-[11px] text-gray-400">not run</span>
}

// Full dropdown of business dates — each option shows that day's status at a glance.
function DateDropdown({ rows, value, onChange }) {
  const [open, setOpen] = useState(false)
  const ref = React.useRef()
  useEffect(() => {
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [])
  const sel = rows.find(r => r.date === value)
  return (
    <div className="relative" ref={ref}>
      <button onClick={() => setOpen(o => !o)}
        className="flex items-center justify-between gap-3 min-w-[260px] rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm hover:border-gray-300 transition-colors">
        <span className="font-mono text-gray-800">{value || 'Select a date'}</span>
        <span className="flex items-center gap-2">{sel && <DayStatus r={sel} />}<ChevronDown size={15} className={`text-gray-400 transition-transform ${open ? 'rotate-180' : ''}`} /></span>
      </button>
      {open && (
        <div className="absolute z-30 mt-1 w-full max-h-[22rem] overflow-auto rounded-lg border border-gray-200 bg-white shadow-xl py-1">
          {rows.map(r => (
            <button key={r.date} onClick={() => { onChange(r.date); setOpen(false) }}
              className={`w-full flex items-center justify-between gap-3 px-3 py-2 text-left hover:bg-gray-50 ${r.date === value ? 'bg-primary/5' : ''}`}>
              <span className="flex items-center gap-2">
                {r.date === value ? <Check size={13} className="text-primary" /> : <span className="w-[13px]" />}
                <span className="font-mono text-xs text-gray-700">{r.date}</span>
              </span>
              <DayStatus r={r} />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function MiniStat({ label, value, warn }) {
  const ok = Number(value || 0) > 0
  return (
    <div className={`rounded-lg border p-2.5 text-center ${warn ? 'bg-red-50 border-red-100' : 'bg-gray-50/70 border-gray-100'}`}>
      <div className={`text-base font-bold tabular-nums ${warn ? 'text-red-500' : ok ? 'text-gray-800' : 'text-gray-300'}`}>{ok ? Number(value).toLocaleString('en-IN') : '—'}</div>
      <div className="text-[11px] text-gray-500 leading-tight mt-0.5">{label}{warn && ' · missing'}</div>
    </div>
  )
}

// Data overview + per-business-date readiness (via a date dropdown instead of a wide table).
// One /sbi/readiness call drives both. Replaces the old today-filtered "Upload Status".
function ReadinessPanel({ refreshKey, onReconciled, reconDate, setReconDate }) {
  const [data, setData] = useState(null)
  const load = async () => {
    try {
      const { data } = await api.get('/sbi/readiness')
      setData(data)
      // keep the shared date valid — if it isn't a day we have data for, snap to the latest
      if (!(data.dates || []).some(r => r.date === reconDate)) setReconDate(data.latest || '')
    } catch { setData({ dates: [], totals: {} }) }
  }
  useEffect(() => { load() }, [refreshKey])
  if (!data) return null
  const t = data.totals || {}
  const rows = data.dates || []
  const missing = rows.filter(r => r.bank && !r.txn_report)
  const hasData = (t.bank || 0) + (t.txn_report || 0) > 0
  const day = rows.find(r => r.date === reconDate)

  return (
    <>
      {/* Data overview — totals across every date */}
      <div className="grid grid-cols-3 lg:grid-cols-6 gap-3 mb-4">
        <OverviewTile icon="🏦" label="Bank statement rows" value={t.bank} />
        <OverviewTile icon="📋" label="Transaction reports" value={t.txn_report} />
        <OverviewTile icon="⚙️" label="KO limits" value={t.ko_limits} />
        <OverviewTile icon="💰" label="KO cash holding" value={t.cash_holding} />
        <OverviewTile icon="⚠️" label="Limit failures" value={t.limit_failures} />
        <OverviewTile icon="📊" label="CSP master" value={t.csp_master} />
      </div>

      {hasData && (
        <div className="card mb-5">
          <div className="flex items-center justify-between mb-3 flex-wrap gap-3">
            <div>
              <h3 className="font-semibold text-gray-800 text-sm">Reconciliation readiness</h3>
              <p className="text-xs text-gray-400 mt-0.5">
                {t.days_with_data || rows.length} day(s) of data
                {t.p02_rate != null && <> · overall P02 match <b style={{ color: rateColor(t.p02_rate) }}>{t.p02_rate}%</b></>}
                {t.days_missing_report ? <> · <span className="text-amber-600">{t.days_missing_report} day(s) missing a report file</span></> : ''}
              </p>
            </div>
            <RunAllDatesButton onDone={() => { load(); onReconciled && onReconciled() }} />
          </div>

          {missing.length > 0 && (
            <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs text-amber-800 leading-relaxed flex gap-2">
              <AlertTriangle size={14} className="mt-0.5 flex-shrink-0" />
              <span><b>{missing.length} day(s)</b> have a bank statement but <b>no transaction report</b> uploaded
              ({missing.map(r => r.date).join(', ')}). Those days can’t be matched until their report file is
              uploaded — a <b>missing file, not a recon failure</b>.</span>
            </div>
          )}

          {/* Pick a business date, see its detail */}
          <div className="flex items-center gap-2 mb-4 flex-wrap">
            <span className="text-xs text-gray-400">Business date</span>
            <DateDropdown rows={rows} value={reconDate} onChange={setReconDate} />
            <span className="text-[11px] text-gray-400">— also filters the P01–P04 tabs below</span>
            <div className="ml-auto"><DownloadReports reconDate={reconDate} /></div>
          </div>

          {day && (
            <div>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
                <MiniStat label="Bank statement" value={day.bank} />
                <MiniStat label="Txn report" value={day.txn_report} warn={day.bank && !day.txn_report} />
                <MiniStat label="KO limits" value={day.ko_limits} />
                <MiniStat label="Cash holding" value={day.cash_holding} />
                <MiniStat label="Limit failures" value={day.limit_failures} />
                <div className="rounded-lg border border-gray-100 bg-white p-2.5 text-center flex flex-col justify-center">
                  <div className="leading-tight"><DayStatus r={day} size="lg" /></div>
                  <div className="text-[11px] text-gray-500 mt-0.5">P02 match</div>
                </div>
              </div>
              {day.source_files_total > 0 && (
                <div className="mt-3 flex items-center gap-2 flex-wrap text-[11px]">
                  <span className="text-gray-400">Source files:</span>
                  <span className={`font-semibold ${day.source_files_have === day.source_files_total ? 'text-emerald-600' : 'text-amber-600'}`}>
                    {day.source_files_have}/{day.source_files_total} present
                  </span>
                  {(day.source_files_missing || []).length > 0 && <>
                    <span className="text-gray-300">·</span>
                    <span className="text-red-500">missing:</span>
                    {day.source_files_missing.map(p => (
                      <span key={p} className="px-2 py-0.5 rounded-full bg-red-50 text-red-600 border border-red-100">{p}</span>
                    ))}
                  </>}
                </div>
              )}
              {day.p02_total > 0 && (
                <p className="text-[11px] text-gray-400 mt-2">
                  {Number(day.p02_matched).toLocaleString('en-IN')} matched
                  {day.p02_reversal > 0 && <> + {Number(day.p02_reversal).toLocaleString('en-IN')} reversal <span className="text-gray-300">(net-zero, reconciled)</span></>}
                  {' '}of {Number(day.p02_total).toLocaleString('en-IN')} bank rows on {day.date}.
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </>
  )
}

export default function SBIKiosk() {
  const [tab, setTab] = useState('upload')
  const [uploadKey, setUploadKey] = useState(0)
  const onUploadDone = () => setUploadKey(k => k + 1)
  // Shared business date — the Readiness dropdown and the P01–P04 tabs all read/write it,
  // so picking a date up top drills the whole page into that day (defaults to latest data).
  const [reconDate, setReconDate] = useLatestSbiDate()

  return (
    <div>
      <div className="flex items-center justify-between mb-5 flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-800">SBI Kiosk Banking Recon</h1>
          <p className="text-sm text-gray-400">
            Upload the process files (<span className="font-medium">Upload</span> tab), then <span className="font-medium">Reconcile all dates</span>. Each day is reconciled on its own business date.
          </p>
        </div>
        <HeaderActions />
      </div>

      <ReadinessPanel refreshKey={uploadKey} onReconciled={onUploadDone} reconDate={reconDate} setReconDate={setReconDate} />

      {/* Tab bar */}
      <div className="flex gap-0 mb-5 border-b border-gray-200 overflow-x-auto">
        <button key="upload" onClick={() => setTab('upload')}
          className={`px-5 py-3 text-sm font-medium border-b-2 transition-colors whitespace-nowrap
            ${tab === 'upload' ? 'border-primary text-primary' : 'border-transparent text-gray-500 hover:text-gray-700'}`}>
          ⬆ Upload
        </button>
        {PROCESSES.map(p => (
          <button key={p.key} onClick={() => setTab(p.key)}
            className={`px-5 py-3 text-sm font-medium border-b-2 transition-colors whitespace-nowrap
              ${tab === p.key ? 'border-primary text-primary' : 'border-transparent text-gray-500 hover:text-gray-700'}`}>
            {p.label}
          </button>
        ))}
      </div>

      {/* Upload tab — card-style uploads for all four processes */}
      {tab === 'upload' && (
        <div className="space-y-6">
          <div>
            <div className="flex items-center gap-2 mb-3">
              <span className="text-xs font-bold text-blue-700 bg-blue-100 px-2.5 py-1 rounded-full uppercase tracking-wide">P01 — Settlement Recon</span>
            </div>
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
              <UploadCard label="Bank Statement" desc="SBI settlement account — .xlsx or tab-separated .xls" endpoint="/sbi/upload/bank-statement" onDone={onUploadDone} color="blue" />
              <UploadCard label="KO Limits Config Report" desc="KO_Limits_Configuration_Report_*.xls" endpoint="/sbi/upload/ko-limits" onDone={onUploadDone} color="blue" />
            </div>
          </div>

          <div>
            <div className="flex items-center gap-2 mb-3">
              <span className="text-xs font-bold text-green-700 bg-green-100 px-2.5 py-1 rounded-full uppercase tracking-wide">P02 — Bank + Transaction Report Recon</span>
              <span className="text-xs text-gray-400">Upload all 7 transaction report files one at a time</span>
            </div>
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
              <UploadCard label="Transaction Report File" desc="Any of the 7 BC Transaction Report files (AEPS, Withdrawal, Deposit, Money Transfer, etc.)" endpoint="/sbi/upload/txn-report" onDone={onUploadDone} color="green" />
            </div>
          </div>

          <div>
            <div className="flex items-center gap-2 mb-3">
              <span className="text-xs font-bold text-purple-700 bg-purple-100 px-2.5 py-1 rounded-full uppercase tracking-wide">P03 — CSP-Transaction-Bank Recon</span>
            </div>
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
              <UploadCard label="CSP Master Sheet" desc="CSP Code | Ref Number | Mode (Cash/CDM/Electronic)" endpoint="/sbi/upload/csp-master" onDone={onUploadDone} color="purple" />
            </div>
          </div>

          <div>
            <div className="flex items-center gap-2 mb-3">
              <span className="text-xs font-bold text-amber-700 bg-amber-100 px-2.5 py-1 rounded-full uppercase tracking-wide">P04 — Wallet Balance Recon</span>
            </div>
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
              <UploadCard label="KO Cash Holding Report" desc="KO_Cash_Holding_Report_*.xls — opening/closing wallet balances" endpoint="/sbi/upload/ko-cash-holding" onDone={onUploadDone} color="amber"
                extraFields={[{ name: 'report_date', label: 'Report Date', type: 'date', placeholder: 'YYYY-MM-DD' }]} />
              <UploadCard label="Limit Update Failure Report" desc="Limit_Fail_*.xls — KOs whose wallet limit update failed" endpoint="/sbi/upload/limit-failures" onDone={onUploadDone} color="amber" />
            </div>
          </div>
        </div>
      )}

      {tab === 'unified' && <UnifiedTab reconDate={reconDate} setReconDate={setReconDate} />}
      {tab === 'pair' && <ManualPairTab reconDate={reconDate} />}
      {tab === 'p01' && <P01Tab reconDate={reconDate} setReconDate={setReconDate} />}
      {tab === 'p02' && <P02Tab reconDate={reconDate} setReconDate={setReconDate} />}
      {tab === 'p03' && <P03Tab reconDate={reconDate} setReconDate={setReconDate} />}
      {tab === 'p04' && <P04Tab reconDate={reconDate} setReconDate={setReconDate} />}
    </div>
  )
}
