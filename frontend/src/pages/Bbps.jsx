import React, { useState, useEffect, useRef } from 'react'
import {
  Receipt, Upload, Play, Download, RefreshCw, FileSpreadsheet, CheckCircle2,
  AlertTriangle, RotateCcw, Undo2, Tag, Tags,
} from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'
import ModuleUploadCard from '../components/ModuleUploadCard'
import ActionModal from '../components/ActionModal'

const STATUS_META = {
  matched:               { label: 'Matched', cls: 'bg-green-50 text-green-700' },
  failed_refunded:       { label: 'Failed & Refunded', cls: 'bg-emerald-50 text-emerald-700' },
  failed_pending_refund: { label: 'Failed — Pending Refund', cls: 'bg-red-50 text-red-600' },
  refunded_but_success:  { label: 'Refunded but Success', cls: 'bg-orange-50 text-orange-700' },
  amount_mismatch:       { label: 'Amount Mismatch', cls: 'bg-amber-50 text-amber-700' },
  unmatched_internal:    { label: 'Unmatched (Internal)', cls: 'bg-red-50 text-red-600' },
  unmatched_bank:        { label: 'Unmatched (Operator)', cls: 'bg-red-50 text-red-600' },
  written_off:           { label: 'Written Off', cls: 'bg-gray-200 text-gray-600' },
  under_review:          { label: 'Under Review', cls: 'bg-yellow-50 text-yellow-700' },
}
const OVERRIDE = ['matched', 'failed_refunded', 'failed_pending_refund', 'refunded_but_success', 'written_off', 'under_review']
import { useSrcCodes } from '../srcCodes'
const SRC_ASSIGNABLE = ['unmatched_bank', 'unmatched_internal', 'failed_pending_refund', 'refunded_but_success', 'amount_mismatch', 'src_assigned']
const inr = n => '₹' + Number(n || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })
// Maker-checker: a queued action returns {queued:true, message}; toast "pending" not "done".
const mcQueued = (data) => {
  if (data?.queued) { toast(data.message || 'Pending approval by another user', { icon: '🕐' }); return true }
  return false
}

export default function Bbps() {
  const [tab, setTab] = useState('upload')
  const [summary, setSummary] = useState(null)
  const [busy, setBusy] = useState(false)
  const [dates, setDates] = useState({ from: '', to: '' })
  const intRef = useRef(); const bankRef = useRef()

  const dateParams = () => { const p = {}; if (dates.from) p.date_from = dates.from; if (dates.to) p.date_to = dates.to; return p }
  const loadSummary = () => api.get('/bbps/summary', { params: dateParams() })
    .then(({ data }) => setSummary(data))
    .catch((e) => { console.error('BBPS summary load failed', e); toast.error('Failed to load BBPS summary') })
  useEffect(() => { loadSummary() }, [dates.from, dates.to])

  // On a 409 [DUPLICATE] offer Re-upload (replace) with force=true — re-applying is
  // idempotent on both paths (internal = upsert per eko id, bank = replace per provider),
  // so it restores rows removed since without ever duplicating.
  const postWithForce = async (url, f, onOk) => {
    setBusy(true)
    const attempt = force => { const fd = new FormData(); fd.append('file', f); if (force) fd.append('force', 'true'); return api.post(url, fd) }
    try {
      let res
      try { res = await attempt(false) }
      catch (e) {
        const detail = e.response?.data?.detail || 'Upload failed'
        if (e.response?.status === 409 &&
            window.confirm(`${detail}\n\nRe-upload anyway and re-apply this file? Rows it already covers are replaced, not duplicated.`)) {
          res = await attempt(true)
        } else { toast.error(detail); return }
      }
      onOk(res.data)
    } catch (e) { toast.error(e.response?.data?.detail || 'Upload failed') } finally { setBusy(false) }
  }
  const uploadInternal = f => { if (!f) return; postWithForce('/bbps/upload-internal', f,
    data => { toast.success(`Internal: ${data.rows} txns (${Object.entries(data.by_provider).map(([k, v]) => `${k} ${v}`).join(', ')})`); loadSummary() }) }
  const uploadBank = f => { if (!f) return; postWithForce('/bbps/upload-bank', f,
    data => { toast.success(`${data.provider}: ${data.rows} rows (${data.success} success, ${data.failed} failed)`); loadSummary() }) }
  const runRecon = async () => {
    setBusy(true)
    try { const { data } = await api.post('/bbps/run-recon'); toast.success(`Reconciled — ${data.match_rate}% (${data.matched} matched, ${data.exceptions || 0} exceptions)`); loadSummary(); setTab('recon') }
    catch (e) { toast.error(e.response?.data?.detail || 'Recon failed') } finally { setBusy(false) }
  }
  const exportXlsx = () => api.get('/bbps/export', { responseType: 'blob', params: dateParams() })
    .then(r => { const u = URL.createObjectURL(r.data); const a = document.createElement('a'); a.href = u; a.download = 'bbps_recon.xlsx'; a.click(); URL.revokeObjectURL(u) }).catch(() => toast.error('Export failed'))

  const TABS = [{ key: 'upload', label: 'Upload', icon: Upload }, { key: 'recon', label: 'Reconciliation', icon: FileSpreadsheet }]
  return (
    <div>
      <div className="flex items-center gap-2 mb-1"><Receipt size={20} className="text-primary" /><h1 className="text-xl font-bold text-gray-800">BBPS Reconciliation</h1></div>
      <p className="text-sm text-gray-500 mb-5">Match Simplibank bill-payment / recharge transactions against the Moneyart &amp; Levin operator statements (eko_trxn_id ↔ ClientRef).</p>

      <div className="flex gap-2 mb-5">
        {TABS.map(({ key, label, icon: Icon }) => (
          <button key={key} onClick={() => setTab(key)} className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${tab === key ? 'bg-primary text-white' : 'bg-white border border-gray-200 text-gray-600 hover:bg-gray-50'}`}>
            <Icon size={15} />{label}
          </button>
        ))}
      </div>

      {tab === 'upload' && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-5 max-w-5xl">
          <ModuleUploadCard
            icon="🗂️" title="Simplibank Dump" color="purple"
            desc="Internal BBPS dump (SMB_…_UR…csv) — refunds (DR+CR) collapsed per transaction"
            dateNote="Date auto-detected from each row"
            onUpload={uploadInternal} busy={busy} buttonLabel="Upload Simplibank Dump →"
          />
          <ModuleUploadCard
            icon="🧾" title="Operator Statement" color="purple"
            desc="Moneyart TransactionReport or Levin UtilityReport — provider auto-detected"
            dateNote="Provider & date auto-detected from the file"
            onUpload={uploadBank} busy={busy} buttonLabel="Upload Operator Statement →"
          />
          <div className="xl:col-span-2 flex gap-3">
            <button onClick={runRecon} disabled={busy} className="btn-primary flex items-center gap-2"><Play size={15} /> Run Reconciliation</button>
            <button onClick={loadSummary} className="btn-ghost flex items-center gap-1.5"><RefreshCw size={14} /> Refresh</button>
          </div>
        </div>
      )}

      {tab === 'recon' && <ReconView summary={summary} refresh={loadSummary} exportXlsx={exportXlsx} runRecon={runRecon} busy={busy} dates={dates} setDates={setDates} />}
    </div>
  )
}

function ReconView({ summary, refresh, exportXlsx, runRecon, busy, dates, setDates }) {
  const srcCodes = useSrcCodes()
  const [status, setStatus] = useState('')
  const [provider, setProvider] = useState('')
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(false)
  // Per-column keyword filters (one or many keywords, comma/space separated)
  const [colF, setColF] = useState({ side: '', provider: '', eko: '', match_id: '', recon: '' })
  const _kw = (v) => (v || '').split(/[\s,]+/).map(s => s.trim().toLowerCase()).filter(Boolean)
  const _hit = (cell, q) => { const ks = _kw(q); return !ks.length || ks.some(k => String(cell ?? '').toLowerCase().includes(k)) }
  const colFActive = Object.values(colF).some(v => (v || '').trim())

  // Operator + Internal are shown together in ONE table (no side toggle).
  const fetchRows = () => {
    setLoading(true)
    const p = { status: status || undefined, provider: provider || undefined, date_from: dates.from || undefined, date_to: dates.to || undefined }
    Promise.all([
      api.get('/bbps/results', { params: { ...p, side: 'bank' } }),
      api.get('/bbps/results', { params: { ...p, side: 'internal' } }),
    ]).then(([b, i]) => {
      const merged = [
        ...(b.data || []).map(r => ({ ...r, _side: 'bank' })),
        ...(i.data || []).map(r => ({ ...r, _side: 'internal' })),
      ]
      // group matched pairs together by recon id, then by date
      merged.sort((a, c) => String(a.match_id || 'zzz').localeCompare(String(c.match_id || 'zzz')) || String(c.transaction_date || '').localeCompare(String(a.transaction_date || '')))
      setRows(merged)
    }).catch(() => setRows([])).finally(() => setLoading(false))
  }
  useEffect(() => { fetchRows() }, [status, provider, dates.from, dates.to])

  // Branded modal instead of browser prompts (audit F2)
  const [modal, setModal] = useState(null)
  const [modalBusy, setModalBusy] = useState(false)

  // Multi-select delete (keys are `${side}:${id}` since the table merges both sides).
  // Routes through /upload/delete-rows: recycle-binned + matched counterparts reverted.
  const [sel, setSel] = useState(() => new Set())
  const selKey = r => `${r._side}:${r.id}`
  const toggleSel = r => setSel(s => { const n = new Set(s); const k = selKey(r); n.has(k) ? n.delete(k) : n.add(k); return n })
  const deleteSelected = async () => {
    if (!sel.size) return
    if (!window.confirm(`Delete ${sel.size} selected row(s)?\n\nMatched counterparts are safely reverted to unmatched, and the rows go to the Recycle Bin (restorable for 30 days).`)) return
    try {
      let deleted = 0, reverted = 0
      for (const side of ['bank', 'internal']) {
        const ids = [...sel].filter(k => k.startsWith(side + ':')).map(k => k.slice(side.length + 1))
        if (!ids.length) continue
        const fd = new FormData()
        fd.append('module', 'bbps'); fd.append('table', side); fd.append('row_ids', ids.join(','))
        const { data } = await api.post('/upload/delete-rows', fd)
        deleted += data.deleted || 0; reverted += data.counterparts_unmatched || 0
      }
      toast.success(`Deleted ${deleted} row(s)${reverted ? ` · ${reverted} counterpart(s) reverted` : ''} — recoverable from the Recycle Bin`, { duration: 6000 })
      setSel(new Set()); fetchRows(); refresh()
    } catch (e) { toast.error(e.response?.data?.detail || 'Delete failed (needs the Clear/Delete Data permission)') }
  }
  const unmatch = (mid) => setModal({
    config: { title: 'Unmatch pair', danger: true, confirmLabel: 'Unmatch',
      description: `Break match ${mid} — both rows go back to unmatched.`, fields: [] },
    action: async () => { const { data } = await api.post(`/bbps/unmatch?match_id=${encodeURIComponent(mid)}`); if (!mcQueued(data)) toast.success('Unmatched') },
  })
  const override = (row) => setModal({
    config: { title: 'Override status', danger: true, confirmLabel: 'Override',
      description: `${row.client_ref || row.eko_trxn_id || row.id} — current status: ${row.recon_status}`,
      fields: [
        { name: 'status', label: 'New status', type: 'select', options: OVERRIDE, required: true, default: 'written_off' },
        { name: 'note', label: 'Reason', required: true, minLength: 10, placeholder: 'Why is this being overridden? (min 10 chars)' },
      ] },
    action: async (v) => { const { data } = await api.post('/bbps/override-status', { id: row.id, side: row._side, status: v.status, note: v.note }); if (!mcQueued(data)) toast.success('Status updated') },
  })
  const assignSrc = (row) => setModal({
    config: { title: 'Assign SRC', confirmLabel: 'Assign',
      description: `${row.client_ref || row.eko_trxn_id || row.id} — current status: ${row.recon_status}`,
      fields: [
        { name: 'src_code', label: 'SRC code', type: 'select', options: srcCodes, required: true, default: row.src_code || 'UNCLAIMED' },
        { name: 'src_note', label: 'Note (optional)', placeholder: 'Optional context for this SRC assignment' },
      ] },
    action: async (v) => { const { data } = await api.post('/bbps/assign-src', { id: row.id, side: row._side, src_code: v.src_code, src_note: v.src_note }); if (!mcQueued(data)) toast.success(`SRC assigned: ${data.src_code}`) },
  })
  const removeSrc = async (row) => {
    if (!window.confirm(`Remove SRC from ${row.client_ref || row.eko_trxn_id || row.id}?\n\nIt reverts to the status it held before the tag.`)) return
    try { const { data } = await api.post('/bbps/remove-src', { id: row.id, side: row._side }); if (!mcQueued(data)) toast.success('SRC removed'); fetchRows(); refresh() }
    catch (e) { toast.error(e.response?.data?.detail || 'Remove failed') }
  }
  const runModal = async (v) => {
    setModalBusy(true)
    try { await modal.action(v); setModal(null); fetchRows(); refresh() }
    catch (e) { toast.error(e.response?.data?.detail || 'Action failed') }
    finally { setModalBusy(false) }
  }

  if (!summary) return <div className="card text-center py-10 max-w-2xl"><FileSpreadsheet size={28} className="mx-auto text-gray-300 mb-2" /><p className="text-sm text-gray-500">Upload the dump + an operator statement, then run reconciliation.</p></div>
  const o = summary.overall
  return (
    <div className="max-w-5xl">
      <div className="grid grid-cols-3 md:grid-cols-6 gap-3 mb-4">
        {[
          { l: 'Operator Rows', v: o.bank_rows, c: 'text-gray-800', i: Receipt },
          { l: 'Matched', v: o.matched, c: 'text-green-600', i: CheckCircle2 },
          { l: 'Failed & Refunded', v: o.failed_refunded, c: 'text-emerald-600', i: RotateCcw },
          { l: 'Exceptions', v: o.exceptions, c: 'text-red-600', i: AlertTriangle },
          { l: 'Unmatched Op.', v: o.unmatched_bank, c: 'text-red-500', i: AlertTriangle },
          { l: 'Unmatched Int.', v: o.unmatched_internal, c: 'text-red-500', i: AlertTriangle },
        ].map(({ l, v, c, i: I }) => (
          <div key={l} className="bg-white rounded-lg border border-gray-100 p-3"><I size={15} className={c} /><div className={`text-lg font-bold mt-1 ${c}`}>{v}</div><div className="text-[11px] text-gray-500">{l}</div></div>
        ))}
      </div>
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <div className="flex items-center gap-2">
          {summary.by_provider.map(p => (
            <span key={p.provider} className="text-xs px-3 py-1 rounded-full bg-blue-50 text-blue-700 capitalize">{p.provider}: <strong>{p.match_rate}%</strong> ({p.reconciled}/{p.bank_rows})</span>
          ))}
          <span className={`text-sm font-bold ${o.match_rate >= 95 ? 'text-green-600' : o.match_rate >= 80 ? 'text-amber-600' : 'text-red-500'}`}>Overall {o.match_rate}%</span>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <input type="date" title="From date" className="select text-xs py-1" value={dates.from} onChange={e => setDates(d => ({ ...d, from: e.target.value }))} />
          <span className="text-xs text-gray-400">→</span>
          <input type="date" title="To date" className="select text-xs py-1" value={dates.to} onChange={e => setDates(d => ({ ...d, to: e.target.value }))} />
          {(dates.from || dates.to) && <button onClick={() => setDates({ from: '', to: '' })} className="text-xs text-gray-400 hover:text-gray-600">clear</button>}
          <button onClick={runRecon} disabled={busy} className="btn-ghost text-xs flex items-center gap-1"><Play size={12} /> Re-run</button>
          <button onClick={exportXlsx} className="btn-ghost text-xs flex items-center gap-1"><Download size={12} /> Export</button>
        </div>
      </div>

      <div className="card">
        <div className="flex items-center gap-2 mb-3 flex-wrap">
          <select className="select w-52 text-xs py-1" value={status} onChange={e => setStatus(e.target.value)}><option value="">All statuses</option>{Object.entries(STATUS_META).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}</select>
          <select className="select w-32 text-xs py-1" value={provider} onChange={e => setProvider(e.target.value)}><option value="">All providers</option><option value="moneyart">Moneyart</option><option value="levin">Levin</option></select>
          <span className="text-[11px] text-gray-400">Operator &amp; Internal shown together · {rows.length} rows</span>
          {sel.size > 0 && (
            <span className="flex items-center gap-2 ml-auto">
              <span className="text-xs font-medium text-primary">{sel.size} selected</span>
              <button onClick={deleteSelected} className="px-3 py-1 rounded text-xs font-medium bg-red-600 text-white hover:bg-red-700">Delete selected</button>
              <button onClick={() => setSel(new Set())} className="text-xs text-gray-500 hover:text-gray-700">Clear</button>
            </span>
          )}
        </div>
        {(() => { const fRows = rows.filter(r =>
          _hit(r._side === 'bank' ? 'operator' : 'internal', colF.side) && _hit(r.provider, colF.provider) &&
          _hit(r.client_ref || r.eko_trxn_id, colF.eko) && _hit(r.match_id, colF.match_id) && _hit(r.recon_status, colF.recon)); return (
        loading ? <p className="text-xs text-gray-400 py-4 text-center">Loading…</p> : (
          <div className="overflow-x-auto"><table className="w-full text-xs">
            <thead>
            <tr className="border-b text-gray-500">
              <th className="table-th w-7"><input type="checkbox"
                checked={fRows.length > 0 && fRows.every(r => sel.has(selKey(r)))}
                onChange={() => setSel(s => { const all = fRows.every(r => s.has(selKey(r))); const n = new Set(s); fRows.forEach(r => all ? n.delete(selKey(r)) : n.add(selKey(r))); return n })} /></th>
              <th className="table-th">Side</th><th className="table-th">Provider</th><th className="table-th">Eko TID</th>
              <th className="table-th text-right">Amount</th><th className="table-th">Status</th><th className="table-th">Refunded</th>
              <th className="table-th">Date</th><th className="table-th">Recon ID</th><th className="table-th">Recon Status</th><th className="table-th">Actions</th>
            </tr>
            <tr className="border-b border-gray-100 bg-gray-50/40">
              <th className="px-1 py-1"></th>
              <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="op/int…" value={colF.side} onChange={e => setColF(f => ({ ...f, side: e.target.value }))} /></th>
              <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="provider…" value={colF.provider} onChange={e => setColF(f => ({ ...f, provider: e.target.value }))} /></th>
              <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="eko tid…" value={colF.eko} onChange={e => setColF(f => ({ ...f, eko: e.target.value }))} /></th>
              <th className="px-1 py-1"></th><th className="px-1 py-1"></th><th className="px-1 py-1"></th><th className="px-1 py-1"></th>
              <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="recon id…" value={colF.match_id} onChange={e => setColF(f => ({ ...f, match_id: e.target.value }))} /></th>
              <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="status…" value={colF.recon} onChange={e => setColF(f => ({ ...f, recon: e.target.value }))} /></th>
              <th className="px-1 py-1">{colFActive && <button onClick={() => setColF({ side: '', provider: '', eko: '', match_id: '', recon: '' })} className="text-[10px] text-gray-400 hover:text-gray-600">clear</button>}</th>
            </tr>
            </thead>
            <tbody>{fRows.length === 0 ? <tr><td colSpan="11" className="text-center text-gray-400 py-4">{rows.length ? 'No rows match the column filters.' : 'No rows.'}</td></tr> : fRows.map((r, i) => {
              const m = STATUS_META[r.recon_status] || { label: r.recon_status, cls: 'bg-gray-100 text-gray-600' }
              return (
                <tr key={i} className="border-b border-gray-50 hover:bg-gray-50">
                  <td className="table-td"><input type="checkbox" checked={sel.has(selKey(r))} onChange={() => toggleSel(r)} /></td>
                  <td className="table-td"><span className={`px-2 py-0.5 rounded-full text-[10px] ${r._side === 'bank' ? 'bg-blue-50 text-blue-700' : 'bg-violet-50 text-violet-700'}`}>{r._side === 'bank' ? 'Operator' : 'Internal'}</span></td>
                  <td className="table-td capitalize">{r.provider}</td>
                  <td className="table-td font-mono">{r.client_ref || r.eko_trxn_id || '—'}</td>
                  <td className="table-td text-right tabular-nums">{inr(r.amount)}</td>
                  <td className="table-td">{r.status}</td>
                  <td className="table-td">{r._side === 'internal' ? (r.is_refunded ? 'Yes' : '—') : '—'}</td>
                  <td className="table-td font-mono">{r.transaction_date}</td>
                  <td className="table-td font-mono text-primary">{r.match_id || '—'}</td>
                  <td className="table-td"><div className="flex items-center gap-1 flex-wrap">
                    <span className={`px-2 py-0.5 rounded-full ${m.cls}`}>{m.label}</span>
                    {r.src_code && <span title={r.src_note || ''} className="px-2 py-0.5 rounded-full bg-yellow-100 text-yellow-800 font-medium">{r.src_code}</span>}
                  </div></td>
                  <td className="table-td"><div className="flex gap-1">
                    {r.match_id && <button title="Unmatch" onClick={() => unmatch(r.match_id)} className="p-1 rounded hover:bg-red-50 text-red-500"><Undo2 size={12} /></button>}
                    <button title="Override" onClick={() => override(r)} className="p-1 rounded hover:bg-amber-50 text-amber-600"><Tag size={12} /></button>
                    {SRC_ASSIGNABLE.includes(r.recon_status) && <button title="Assign SRC" onClick={() => assignSrc(r)} className="p-1 rounded hover:bg-yellow-50 text-yellow-700"><Tags size={12} /></button>}
                    {r.src_code && <button title="Remove SRC" onClick={() => removeSrc(r)} className="p-1 rounded hover:bg-gray-100 text-gray-500 text-[11px] font-bold leading-none">✕</button>}
                  </div></td>
                </tr>
              )
            })}</tbody>
          </table></div>
        )) })()}
      </div>

      <ActionModal open={!!modal} config={modal?.config} busy={modalBusy}
        onClose={() => setModal(null)} onSubmit={runModal} />
    </div>
  )
}
