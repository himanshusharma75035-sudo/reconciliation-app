import React, { useState, useEffect, useCallback } from 'react'
import { Upload, Play, RefreshCw, X, ChevronLeft, ChevronRight, ArrowLeftRight } from 'lucide-react'
import api from '../utils/api'
import toast from 'react-hot-toast'

// ── Money / date helpers ────────────────────────────────────────────────────────

function fmtINR(n) {
  if (n == null || isNaN(n)) return '₹0'
  const abs = Math.abs(Number(n))
  return `₹${abs.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}
function today() { return new Date().toISOString().split('T')[0] }

// ── Plain-language status metadata, per process ─────────────────────────────────
// label = human wording the team reads; tone = colour; hint = hover tooltip that
// explains what the status actually means (incl. the common "file not uploaded" cases).
const META = {
  p01: {
    CREDITED: { label: 'Settled',        tone: 'green',  hint: 'Wallet withdrawal is fully matched by the bank settlement credit.' },
    PENDING:  { label: 'Awaiting bank',  tone: 'amber',  hint: 'Wallet was withdrawn but no bank settlement appears yet — D+1 timing, or the bank statement for this date isn’t uploaded.' },
    PARTIAL:  { label: 'Amount differs', tone: 'orange', hint: 'Wallet total and bank-settled total don’t match for this KO.' },
    EXCESS:   { label: 'Bank extra',     tone: 'purple', hint: 'Bank settled with no matching wallet withdrawal — usually the KO Limits file wasn’t uploaded for this date.' },
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

// Run + filter control row, shared shape.
function RunBar({ process, reconDate, setReconDate, onRun, running, children }) {
  const a = ACCENT[process] || ACCENT.p01
  return (
    <div className="flex items-end gap-3 mb-4 flex-wrap">
      <div>
        <label className="text-xs text-gray-400 block mb-1">Recon Date <span className="text-gray-300">(run batch)</span></label>
        <input type="date" className="input" value={reconDate} onChange={e => setReconDate(e.target.value)} />
      </div>
      <button onClick={onRun} disabled={running} className="btn-primary flex items-center gap-2" style={{ background: a.btn }}>
        <Play size={14} />{running ? 'Running…' : `Run ${process.toUpperCase()}`}
      </button>
      <div className="w-px h-9 bg-gray-200 mx-1" />
      {children}
    </div>
  )
}

// Disposition reason codes for unmatched SBI rows (mirrors backend SRC_CODES)
import { SRC_CODES } from '../srcCodes'
// Maker-checker: a queued action returns {queued:true, message}; toast "pending" not "done".
const mcQueued = (data) => {
  if (data?.queued) { toast(data.message || 'Pending approval by another user', { icon: '🕐' }); return true }
  return false
}

// ── Reusable modals ─────────────────────────────────────────────────────────────

function SrcModal({ process, procLabel, row, summary, onClose, onDone }) {
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
          {SRC_CODES.map(c => <option key={c} value={c}>{c}</option>)}
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

function P01Tab() {
  const [reconDate, setReconDate] = useState(today())
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
      toast.success(`P01 complete: ${r.summary.CREDITED || 0} settled, ${r.summary.PENDING || 0} awaiting bank`)
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

  // status counts for the cards (from the loaded rows — P01 returns the full set)
  const counts = {}
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

function P02Tab() {
  const [reconDate, setReconDate] = useState(today())
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
      toast.success(`P02: ${r.summary.Matched || 0} matched, ${r.summary.Unmatched || 0} unmatched, ${r.summary.Reversal || 0} reversals`)
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
                          {r.match_status === 'Reversal' && <div className="text-[10px] text-purple-500 mt-0.5">{drcr ? 'debit leg' : 'credit leg'}</div>}
                        </td>
                        {/* Bank side — always present */}
                        <td className="px-3 py-2 align-top">
                          <div className="leading-tight">
                            <div className="font-semibold tabular-nums">
                              <span className={drcr ? 'text-red-500' : 'text-green-600'}>{r.bank_type}</span> {fmtINR(r.bank_amount)}
                              {r.bank_txn_date && <span className="ml-2 text-[11px] font-normal text-gray-400">{r.bank_txn_date}</span>}
                            </div>
                            <div className="text-[11px] font-mono text-gray-500">{r.reference_number || '—'}</div>
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
                            : <span className="text-[11px] text-gray-300 italic">{r.match_status === 'Reversal' ? '— reversal, no report —' : '— no report match —'}</span>}
                        </td>
                        <td className="px-3 py-2 text-right align-top">{hasReport(r) ? <Delta a={r.bank_amount} b={r.report_amount} /> : <span className="text-gray-300">—</span>}</td>
                        <td className="px-3 py-2 text-center whitespace-nowrap align-top">
                          {r.match_status === 'Manual_Matched'
                            ? <button onClick={() => undoManualMatch(r, loadResults)} title={r.manual_remark || ''} className="text-[11px] text-amber-700 hover:underline">↩ undo</button>
                            : ((r.match_status || '').startsWith('Unmatched') || r.match_status === 'Partial' || r.match_status === 'Reversal')
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

function P03Tab() {
  const [reconDate, setReconDate] = useState(today())
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
                              <div className="text-[11px] text-gray-400">{r.txn_date || ''}{r.ref_number ? ` · ${r.ref_number}` : ''}</div>
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

function P04Tab() {
  const [reconDate, setReconDate] = useState(today())
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

function UnifiedTab() {
  const [reconDate, setReconDate] = useState(today())
  const [data, setData] = useState(null)
  const [side, setSide] = useState('')
  const [statusF, setStatusF] = useState('')
  const [procF, setProcF] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [srcModal, setSrcModal] = useState(null)
  const [mmModal, setMmModal] = useState(null)
  const PAGE_SIZE = 100

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

  const rows = data?.rows || []
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
                    <tr key={r.side + r.id} className="border-b border-gray-50 hover:bg-gray-50">
                      <td className="px-3 py-2"><span className={`text-[10px] px-1.5 py-0.5 rounded-full font-semibold ${r.side === 'bank' ? 'bg-blue-100 text-blue-700' : 'bg-green-100 text-green-700'}`}>{r.side}</span></td>
                      <td className="px-3 py-2 text-gray-600 whitespace-nowrap">{r.source}</td>
                      <td className="px-3 py-2">
                        <div className="font-mono text-gray-600">{r.ref || '—'}</div>
                        {r.ko_csp && <div className="text-[11px] text-gray-400 font-mono">{r.ko_csp}</div>}
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
                  {rows.length === 0 && <tr><td colSpan={9} className="text-center py-10 text-gray-400 text-sm">No entries — pick a date with SBI data.</td></tr>}
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
    </div>
  )
}

// ── Main Page ──────────────────────────────────────────────────────────────────

const PROCESSES = [
  { key: 'unified', label: '🔎 All Entries' },
  { key: 'p01', label: '💳 P01 · Wallet ↔ Settlement' },
  { key: 'p02', label: '📊 P02 · Bank ↔ Txn Report' },
  { key: 'p03', label: '🔄 P03 · Money Out ↔ In' },
  { key: 'p04', label: '⚖️ P04 · Wallet Balance' },
]

// Header quick actions: run all four processes for one date, and pull the
// Daily Recon Pack (overview + all processes + exceptions + logs) in one click.
function HeaderActions() {
  const [date, setDate] = useState(today())
  const [running, setRunning] = useState(false)
  const runAll = async () => {
    setRunning(true)
    try {
      const { data } = await api.post('/sbi/run/all', null, { params: { recon_date: date } })
      const ok  = Object.entries(data.results || {}).filter(([, v]) => !v.error).map(([k]) => k.toUpperCase())
      const bad = Object.entries(data.results || {}).filter(([, v]) => v.error)
      if (ok.length) toast.success(`Ran ${ok.join(' · ')} for ${date}`)
      bad.forEach(([k, v]) => toast.error(`${k.toUpperCase()}: ${v.error}`))
    } catch (e) { toast.error(e?.response?.data?.detail || 'Run failed') }
    finally { setRunning(false) }
  }
  const dailyPack = async () => {
    try {
      const r = await api.get('/sbi/report', { params: { type: 'daily_pack', recon_date: date }, responseType: 'blob' })
      const used = r.headers['x-recon-date']
      if (used === 'none') { toast('No SBI recon results yet — run P01–P04 first', { icon: 'ℹ️' }); return }
      const url = URL.createObjectURL(r.data)
      const a = document.createElement('a'); a.href = url; a.download = `sbi_daily_pack_${used}.xlsx`; a.click()
      URL.revokeObjectURL(url)
      if (used !== date) toast(`No data for ${date} — exported latest (${used})`, { icon: 'ℹ️' })
    } catch { toast.error('Report failed') }
  }
  return (
    <div className="flex items-end gap-2">
      <div>
        <label className="text-xs text-gray-400 block mb-1">Recon date</label>
        <input type="date" className="input" value={date} onChange={e => setDate(e.target.value)} />
      </div>
      <button onClick={runAll} disabled={running}
        className="btn-primary flex items-center gap-1.5 text-sm whitespace-nowrap">
        <Play size={14} className={running ? 'animate-pulse' : ''} />
        {running ? 'Running…' : 'Run all (P01→P04)'}
      </button>
      <button onClick={dailyPack} title="Overview + all four processes + exceptions + logs, one workbook"
        className="btn-ghost flex items-center gap-1.5 text-sm whitespace-nowrap">
        ⬇ Daily Pack
      </button>
    </div>
  )
}

export default function SBIKiosk() {
  const [tab, setTab] = useState('upload')
  const [uploadKey, setUploadKey] = useState(0)
  const onUploadDone = () => setUploadKey(k => k + 1)

  return (
    <div>
      <div className="flex items-center justify-between mb-5 flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-800">SBI Kiosk Banking Recon</h1>
          <p className="text-sm text-gray-400">
            Upload the four process files in the <span className="font-medium">Upload</span> tab, then run P01–P04. Each result reads <span className="font-medium">left source ↔ right source</span>.
          </p>
        </div>
        <HeaderActions />
      </div>

      <UploadStatus key={uploadKey} />

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

      {tab === 'unified' && <UnifiedTab />}
      {tab === 'p01' && <P01Tab />}
      {tab === 'p02' && <P02Tab />}
      {tab === 'p03' && <P03Tab />}
      {tab === 'p04' && <P04Tab />}
    </div>
  )
}
