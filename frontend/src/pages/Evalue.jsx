import React, { useState, useEffect, useRef } from 'react'
import {
  Wallet, Upload, Play, Download, RefreshCw, Building2, FileSpreadsheet,
  CheckCircle2, AlertTriangle, Banknote, Copy, FileWarning, GitMerge, Link2,
  Undo2, Tag, X, Clock, Coins, Plus, Trash2, Search, ArrowRight,
} from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'
import ModuleUploadCard from '../components/ModuleUploadCard'
import ActionModal from '../components/ActionModal'

const STATUS_META = {
  matched_online:   { label: 'Matched (Online)', cls: 'bg-green-50 text-green-700' },
  matched_cash:     { label: 'Matched (Cash)',   cls: 'bg-emerald-50 text-emerald-700' },
  matched_manual:   { label: 'Matched (Manual)', cls: 'bg-teal-50 text-teal-700' },
  interbank_matched:{ label: 'Interbank',        cls: 'bg-indigo-50 text-indigo-700' },
  twice_credit:     { label: 'Twice Credit',     cls: 'bg-orange-50 text-orange-700' },
  wrong_amount:     { label: 'Wrong Amount',     cls: 'bg-amber-50 text-amber-700' },
  unmatched_bank:   { label: 'Unmatched (Bank)', cls: 'bg-red-50 text-red-600' },
  unmatched_load:   { label: 'Unmatched (Load)', cls: 'bg-red-50 text-red-600' },
  transaction_fee:  { label: 'Txn Fee',          cls: 'bg-slate-100 text-slate-600' },
  bank_debit:       { label: 'Bank Debit',       cls: 'bg-slate-100 text-slate-600' },
  written_off:      { label: 'Written Off',      cls: 'bg-gray-200 text-gray-600' },
  adhoc_settlement: { label: 'Adhoc',            cls: 'bg-purple-50 text-purple-700' },
  under_review:     { label: 'Under Review',     cls: 'bg-yellow-50 text-yellow-700' },
  src_assigned:     { label: 'SRC Assigned',     cls: 'bg-yellow-50 text-yellow-700' },
  ignore:           { label: 'Ignored',          cls: 'bg-gray-100 text-gray-500' },
}
const OVERRIDE_STATUSES = ['written_off', 'twice_credit', 'wrong_amount', 'adhoc_settlement', 'under_review', 'ignore']
const SRC_CODES = ['UNCLAIMED', 'ADVANCE_CREDIT', 'BANK_CHARGES', 'TWICE_CREDITED', 'INTERNAL_TXN', 'DELAYED_TXN', 'DUPLICATE', 'MISSING_TID', 'OTHER']
const SRC_STATUSES = ['unmatched_bank', 'unmatched_load', 'twice_credit', 'wrong_amount', 'src_assigned']
const inr = n => '₹' + Number(n || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })
const isMatched = s => ['matched_online', 'matched_cash', 'matched_manual', 'interbank_matched'].includes(s)

export default function Evalue() {
  const [tab, setTab] = useState('upload')
  const [banks, setBanks] = useState([])
  const [selBank, setSelBank] = useState('')
  const [selAcct, setSelAcct] = useState('')
  const [summary, setSummary] = useState([])
  const [busy, setBusy] = useState(false)
  const [dates, setDates] = useState({ from: '', to: '' })
  const internalRef = useRef(); const bankRef = useRef()

  const dateParams = () => { const p = {}; if (dates.from) p.date_from = dates.from; if (dates.to) p.date_to = dates.to; return p }
  const loadAccounts = () => api.get('/evalue/accounts').then(({ data }) => setBanks(data.banks || [])).catch(() => toast.error('Failed to load accounts'))
  const loadSummary = () => api.get('/evalue/summary', { params: dateParams() })
    .then(({ data }) => setSummary(data.accounts || []))
    .catch((e) => { console.error('E-Value summary load failed', e); toast.error('Failed to load E-Value summary') })
  useEffect(() => { loadAccounts() }, [])
  useEffect(() => { loadSummary() }, [dates.from, dates.to])
  const accountsForBank = banks.find(b => b.bank_name === selBank)?.accounts || []

  const uploadInternal = async f => {
    if (!f) return
    setBusy(true); const fd = new FormData(); fd.append('file', f)
    try { const { data } = await api.post('/evalue/upload-internal', fd)
      toast.success(`Internal dump: ${data.rows} loads across ${data.accounts} accounts`); loadSummary()
    } catch (e) { toast.error(e.response?.data?.detail || 'Upload failed') }
    finally { setBusy(false) }
  }
  const uploadBank = async f => {
    if (!f) return
    if (!selBank || !selAcct) { toast.error('Select bank and account first'); return }
    setBusy(true); const fd = new FormData()
    fd.append('file', f); fd.append('bank_name', selBank); fd.append('reco_acc_no', selAcct)
    try { const { data } = await api.post('/evalue/upload-bank', fd)
      toast.success(`${data.account}: ${data.rows} rows (${data.credits} credits)`); loadSummary()
    } catch (e) { toast.error(e.response?.data?.detail || 'Upload failed') }
    finally { setBusy(false) }
  }
  const runOne = async acct => {
    setBusy(true)
    try { const { data } = await api.post(`/evalue/run-recon?reco_acc_no=${encodeURIComponent(acct)}`)
      toast.success(`${acct}: ${data.matched_online + data.matched_cash}/${data.bank_credits} matched (${data.match_rate}%)`); loadSummary()
    } catch (e) { toast.error(e.response?.data?.detail || 'Recon failed') } finally { setBusy(false) }
  }
  const runAll = async () => {
    setBusy(true)
    try { const { data } = await api.post('/evalue/run-recon-all')
      toast.success(`Reconciled ${data.accounts} account(s)`); loadSummary(); setTab('recon')
    } catch (e) { toast.error(e.response?.data?.detail || 'Recon failed') } finally { setBusy(false) }
  }

  const TABS = [
    { key: 'upload',   label: 'Upload', icon: Upload },
    { key: 'recon',    label: 'Reconciliation', icon: FileSpreadsheet },
    { key: 'manual',   label: 'Manual Match', icon: GitMerge },
    { key: 'ageing',   label: 'Ageing', icon: Clock },
    { key: 'recovery', label: 'Recovery', icon: Coins },
    { key: 'reports',  label: 'Reports', icon: Download },
    { key: 'interbank',label: 'Interbank', icon: Link2 },
  ]

  return (
    <div>
      <div className="flex items-center gap-2 mb-1">
        <Wallet size={20} className="text-primary" />
        <h1 className="text-xl font-bold text-gray-800">E-Value Reconciliation</h1>
      </div>
      <p className="text-sm text-gray-500 mb-5">Match CSP wallet-load requests against bank credits — online (UTR), cash (CDM/ATM), manual, and cross-product interbank transfers.</p>

      <div className="flex gap-2 mb-5 flex-wrap">
        {TABS.map(({ key, label, icon: Icon }) => (
          <button key={key} onClick={() => setTab(key)}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors
              ${tab === key ? 'bg-primary text-white' : 'bg-white border border-gray-200 text-gray-600 hover:bg-gray-50'}`}>
            <Icon size={15} />{label}
          </button>
        ))}
      </div>

      {tab === 'upload' && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-5 max-w-5xl">
          <ModuleUploadCard
            icon="🗂️" title="Internal Dump" color="cyan"
            desc="Simplibank E-Value load-in dump (SMB_BANK_LOADIN…csv) — covers all accounts"
            dateNote="Date auto-detected from each row"
            onUpload={uploadInternal} busy={busy} buttonLabel="Upload Internal Dump →"
          />
          <ModuleUploadCard
            icon="🏦" title="Bank Statement" color="cyan"
            desc="Select bank → account, then upload that account's statement"
            dateNote="Date auto-detected from each row"
            onUpload={uploadBank} busy={busy} disabled={!selAcct}
            buttonLabel="Upload Bank Statement →">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-xs text-gray-500 block mb-1">Bank</label>
                <select className="select" value={selBank} onChange={e => { setSelBank(e.target.value); setSelAcct('') }}>
                  <option value="">Select bank</option>
                  {banks.map(b => <option key={b.bank_name} value={b.bank_name}>{b.bank_name}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs text-gray-500 block mb-1">Account</label>
                <select className="select" value={selAcct} disabled={!selBank} onChange={e => setSelAcct(e.target.value)}>
                  <option value="">Select account</option>
                  {accountsForBank.map(a => <option key={a.reco_acc_no} value={a.reco_acc_no}>{a.reco_acc_no} · {a.account_number}</option>)}
                </select>
              </div>
            </div>
          </ModuleUploadCard>
          <div className="xl:col-span-2 flex items-center gap-3">
            <button onClick={runAll} disabled={busy} className="btn-primary flex items-center gap-2"><Play size={15} /> Run Reconciliation (all accounts)</button>
            <button onClick={() => { loadAccounts(); loadSummary() }} className="btn-ghost flex items-center gap-1.5"><RefreshCw size={14} /> Refresh</button>
          </div>
        </div>
      )}

      {tab === 'recon'     && <ResultsView summary={summary} busy={busy} runOne={runOne} refresh={loadSummary} dates={dates} setDates={setDates} />}
      {tab === 'manual'    && <ManualMatchView />}
      {tab === 'ageing'    && <AgeingView summary={summary} />}
      {tab === 'recovery'  && <RecoveryView />}
      {tab === 'reports'   && <ReportsView banks={banks} />}
      {tab === 'interbank' && <InterbankView summary={summary} refresh={loadSummary} />}
    </div>
  )
}

// ─── Reconciliation view (summary + drill-down + manual actions) ───────────────
function ResultsView({ summary, busy, runOne, refresh, dates = { from: '', to: '' }, setDates = () => {} }) {
  const [expanded, setExpanded] = useState(null)
  const [statusFilter, setStatusFilter] = useState('')
  const [rows, setRows] = useState([])
  const [loadingRows, setLoadingRows] = useState(false)
  const [showManual, setShowManual] = useState(null)   // reco_acc_no for manual matcher

  const dp = () => { const p = {}; if (dates.from) p.date_from = dates.from; if (dates.to) p.date_to = dates.to; return p }
  // Bank Statement + Wallet Loads are shown together in ONE table (no side toggle).
  const fetchRows = (acct, st) => {
    setLoadingRows(true)
    const p = { reco_acc_no: acct, status: st || undefined, ...dp() }
    Promise.all([
      api.get('/evalue/results', { params: { ...p, side: 'bank' } }),
      api.get('/evalue/results', { params: { ...p, side: 'load' } }),
    ]).then(([b, l]) => {
      const merged = [
        ...(b.data || []).map(r => ({ ...r, _side: 'bank' })),
        ...(l.data || []).map(r => ({ ...r, _side: 'load' })),
      ]
      merged.sort((a, c) => String(a.match_id || 'zzz').localeCompare(String(c.match_id || 'zzz')))
      setRows(merged)
    }).catch(() => setRows([])).finally(() => setLoadingRows(false))
  }
  const openAcct = acct => {
    if (expanded === acct) { setExpanded(null); return }
    setExpanded(acct); setStatusFilter(''); fetchRows(acct, '')
  }
  const reload = () => { if (expanded) fetchRows(expanded, statusFilter); refresh() }
  const exportAcct = acct => api.get(`/evalue/export`, { params: { reco_acc_no: acct, ...dp() }, responseType: 'blob' })
    .then(r => dl(r.data, `evalue_${acct}.xlsx`)).catch(() => toast.error('Export failed'))

  // Branded modal instead of browser prompts (audit F2)
  const [modal, setModal] = useState(null)
  const [modalBusy, setModalBusy] = useState(false)
  const unmatch = (matchId) => setModal({
    config: { title: 'Unmatch pair', danger: true, confirmLabel: 'Unmatch',
      description: `Break match ${matchId} — both rows go back to unmatched.`, fields: [] },
    action: async () => { await api.post(`/evalue/unmatch?match_id=${encodeURIComponent(matchId)}`); toast.success('Unmatched') },
  })
  const override = (row, sd) => setModal({
    config: { title: 'Override status', danger: true, confirmLabel: 'Override',
      description: `${row.eko_trxn_id || row.utr || row.id} — current status: ${row.recon_status}`,
      fields: [
        { name: 'status', label: 'New status', type: 'select', options: OVERRIDE_STATUSES, required: true, default: 'written_off' },
        { name: 'note', label: 'Reason / remark', required: true, minLength: 5, placeholder: 'Required — recorded in the audit trail' },
      ] },
    action: async (v) => { await api.post('/evalue/override-status', { id: row.id, side: row._side, status: v.status, note: v.note }); toast.success('Status updated') },
  })
  const assignSrc = (row) => setModal({
    config: { title: 'Assign SRC', confirmLabel: 'Assign',
      description: `${row.eko_trxn_id || row.utr || row.id} — current status: ${row.recon_status}${row.src_code ? ` · SRC: ${row.src_code}` : ''}`,
      fields: [
        { name: 'src_code', label: 'SRC code', type: 'select', options: SRC_CODES, required: true, default: row.src_code || 'UNCLAIMED' },
        { name: 'src_note', label: 'Note (optional)', placeholder: 'Optional — recorded with the SRC', default: row.src_note || '' },
      ] },
    action: async (v) => { const { data } = await api.post('/evalue/assign-src', { id: row.id, side: row._side, src_code: v.src_code, src_note: v.src_note }); toast.success(`SRC assigned: ${data.src_code}`) },
  })
  const runModal = async (v) => {
    setModalBusy(true)
    try { await modal.action(v); setModal(null); reload() }
    catch (e) { toast.error(e.response?.data?.detail || 'Action failed') }
    finally { setModalBusy(false) }
  }

  if (!summary.length) return (
    <div className="card text-center py-10 max-w-2xl"><FileSpreadsheet size={28} className="mx-auto text-gray-300 mb-2" />
      <p className="text-sm text-gray-500">No reconciled accounts yet. Upload statements and run reconciliation.</p></div>
  )

  const totals = summary.reduce((a, r) => ({
    credits: a.credits + r.bank_credits, matched: a.matched + r.matched_online + r.matched_cash + (r.matched_manual || 0) + (r.interbank_matched || 0),
    twice: a.twice + r.twice_credit, wrong: a.wrong + r.wrong_amount, unb: a.unb + r.unmatched_bank, unl: a.unl + r.unmatched_load,
  }), { credits: 0, matched: 0, twice: 0, wrong: 0, unb: 0, unl: 0 })

  return (
    <div className="max-w-5xl">
      <div className="grid grid-cols-3 md:grid-cols-6 gap-3 mb-4">
        {[
          { label: 'Bank Credits', value: totals.credits, icon: Banknote, color: 'text-gray-800' },
          { label: 'Matched', value: totals.matched, icon: CheckCircle2, color: 'text-green-600' },
          { label: 'Twice Credit', value: totals.twice, icon: Copy, color: 'text-orange-600' },
          { label: 'Wrong Amount', value: totals.wrong, icon: FileWarning, color: 'text-amber-600' },
          { label: 'Unmatched Bank', value: totals.unb, icon: AlertTriangle, color: 'text-red-600' },
          { label: 'Unmatched Load', value: totals.unl, icon: AlertTriangle, color: 'text-red-600' },
        ].map(({ label, value, icon: Icon, color }) => (
          <div key={label} className="bg-white rounded-lg border border-gray-100 p-3">
            <Icon size={15} className={color} /><div className={`text-lg font-bold mt-1 ${color}`}>{value}</div>
            <div className="text-[11px] text-gray-500">{label}</div>
          </div>
        ))}
      </div>
      <div className="flex items-center justify-end gap-2 mb-2 flex-wrap">
        <span className="text-xs text-gray-400">Date range:</span>
        <input type="date" title="From date" className="select text-xs py-1" value={dates.from} onChange={e => setDates(d => ({ ...d, from: e.target.value }))} />
        <span className="text-xs text-gray-400">→</span>
        <input type="date" title="To date" className="select text-xs py-1" value={dates.to} onChange={e => setDates(d => ({ ...d, to: e.target.value }))} />
        {(dates.from || dates.to) && <button onClick={() => setDates({ from: '', to: '' })} className="text-xs text-gray-400 hover:text-gray-600">clear</button>}
        <button onClick={refresh} className="btn-ghost flex items-center gap-1.5 text-xs"><RefreshCw size={13} /> Refresh</button>
      </div>

      <div className="space-y-2">
        {summary.map(r => (
          <div key={r.reco_acc_no} className="bg-white rounded-lg border border-gray-100">
            <div className="flex items-center justify-between gap-3 px-4 py-3 cursor-pointer hover:bg-gray-50" onClick={() => openAcct(r.reco_acc_no)}>
              <div className="flex items-center gap-3 min-w-0">
                <span className="font-semibold text-gray-800 text-sm w-24">{r.reco_acc_no}</span>
                <span className="text-xs text-gray-500">{r.bank_credits} credits · {r.loads} loads</span>
              </div>
              <div className="flex items-center gap-2 flex-wrap justify-end text-[11px]">
                <Pill n={r.matched_online + r.matched_cash + (r.matched_manual || 0) + (r.interbank_matched || 0)} s="matched_online" />
                <Pill n={r.twice_credit} s="twice_credit" /><Pill n={r.wrong_amount} s="wrong_amount" /><Pill n={r.unmatched_bank} s="unmatched_bank" />
                <span className={`font-bold ${r.match_rate >= 90 ? 'text-green-600' : r.match_rate >= 50 ? 'text-amber-600' : 'text-red-500'}`}>{r.match_rate}%</span>
                <button onClick={e => { e.stopPropagation(); setShowManual(showManual === r.reco_acc_no ? null : r.reco_acc_no) }} title="Manual match" className="p-1.5 rounded hover:bg-teal-50 text-teal-600"><GitMerge size={13} /></button>
                <button onClick={e => { e.stopPropagation(); runOne(r.reco_acc_no) }} disabled={busy} title="Re-run" className="p-1.5 rounded hover:bg-blue-50 text-blue-600"><Play size={13} /></button>
                <button onClick={e => { e.stopPropagation(); exportAcct(r.reco_acc_no) }} title="Export" className="p-1.5 rounded hover:bg-gray-100 text-gray-500"><Download size={13} /></button>
              </div>
            </div>

            {showManual === r.reco_acc_no && <ManualMatchPanel acct={r.reco_acc_no} onDone={() => { setShowManual(null); reload() }} />}

            {expanded === r.reco_acc_no && (
              <div className="border-t border-gray-100 p-3">
                <div className="flex items-center gap-2 mb-3 flex-wrap">
                  <select className="select w-44 text-xs py-1" value={statusFilter} onChange={e => { setStatusFilter(e.target.value); fetchRows(r.reco_acc_no, e.target.value) }}>
                    <option value="">All statuses</option>
                    {Object.entries(STATUS_META).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
                  </select>
                  <span className="text-[11px] text-gray-400">Bank statement &amp; wallet loads shown together · {rows.length} rows</span>
                </div>
                {loadingRows ? <p className="text-xs text-gray-400 py-4 text-center">Loading…</p>
                  : <RowsTable rows={rows} onUnmatch={unmatch} onOverride={override} onAssignSrc={assignSrc} />}
              </div>
            )}
          </div>
        ))}
      </div>

      <ActionModal open={!!modal} config={modal?.config} busy={modalBusy}
        onClose={() => setModal(null)} onSubmit={runModal} />
    </div>
  )
}

// ─── Manual match panel: two-pane unmatched bank credits vs unmatched loads ────
function ManualMatchPanel({ acct, onDone }) {
  const [bank, setBank] = useState([]); const [loads, setLoads] = useState([])
  const [selB, setSelB] = useState(null); const [selL, setSelL] = useState(null)
  const [busy, setBusy] = useState(false)
  const load = () => {
    api.get('/evalue/results', { params: { reco_acc_no: acct, side: 'bank', status: 'unmatched_bank' } }).then(({ data }) => setBank(data))
    api.get('/evalue/results', { params: { reco_acc_no: acct, side: 'load', status: 'unmatched_load' } }).then(({ data }) => setLoads(data))
  }
  useEffect(load, [acct])
  const doMatch = async () => {
    if (!selB || !selL) { toast.error('Select one bank credit and one load'); return }
    const bAmt = bank.find(x => x.id === selB)?.amount, lAmt = loads.find(x => x.id === selL)?.amount
    if (bAmt != null && lAmt != null && Math.abs(bAmt - lAmt) > 1 &&
        !window.confirm(`⚠ Amounts differ: bank ${inr(bAmt)} vs load ${inr(lAmt)}.\nThe pair will be linked but flagged "wrong_amount" — it will NOT count as matched. Continue?`)) return
    setBusy(true)
    try { const { data } = await api.post('/evalue/manual-match', { bank_txn_id: selB, load_id: selL })
      if (data.status === 'wrong_amount') toast(data.message, { icon: '⚠️' })
      else toast.success(data.cross_account ? 'Matched (cross-account)' : 'Matched')
      setSelB(null); setSelL(null); load(); onDone?.()
    } catch (e) { toast.error(e.response?.data?.detail || 'Failed') } finally { setBusy(false) }
  }
  return (
    <div className="border-t border-gray-100 bg-teal-50/40 p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-semibold text-teal-700">Manual Match — pick one unmatched bank credit + one unmatched load</span>
        <button onClick={doMatch} disabled={busy || !selB || !selL} className="btn-primary text-xs py-1 px-3 flex items-center gap-1"><GitMerge size={13} /> Match</button>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <MiniPick title="Unmatched bank credits" rows={bank} sel={selB} setSel={setSelB}
          render={b => `${b.txn_date} · ${inr(b.amount)} · ${b.channel} · ${b.utr || b.atm_ref || '—'}`} idKey="id" />
        <MiniPick title="Unmatched loads" rows={loads} sel={selL} setSel={setSelL}
          render={l => `${l.transaction_date} · ${inr(l.amount)} · ${l.csp_code || ''} · ${l.utr_number || '—'}`} idKey="id" />
      </div>
      <p className="text-[11px] text-gray-400 mt-2">Tip: loads from a different account also appear in their own account — use this for same-account pairs the engine missed; cross-account pairing is supported via the API.</p>
    </div>
  )
}
function MiniPick({ title, rows, sel, setSel, render, idKey }) {
  return (
    <div className="bg-white rounded border border-gray-200">
      <div className="px-2 py-1 text-[11px] font-semibold text-gray-500 border-b">{title} ({rows.length})</div>
      <div className="max-h-48 overflow-y-auto">
        {rows.length === 0 ? <div className="text-[11px] text-gray-400 p-3 text-center">none</div> :
          rows.map(r => (
            <div key={r[idKey]} onClick={() => setSel(r[idKey])}
              className={`px-2 py-1.5 text-[11px] cursor-pointer border-b border-gray-50 ${sel === r[idKey] ? 'bg-teal-100' : 'hover:bg-gray-50'}`}>
              {render(r)}
            </div>
          ))}
      </div>
    </div>
  )
}

// ─── Manual Match tab: cross-account pairing of unmatched bank credits + loads ──
// The per-account quick-match (ManualMatchPanel, on each Reconciliation row) only
// pairs WITHIN an account. This tab loads EVERY unmatched bank credit and load
// across all accounts (via the universal open-items window), so a credit can be
// paired with a load tagged to a DIFFERENT account (the account-mismatch case the
// cross-account reference pass exists for — behavior-contract #13). Queue + submit.
function ManualMatchView() {
  const [filters, setFilters] = useState({ date_from: '', date_to: '', acct: '' })
  const [bankItems, setBankItems] = useState([])
  const [loadItems, setLoadItems] = useState([])
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [queue, setQueue] = useState([])
  const [pendB, setPendB] = useState(null)
  const [pendL, setPendL] = useState(null)

  const loadItemsFn = async () => {
    setLoading(true)
    try {
      const p = { partner: 'evalue', page_size: 500,
        ...(filters.date_from ? { date_from: filters.date_from } : {}),
        ...(filters.date_to ? { date_to: filters.date_to } : {}) }
      const [b, l] = await Promise.all([
        api.get('/recon/open-items', { params: { ...p, side: 'bank', recon_status: 'unmatched_bank' } }),
        api.get('/recon/open-items', { params: { ...p, side: 'internal', recon_status: 'unmatched_load' } }),
      ])
      const acctF = (filters.acct || '').trim().toLowerCase()
      const byAcct = r => !acctF || String(r.tracking_number || '').toLowerCase().includes(acctF)
      setBankItems((b.data.items ?? []).filter(byAcct))
      setLoadItems((l.data.items ?? []).filter(byAcct))
      setPendB(null); setPendL(null); setQueue([])
    } catch { toast.error('Load failed') } finally { setLoading(false) }
  }

  const qB = new Set(queue.map(p => p.bank.id))
  const qL = new Set(queue.map(p => p.load.id))
  const addPair = () => {
    if (!pendB || !pendL) return toast.error('Select one bank credit and one load')
    if (Math.abs((pendB.amount ?? 0) - (pendL.amount ?? 0)) > 1 &&
        !window.confirm(`⚠ Amounts differ: bank ${inr(pendB.amount)} vs load ${inr(pendL.amount)}.\nThis pair will be linked but flagged "wrong_amount" — it will NOT count as matched. Queue it anyway?`)) return
    setQueue(q => [...q, { bank: pendB, load: pendL }]); setPendB(null); setPendL(null)
  }
  const submitAll = async () => {
    if (!queue.length) return toast.error('Queue is empty')
    setSubmitting(true)
    try {
      const res = await Promise.allSettled(queue.map(p =>
        api.post('/evalue/manual-match', { bank_txn_id: p.bank.id, load_id: p.load.id })))
      const okIdx = res.map(r => r.status === 'fulfilled')
      const ok = okIdx.filter(Boolean).length
      const flagged = res.filter(r => r.status === 'fulfilled' && r.value?.data?.status === 'wrong_amount').length
      const okB = new Set(queue.filter((_, i) => okIdx[i]).map(p => p.bank.id))
      const okL = new Set(queue.filter((_, i) => okIdx[i]).map(p => p.load.id))
      setBankItems(prev => prev.filter(i => !okB.has(i.id)))
      setLoadItems(prev => prev.filter(i => !okL.has(i.id)))
      setQueue([])
      toast.success(`Matched ${ok} pair(s)${flagged ? ` (${flagged} flagged wrong_amount)` : ''}${res.length - ok ? `, ${res.length - ok} failed` : ''}`)
    } catch { toast.error('Submit failed') } finally { setSubmitting(false) }
  }

  const xacct = pendB && pendL && pendB.tracking_number !== pendL.tracking_number
  const Panel = ({ title, items, sel, setSel, queued, color, isLoad }) => (
    <div className="card p-0 overflow-hidden">
      <div className={`px-4 py-2.5 bg-${color}-50 border-b border-${color}-100 flex items-center justify-between`}>
        <h3 className={`font-semibold text-${color}-800 text-sm`}>{title} ({items.length})</h3>
        {sel && <span className={`text-xs text-${color}-600 font-medium`}>1 selected</span>}
      </div>
      <div className="overflow-auto max-h-96">
        <table className="w-full text-xs">
          <thead className="bg-gray-50 sticky top-0">
            <tr className="text-gray-500 text-left">
              <th className="px-2 py-2 w-5"></th><th className="px-2 py-2">Account</th>
              <th className="px-2 py-2">Date</th><th className="px-2 py-2 text-right">Amount</th>
              <th className="px-2 py-2">{isLoad ? 'Eko TID / UTR' : 'UTR / Ref'}</th>
              <th className="px-2 py-2">{isLoad ? 'CSP' : 'Description'}</th>
            </tr>
          </thead>
          <tbody>
            {items.length === 0
              ? <tr><td colSpan={6} className="text-center py-8 text-gray-400">No items — Load first</td></tr>
              : items.map(it => {
                const isQ = queued.has(it.id), isS = sel?.id === it.id
                return (
                  <tr key={it.id} onClick={() => !isQ && setSel(isS ? null : it)}
                    className={`border-b border-gray-50 ${isQ ? 'bg-gray-100 opacity-50 cursor-not-allowed' : 'cursor-pointer'} ${isS ? `bg-${color}-100 ring-1 ring-inset ring-${color}-300` : `hover:bg-${color}-50`}`}>
                    <td className="px-2 py-2"><div className={`w-3.5 h-3.5 rounded-full border-2 ${isS ? `border-${color}-500 bg-${color}-500` : 'border-gray-300'}`} /></td>
                    <td className="px-2 py-2 font-mono text-gray-700">{it.tracking_number || '—'}</td>
                    <td className="px-2 py-2 text-gray-500">{it.transaction_date || '—'}</td>
                    <td className="px-2 py-2 text-right tabular-nums font-medium">{inr(it.amount)}</td>
                    <td className="px-2 py-2 font-mono text-gray-500">{it.eko_tid || it.utr_number || '—'}</td>
                    <td className="px-2 py-2 text-gray-500 max-w-[12rem]"><span className="block truncate" title={isLoad ? (it.csp_code || '') : (it.bank_description || '')}>{isLoad ? (it.csp_code || '—') : (it.bank_description || '—')}</span></td>
                  </tr>
                )
              })}
          </tbody>
        </table>
      </div>
    </div>
  )

  return (
    <div>
      <div className="card mb-4">
        <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
          <div><label className="text-xs text-gray-400 block mb-1">From Date</label>
            <input type="date" className="input" value={filters.date_from} onChange={e => setFilters({ ...filters, date_from: e.target.value })} /></div>
          <div><label className="text-xs text-gray-400 block mb-1">To Date</label>
            <input type="date" className="input" value={filters.date_to} onChange={e => setFilters({ ...filters, date_to: e.target.value })} /></div>
          <div><label className="text-xs text-gray-400 block mb-1">Account (filter)</label>
            <input className="input" placeholder="e.g. SBI-4393" value={filters.acct} onChange={e => setFilters({ ...filters, acct: e.target.value })} /></div>
          <div className="flex items-end">
            <button onClick={loadItemsFn} disabled={loading} className="btn-primary flex items-center gap-2"><Search size={14} /> {loading ? 'Loading…' : 'Load Unmatched'}</button>
          </div>
        </div>
        <p className="text-[11px] text-gray-400 mt-2">Pairs ANY unmatched bank credit with ANY unmatched load — including across accounts (flagged for audit). For same-account pairs the engine missed, the quick ⤳ on each Reconciliation row also works.</p>
      </div>

      {(pendB || pendL) && (
        <div className={`${xacct ? 'bg-amber-500' : 'bg-primary'} text-white rounded-xl px-4 py-3 mb-4 flex items-center justify-between`}>
          <div className="flex items-center gap-3 text-sm flex-wrap">
            <span>Bank: <b>{pendB ? `${pendB.tracking_number} · ${inr(pendB.amount)}` : '—'}</b></span>
            <ArrowRight size={16} />
            <span>Load: <b>{pendL ? `${pendL.tracking_number} · ${inr(pendL.amount)}` : '—'}</b></span>
            {xacct && <span className="text-xs bg-white/20 px-2 py-0.5 rounded">⚠ cross-account</span>}
          </div>
          <div className="flex gap-2">
            <button onClick={() => { setPendB(null); setPendL(null) }} className="text-white/70 hover:text-white text-xs flex items-center gap-1"><X size={14} /> Clear</button>
            <button onClick={addPair} disabled={!pendB || !pendL} className="bg-white text-primary text-xs px-3 py-1 rounded-lg font-medium hover:bg-white/90 disabled:opacity-50 flex items-center gap-1"><Plus size={14} /> Add to Queue</button>
          </div>
        </div>
      )}

      {queue.length > 0 && (
        <div className="card mb-4">
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-semibold text-gray-700 text-sm flex items-center gap-2"><GitMerge size={15} className="text-primary" /> Match Queue — {queue.length} pair(s)</h3>
            <button onClick={submitAll} disabled={submitting} className="btn-primary flex items-center gap-2 text-sm"><CheckCircle2 size={14} /> {submitting ? 'Submitting…' : `Submit All ${queue.length}`}</button>
          </div>
          <div className="space-y-1.5">
            {queue.map((p, i) => {
              const cross = p.bank.tracking_number !== p.load.tracking_number
              return (
                <div key={i} className="flex items-center gap-3 bg-primary/5 rounded-lg px-3 py-2 text-xs">
                  <span className="text-gray-500 w-4">{i + 1}.</span>
                  <span className="font-mono text-blue-700 flex-1">{p.bank.tracking_number} · ₹{p.bank.amount?.toLocaleString('en-IN')}</span>
                  <ArrowRight size={12} className="text-gray-400 shrink-0" />
                  <span className="font-mono text-green-700 flex-1">{p.load.tracking_number} · ₹{p.load.amount?.toLocaleString('en-IN')}</span>
                  {cross && <span className="text-[10px] text-amber-600 font-medium">cross-acct</span>}
                  <button onClick={() => setQueue(q => q.filter((_, j) => j !== i))} className="text-gray-400 hover:text-red-500"><Trash2 size={12} /></button>
                </div>
              )
            })}
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Panel title="Unmatched Bank Credits" items={bankItems} sel={pendB} setSel={setPendB} queued={qB} color="blue" isLoad={false} />
        <Panel title="Unmatched Wallet Loads" items={loadItems} sel={pendL} setSel={setPendL} queued={qL} color="green" isLoad={true} />
      </div>
    </div>
  )
}

function Pill({ n, s }) {
  if (!n) return null
  const m = STATUS_META[s] || { label: s, cls: 'bg-gray-100 text-gray-600' }
  return <span className={`px-2 py-0.5 rounded-full ${m.cls}`}>{m.label}: {n}</span>
}

function RowsTable({ rows, onUnmatch, onOverride, onAssignSrc }) {
  const [colF, setColF] = useState({ side: '', detail: '', ref: '', match_id: '', recon: '' })
  const _kw = (v) => (v || '').split(/[\s,]+/).map(s => s.trim().toLowerCase()).filter(Boolean)
  const _hit = (cell, q) => { const ks = _kw(q); return !ks.length || ks.some(k => String(cell ?? '').toLowerCase().includes(k)) }
  const colFActive = Object.values(colF).some(v => (v || '').trim())
  const fRows = rows.filter(r => {
    const isBank = r._side === 'bank'
    const detail = isBank ? `${r.channel || ''} ${r.description || ''}` : `${r.load_mode || ''} ${r.csp_code || ''} ${r.merchant_name || ''}`
    const ref = isBank ? (r.utr || r.atm_ref || '') : (r.eko_trxn_id || r.utr_number || '')
    return _hit(isBank ? 'bank' : 'load', colF.side) && _hit(detail, colF.detail) && _hit(ref, colF.ref) &&
           _hit(r.match_id, colF.match_id) && _hit(r.recon_status, colF.recon)
  })
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
        <tr className="border-b border-gray-100 text-gray-500">
          <th className="table-th">Side</th><th className="table-th">Date</th>
          <th className="table-th text-left">Detail</th><th className="table-th">Eko TID / UTR</th>
          <th className="table-th text-right">Amount</th><th className="table-th">Status</th>
          <th className="table-th">Recon ID</th><th className="table-th">Actions</th>
        </tr>
        <tr className="border-b border-gray-100 bg-gray-50/40">
          <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="bank/load…" value={colF.side} onChange={e => setColF(f => ({ ...f, side: e.target.value }))} /></th>
          <th className="px-1 py-1"></th>
          <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="detail…" value={colF.detail} onChange={e => setColF(f => ({ ...f, detail: e.target.value }))} /></th>
          <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="tid/utr…" value={colF.ref} onChange={e => setColF(f => ({ ...f, ref: e.target.value }))} /></th>
          <th className="px-1 py-1"></th>
          <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="status…" value={colF.recon} onChange={e => setColF(f => ({ ...f, recon: e.target.value }))} /></th>
          <th className="px-1 py-1"><input className="input text-[11px] py-0.5 w-full" placeholder="recon id…" value={colF.match_id} onChange={e => setColF(f => ({ ...f, match_id: e.target.value }))} /></th>
          <th className="px-1 py-1">{colFActive && <button onClick={() => setColF({ side: '', detail: '', ref: '', match_id: '', recon: '' })} className="text-[10px] text-gray-400 hover:text-gray-600">clear</button>}</th>
        </tr>
        </thead>
        <tbody>
          {fRows.length === 0 ? <tr><td colSpan="8" className="text-center text-gray-400 py-4">{rows.length ? 'No rows match the filters.' : 'No rows.'}</td></tr> : fRows.map((r, i) => {
            const m = STATUS_META[r.recon_status] || { label: r.recon_status, cls: 'bg-gray-100 text-gray-600' }
            const isBank = r._side === 'bank'
            const detail = isBank
              ? `${r.channel || ''}${r.description ? ' · ' + r.description : ''}`
              : `${r.load_mode || ''}${r.csp_code ? ' · ' + r.csp_code : ''}${r.merchant_name ? ' · ' + r.merchant_name : ''}`
            const ref = isBank ? (r.utr || r.atm_ref || '—') : (r.eko_trxn_id || r.utr_number || '—')
            return (
              <tr key={i} className="border-b border-gray-50 hover:bg-gray-50">
                <td className="table-td"><span className={`px-2 py-0.5 rounded-full text-[10px] ${isBank ? 'bg-blue-50 text-blue-700' : 'bg-violet-50 text-violet-700'}`}>{isBank ? 'Bank' : 'Load'}</span></td>
                <td className="table-td font-mono">{isBank ? r.txn_date : r.transaction_date}</td>
                <td className="table-td text-left max-w-xs truncate" title={detail}>{detail}</td>
                <td className="table-td font-mono">{ref}</td>
                <td className="table-td text-right tabular-nums">{inr(r.amount)}</td>
                <td className="table-td">
                  <div className="flex items-center justify-center gap-1 flex-wrap">
                    <span className={`px-2 py-0.5 rounded-full ${m.cls}`}>{m.label}</span>
                    {r.src_code && <span title={r.src_note || ''} className="px-2 py-0.5 rounded-full bg-yellow-50 text-yellow-700 text-[10px]">{r.src_code}</span>}
                  </div>
                </td>
                <td className="table-td font-mono text-primary">{r.match_id || '—'}</td>
                <td className="table-td">
                  <div className="flex items-center gap-1">
                    {isMatched(r.recon_status) && r.match_id &&
                      <button title="Unmatch" onClick={() => onUnmatch(r.match_id)} className="p-1 rounded hover:bg-red-50 text-red-500"><Undo2 size={12} /></button>}
                    <button title="Override status" onClick={() => onOverride(r)} className="p-1 rounded hover:bg-amber-50 text-amber-600"><Tag size={12} /></button>
                    {SRC_STATUSES.includes(r.recon_status) &&
                      <button title="Assign SRC" onClick={() => onAssignSrc(r)} className="p-1 rounded hover:bg-yellow-50 text-yellow-600"><Coins size={12} /></button>}
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function dl(blob, name) { const u = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = u; a.download = name; a.click(); URL.revokeObjectURL(u) }

// ─── Reports tab ───────────────────────────────────────────────────────────────
function ReportsView({ banks }) {
  const [types, setTypes] = useState([])
  const [report, setReport] = useState('summary')
  const [filters, setFilters] = useState({ from: '', to: '', bank: '', reco_acc_no: '' })
  const [summary, setSummary] = useState(null)
  useEffect(() => { api.get('/evalue/report/types').then(({ data }) => setTypes(data.types || [])).catch(() => {}) }, [])
  const allAccounts = banks.flatMap(b => b.accounts.map(a => ({ ...a, bank: b.bank_name })))
  const accountsForBank = filters.bank ? allAccounts.filter(a => a.bank === filters.bank) : allAccounts
  const upd = p => setFilters(f => ({ ...f, ...p }))
  const qp = () => { const p = new URLSearchParams(); Object.entries(filters).forEach(([k, v]) => v && p.append(k === 'from' ? 'from' : k, v)); return p }
  const loadSummary = () => api.get('/evalue/report/summary', { params: filters }).then(({ data }) => setSummary(data)).catch(() => toast.error('Failed'))
  const download = () => api.get(`/evalue/report/export?report=${report}&${qp()}`, { responseType: 'blob' })
    .then(r => dl(r.data, `evalue_${report}.xlsx`)).catch(() => toast.error('Export failed'))

  return (
    <div className="max-w-5xl">
      <div className="card mb-4">
        <h2 className="font-semibold text-gray-700 mb-3">E-Value Reports</h2>
        <div className="grid md:grid-cols-5 gap-3 mb-3">
          <div>
            <label className="text-xs text-gray-500 block mb-1">Report</label>
            <select className="select" value={report} onChange={e => setReport(e.target.value)}>
              {types.map(t => <option key={t.key} value={t.key}>{t.label}</option>)}
            </select>
          </div>
          <div><label className="text-xs text-gray-500 block mb-1">From</label><input type="date" className="input" value={filters.from} onChange={e => upd({ from: e.target.value })} /></div>
          <div><label className="text-xs text-gray-500 block mb-1">To</label><input type="date" className="input" value={filters.to} onChange={e => upd({ to: e.target.value })} /></div>
          <div>
            <label className="text-xs text-gray-500 block mb-1">Bank</label>
            <select className="select" value={filters.bank} onChange={e => upd({ bank: e.target.value, reco_acc_no: '' })}>
              <option value="">All</option>{banks.map(b => <option key={b.bank_name} value={b.bank_name}>{b.bank_name}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-500 block mb-1">Account</label>
            <select className="select" value={filters.reco_acc_no} onChange={e => upd({ reco_acc_no: e.target.value })}>
              <option value="">All</option>{accountsForBank.map(a => <option key={a.reco_acc_no} value={a.reco_acc_no}>{a.reco_acc_no}</option>)}
            </select>
          </div>
        </div>
        <div className="flex gap-2">
          <button onClick={download} className="btn-primary flex items-center gap-2"><Download size={15} /> Download Excel</button>
          <button onClick={loadSummary} className="btn-ghost flex items-center gap-1.5"><RefreshCw size={14} /> Preview Summary</button>
        </div>
      </div>

      {summary && (
        <div className="card">
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
            {[['Bank Credits', summary.grand.bank_credits], ['Credit Amount', inr(summary.grand.credit_amount)], ['Loads', summary.grand.loads],
              ['Matched', summary.grand.matched], ['Match Rate', summary.grand.match_rate + '%']].map(([l, v]) => (
              <div key={l} className="bg-gray-50 rounded-lg p-3 text-center"><div className="text-lg font-bold text-gray-800">{v}</div><div className="text-[11px] text-gray-500">{l}</div></div>
            ))}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead><tr className="border-b text-gray-500">
                <th className="table-th text-left">Account</th><th className="table-th text-right">Credits</th><th className="table-th text-right">Amount</th>
                <th className="table-th text-right">Online</th><th className="table-th text-right">Cash</th><th className="table-th text-right">Manual</th>
                <th className="table-th text-right">Interbank</th><th className="table-th text-right">Unmatched Bank</th><th className="table-th text-right">Rate</th>
              </tr></thead>
              <tbody>
                {summary.accounts.map(a => (
                  <tr key={a.reco_acc_no} className="border-b border-gray-50 hover:bg-gray-50">
                    <td className="table-td">{a.reco_acc_no}</td><td className="table-td text-right">{a.bank_credits}</td>
                    <td className="table-td text-right tabular-nums">{inr(a.credit_amount)}</td>
                    <td className="table-td text-right">{a.matched_online}</td><td className="table-td text-right">{a.matched_cash}</td>
                    <td className="table-td text-right">{a.matched_manual}</td><td className="table-td text-right">{a.interbank_matched}</td>
                    <td className="table-td text-right text-red-500">{a.unmatched_bank}</td>
                    <td className={`table-td text-right font-semibold ${a.match_rate >= 90 ? 'text-green-600' : a.match_rate >= 50 ? 'text-amber-600' : 'text-red-500'}`}>{a.match_rate}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      <p className="text-xs text-gray-400 mt-3">Reports: Summary, Matched Pairs, Unmatched (bank/load), Twice-Credit, Wrong-Amount, Fees &amp; Debits, Interbank, All Exceptions, and Full Classified Export. E-Value reports can also be scheduled for email delivery from <strong>Reports → Scheduled</strong> (types: E-Value Summary / Exceptions / Unmatched).</p>
    </div>
  )
}

// ─── Interbank tab ─────────────────────────────────────────────────────────────
function InterbankView({ summary, refresh }) {
  const [unmatched, setUnmatched] = useState([])
  const [acct, setAcct] = useState('')
  const [cands, setCands] = useState(null)   // {bank_txn, candidates}
  const [matches, setMatches] = useState([])
  const [busy, setBusy] = useState(false)

  const loadMatches = () => api.get('/evalue/interbank/list').then(({ data }) => setMatches(data.matches || [])).catch(() => {})
  const loadUnmatched = (a) => { if (!a) { setUnmatched([]); return }
    api.get('/evalue/results', { params: { reco_acc_no: a, side: 'bank', status: 'unmatched_bank' } }).then(({ data }) => setUnmatched(data)).catch(() => setUnmatched([]))
  }
  useEffect(() => { loadMatches() }, [])

  const scan = async () => {
    setBusy(true)
    try { const { data } = await api.post(`/evalue/interbank/scan${acct ? `?reco_acc_no=${encodeURIComponent(acct)}` : ''}`)
      toast.success(`Interbank scan: ${data.matched} matched of ${data.scanned_bank_entries} scanned`); loadMatches(); loadUnmatched(acct); refresh()
    } catch (e) { toast.error(e.response?.data?.detail || 'Scan failed') } finally { setBusy(false) }
  }
  const findCands = async (bankTxnId) => {
    try { const { data } = await api.get('/evalue/interbank/candidates', { params: { bank_txn_id: bankTxnId } }); setCands(data) }
    catch (e) { toast.error(e.response?.data?.detail || 'Failed') }
  }
  const link = async (txnId) => {
    try { const { data } = await api.post('/evalue/interbank/manual-match', { bank_txn_id: cands.bank_txn.id, transaction_id: txnId })
      if (data.amount_diff > 1) toast(data.message, { icon: '⚠️' })
      else toast.success('Interbank matched')
      setCands(null); loadMatches(); loadUnmatched(acct); refresh()
    } catch (e) { toast.error(e.response?.data?.detail || 'Failed') }
  }

  return (
    <div className="max-w-5xl space-y-4">
      <div className="card">
        <h2 className="font-semibold text-gray-700 mb-1 flex items-center gap-2"><Link2 size={16} /> Cross-Product Interbank Matching</h2>
        <p className="text-xs text-gray-400 mb-3">Match an unmatched E-Value bank entry to a fund-transfer / credit entry in another product's ledger (money moved between EKO accounts). Auto-match uses UTR + amount; or pick candidates manually.</p>
        <div className="flex items-end gap-3 flex-wrap">
          <div>
            <label className="text-xs text-gray-500 block mb-1">Account (optional)</label>
            <select className="select w-44" value={acct} onChange={e => { setAcct(e.target.value); loadUnmatched(e.target.value); setCands(null) }}>
              <option value="">All accounts</option>
              {summary.map(s => <option key={s.reco_acc_no} value={s.reco_acc_no}>{s.reco_acc_no}</option>)}
            </select>
          </div>
          <button onClick={scan} disabled={busy} className="btn-primary flex items-center gap-2"><Play size={15} /> Run Interbank Scan</button>
        </div>
      </div>

      {acct && (
        <div className="card">
          <h3 className="font-semibold text-gray-700 text-sm mb-2">Unmatched bank entries — {acct} ({unmatched.length})</h3>
          {unmatched.length === 0 ? <p className="text-xs text-gray-400">None.</p> : (
            <div className="overflow-x-auto"><table className="w-full text-xs">
              <thead><tr className="border-b text-gray-500"><th className="table-th">Date</th><th className="table-th text-right">Amount</th><th className="table-th">UTR</th><th className="table-th text-left">Description</th><th className="table-th"></th></tr></thead>
              <tbody>{unmatched.map(b => (
                <tr key={b.id} className="border-b border-gray-50 hover:bg-gray-50">
                  <td className="table-td font-mono">{b.txn_date}</td><td className="table-td text-right tabular-nums">{inr(b.amount)}</td>
                  <td className="table-td font-mono">{b.utr || '—'}</td><td className="table-td text-left max-w-xs truncate" title={b.description}>{b.description}</td>
                  <td className="table-td"><button onClick={() => findCands(b.id)} className="btn-ghost text-[11px] py-0.5 px-2">Find matches</button></td>
                </tr>
              ))}</tbody>
            </table></div>
          )}
        </div>
      )}

      {cands && (
        <div className="card border-indigo-200">
          <div className="flex items-center justify-between mb-2">
            <h3 className="font-semibold text-gray-700 text-sm">Candidates for {inr(cands.bank_txn.amount)} · {cands.bank_txn.utr || '—'} ({cands.bank_txn.reco_acc_no})</h3>
            <button onClick={() => setCands(null)} className="text-gray-400 hover:text-gray-600"><X size={16} /></button>
          </div>
          {cands.candidates.length === 0 ? <p className="text-xs text-gray-400">No candidate ledger entries found.</p> : (
            <div className="overflow-x-auto"><table className="w-full text-xs">
              <thead><tr className="border-b text-gray-500"><th className="table-th">Product</th><th className="table-th">Side</th><th className="table-th">Eko TID</th><th className="table-th">UTR</th><th className="table-th text-right">Amount</th><th className="table-th">Status</th><th className="table-th">Match on</th><th className="table-th"></th></tr></thead>
              <tbody>{cands.candidates.map(c => (
                <tr key={c.id} className="border-b border-gray-50 hover:bg-indigo-50/40">
                  <td className="table-td capitalize">{c.partner}</td><td className="table-td">{c.side}</td><td className="table-td font-mono">{c.eko_tid || '—'}</td>
                  <td className="table-td font-mono">{c.utr_number || c.tracking_number || '—'}</td><td className={`table-td text-right tabular-nums ${c.amount_ok ? '' : 'text-amber-600'}`}>{inr(c.amount)}</td>
                  <td className="table-td">{c.recon_status}</td><td className="table-td">{c.match_on}{c.amount_ok ? '' : ' (amt≠)'}</td>
                  <td className="table-td"><button onClick={() => link(c.id)} className="btn-primary text-[11px] py-0.5 px-2 flex items-center gap-1"><Link2 size={11} /> Link</button></td>
                </tr>
              ))}</tbody>
            </table></div>
          )}
        </div>
      )}

      <div className="card">
        <div className="flex items-center justify-between mb-2">
          <h3 className="font-semibold text-gray-700 text-sm">Interbank Matches ({matches.length})</h3>
          <button onClick={loadMatches} className="btn-ghost text-xs flex items-center gap-1"><RefreshCw size={12} /> Refresh</button>
        </div>
        {matches.length === 0 ? <p className="text-xs text-gray-400">No interbank matches yet.</p> : (
          <div className="overflow-x-auto"><table className="w-full text-xs">
            <thead><tr className="border-b text-gray-500"><th className="table-th">Match ID</th><th className="table-th">E-Value Acct</th><th className="table-th">Date</th><th className="table-th text-right">Amount</th><th className="table-th">UTR</th><th className="table-th text-left">Counterpart</th></tr></thead>
            <tbody>{matches.map(m => (
              <tr key={m.match_id} className="border-b border-gray-50 hover:bg-gray-50">
                <td className="table-td font-mono">{m.match_id}</td><td className="table-td">{m.reco_acc_no}</td><td className="table-td font-mono">{m.txn_date}</td>
                <td className="table-td text-right tabular-nums">{inr(m.amount)}</td><td className="table-td font-mono">{m.utr || '—'}</td>
                <td className="table-td text-left">{m.counterpart ? `${m.counterpart.partner}/${m.counterpart.side} · ${m.counterpart.eko_tid || ''}` : '—'}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </div>
    </div>
  )
}

// ─── Ageing tab ────────────────────────────────────────────────────────────────
function AgeingView({ summary }) {
  const [acct, setAcct] = useState('')
  const [data, setData] = useState(null)
  const load = () => api.get('/evalue/ageing', { params: { reco_acc_no: acct || undefined } })
    .then(({ data }) => setData(data)).catch(() => toast.error('Failed'))
  useEffect(() => { load() }, [acct])
  const BUCKETS = [['current', 'Current'], ['d1', 'D+1'], ['d3', 'D+2–3'], ['d7', 'D+4–7'], ['d7plus', 'D+7+']]
  return (
    <div className="max-w-5xl">
      <div className="flex items-center gap-3 mb-4">
        <h2 className="font-semibold text-gray-700 flex items-center gap-2"><Clock size={16} /> Ageing of Open Items</h2>
        <select className="select w-44" value={acct} onChange={e => setAcct(e.target.value)}>
          <option value="">All accounts</option>
          {summary.map(s => <option key={s.reco_acc_no} value={s.reco_acc_no}>{s.reco_acc_no}</option>)}
        </select>
        <button onClick={load} className="btn-ghost text-xs flex items-center gap-1"><RefreshCw size={12} /> Refresh</button>
      </div>
      {data && (
        <>
          <div className="grid grid-cols-5 gap-3 mb-5">
            {BUCKETS.map(([k, label], i) => {
              const b = data.buckets[k] || {}
              const red = k === 'd7plus'
              return (
                <div key={k} className={`rounded-lg border p-3 ${red ? 'border-red-200 bg-red-50' : 'border-gray-100 bg-white'}`}>
                  <div className="text-[11px] text-gray-500">{label}</div>
                  <div className={`text-lg font-bold ${red ? 'text-red-600' : 'text-gray-800'}`}>{(b.bank || 0) + (b.load || 0)}</div>
                  <div className="text-[10px] text-gray-400">bank {b.bank || 0} · load {b.load || 0}</div>
                </div>
              )
            })}
          </div>
          <div className="card">
            <h3 className="font-semibold text-gray-700 text-sm mb-2 flex items-center gap-2">
              <AlertTriangle size={14} className="text-red-500" /> Escalation — open beyond D+7 ({data.escalation_count})
            </h3>
            {data.escalation.length === 0 ? <p className="text-xs text-gray-400">Nothing aged beyond 7 days. 🎉</p> : (
              <div className="overflow-x-auto"><table className="w-full text-xs">
                <thead><tr className="border-b text-gray-500"><th className="table-th">Side</th><th className="table-th">Account</th><th className="table-th">Date</th><th className="table-th text-right">Age (d)</th><th className="table-th text-right">Amount</th><th className="table-th">Ref</th></tr></thead>
                <tbody>{data.escalation.map((e, i) => (
                  <tr key={i} className="border-b border-gray-50 hover:bg-red-50/40">
                    <td className="table-td">{e.side}</td><td className="table-td">{e.reco_acc_no}</td>
                    <td className="table-td font-mono">{e.date}</td><td className="table-td text-right font-bold text-red-600">{e.age_days}</td>
                    <td className="table-td text-right tabular-nums">{inr(e.amount)}</td>
                    <td className="table-td font-mono">{e.utr || e.csp_code || '—'}</td>
                  </tr>
                ))}</tbody>
              </table></div>
            )}
          </div>
        </>
      )}
    </div>
  )
}

// ─── Recovery tab (twice-credit / wrong-amount) ────────────────────────────────
function RecoveryView() {
  const [data, setData] = useState(null)
  const load = () => api.get('/evalue/recovery/list').then(({ data }) => setData(data)).catch(() => toast.error('Failed'))
  useEffect(() => { load() }, [])
  const mark = async (id, status) => {
    const note = status === 'recovered' ? (prompt('Recovery note (optional):') || '') : ''
    try { await api.post('/evalue/recovery/mark', { bank_txn_id: id, recovery_status: status, note }); toast.success('Updated'); load() }
    catch (e) { toast.error(e.response?.data?.detail || 'Failed') }
  }
  const RS = { recovery_pending: 'bg-amber-50 text-amber-700', recovered: 'bg-green-50 text-green-700', waived: 'bg-gray-100 text-gray-500' }
  return (
    <div className="max-w-5xl">
      <div className="flex items-center justify-between mb-3">
        <h2 className="font-semibold text-gray-700 flex items-center gap-2"><Coins size={16} /> Excess-Credit Recovery</h2>
        <button onClick={load} className="btn-ghost text-xs flex items-center gap-1"><RefreshCw size={12} /> Refresh</button>
      </div>
      {data && (
        <>
          <div className="grid grid-cols-3 gap-3 mb-4 max-w-lg">
            {[['Total exceptions', data.count], ['Pending recovery', data.pending], ['Pending amount', inr(data.pending_amount)]].map(([l, v]) => (
              <div key={l} className="bg-gray-50 rounded-lg p-3 text-center"><div className="text-lg font-bold text-gray-800">{v}</div><div className="text-[11px] text-gray-500">{l}</div></div>
            ))}
          </div>
          {data.items.length === 0 ? <p className="text-xs text-gray-400 card">No twice-credit / wrong-amount items.</p> : (
            <div className="card overflow-x-auto"><table className="w-full text-xs">
              <thead><tr className="border-b text-gray-500"><th className="table-th">Account</th><th className="table-th">Date</th><th className="table-th text-right">Amount</th><th className="table-th">Type</th><th className="table-th">UTR</th><th className="table-th">Recovery</th><th className="table-th">Action</th></tr></thead>
              <tbody>{data.items.map(it => (
                <tr key={it.id} className="border-b border-gray-50 hover:bg-gray-50">
                  <td className="table-td">{it.reco_acc_no}</td><td className="table-td font-mono">{it.txn_date}</td>
                  <td className="table-td text-right tabular-nums">{inr(it.amount)}</td>
                  <td className="table-td">{it.recon_status === 'twice_credit' ? 'Twice' : 'Wrong amt'}</td>
                  <td className="table-td font-mono">{it.utr || '—'}</td>
                  <td className="table-td"><span className={`px-2 py-0.5 rounded-full ${RS[it.recovery_status] || RS.recovery_pending}`}>{(it.recovery_status || 'recovery_pending').replace('_', ' ')}</span></td>
                  <td className="table-td">
                    <div className="flex gap-1">
                      <button onClick={() => mark(it.id, 'recovered')} className="text-[11px] px-2 py-0.5 rounded bg-green-50 text-green-700 hover:bg-green-100">Recovered</button>
                      <button onClick={() => mark(it.id, 'waived')} className="text-[11px] px-2 py-0.5 rounded bg-gray-100 text-gray-600 hover:bg-gray-200">Waive</button>
                    </div>
                  </td>
                </tr>
              ))}</tbody>
            </table></div>
          )}
        </>
      )}
    </div>
  )
}
