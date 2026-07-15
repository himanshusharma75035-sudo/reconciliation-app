import React, { useState, useEffect } from 'react'
import { DMT_PARTNERS } from '../productRegistry'
import { Play, Zap, CheckCircle, RefreshCw, ArrowLeftRight, ExternalLink, Info } from 'lucide-react'
import { Link } from 'react-router-dom'
import toast from 'react-hot-toast'
import api from '../utils/api'

// ── Product catalogue for Run Recon ──────────────────────────────────────────
// Grouped so the partner dropdown reads cleanly as products grew. Each product
// routes to the right engine: core DMT/ledger products run through /recon, while
// module products (BBPS, E-Value) call their own run endpoints. SBI is a 4-process
// recon and is run from its dedicated window.
const GROUPS = [
  { label: 'DMT Banks',     slugs: DMT_PARTNERS },
  { label: 'Core Products', slugs: ['aeps', 'pg', 'qr', 'digikhata', 'indonepal'] },
  { label: 'Modules',       slugs: ['bbps', 'evalue', 'sbi'] },
]
const DEFAULT_LABEL = {
  fino: 'Fino Payments Bank', airtel: 'Airtel Payments Bank', axis: 'Axis Bank', levin: 'Levin',
  aeps: 'AePS Cashout', pg: 'Accept Payment (PG)', qr: 'QR Collection',
  digikhata: 'Digikhata (PPI)', indonepal: 'Indo-Nepal',
  bbps: 'BBPS', evalue: 'E-Value', sbi: 'SBI Kiosk',
}
const ALL_SLUGS = GROUPS.flatMap(g => g.slugs)

// Which engine a product uses.
function productKind(slug) {
  if (slug === 'bbps')   return 'bbps'
  if (slug === 'evalue') return 'evalue'
  if (slug === 'sbi' || slug === 'kiosk') return 'sbi'
  return 'core'
}

// Fixed rule descriptions used when the live rule list can't be fetched
// (non-admins can't read /admin/match-rules) or for module engines.
const STATIC_RULES = {
  fino:   ['TID + Tracking + UTR', 'TID + Tracking', 'TID + UTR', 'TID Only', 'Tracking Only (IMPS fallback)', 'UTR Only (NEFT fallback)'],
  _core:  ['TID + Tracking Number', 'TID Only', 'Tracking Number Only'],
  bbps:   ['Eko TID ↔ Operator ClientRef (provider-aware)', 'Amount + status cross-check', 'Failed / refund exception flags'],
  evalue: ['Online: UTR / ATM ref match', 'Cash / CDM: amount + date match', 'Twice-credit & wrong-amount detection'],
  sbi:    ['P01 Settlement · P02 Bank↔Txn · P03 CSP↔Txn↔Bank · P04 Wallet balance'],
}

export default function RunRecon() {
  const [partner, setPartner]   = useState('fino')
  const [dateMode, setDateMode] = useState('single')          // 'single' | 'range'
  const [reconDate, setReconDate] = useState(today())
  const [range, setRange]       = useState({ date_from: '', date_to: '' })
  const [labels, setLabels]     = useState(DEFAULT_LABEL)     // slug → display name

  const [running, setRunning]                 = useState(false)
  const [runningNeft, setRunningNeft]         = useState(false)
  const [runningInternal, setRunningInternal] = useState(false)
  const [runningInterbank, setRunningInterbank] = useState(false)

  const [result, setResult]           = useState(null)        // core single
  const [rangeResult, setRangeResult] = useState(null)        // core range
  const [moduleResult, setModuleResult] = useState(null)      // bbps / evalue

  const [interbankResult, setInterbankResult] = useState(null)
  const [interbankMatches, setInterbankMatches] = useState([])
  const [loadingIBT, setLoadingIBT]   = useState(false)
  const [runs, setRuns]               = useState([])

  const [rules, setRules]             = useState([])          // live rule names for the selected partner

  function today() { return new Date().toISOString().split('T')[0] }
  const kind   = productKind(partner)
  const isCore = kind === 'core'
  const labelOf = (s) => labels[s] || DEFAULT_LABEL[s] || s

  // ── Loaders ────────────────────────────────────────────────────────────────
  const loadRuns = async () => { try { const { data } = await api.get('/recon/runs'); setRuns(data) } catch {} }
  const loadInterbankMatches = async () => {
    setLoadingIBT(true)
    try { const { data } = await api.get('/recon/interbank-matches'); setInterbankMatches(data.matches || []) }
    catch {} finally { setLoadingIBT(false) }
  }

  useEffect(() => {
    document.title = 'Run Recon | Eko Recon'
    return () => { document.title = 'Eko Bharat Ventures — Recon' }
  }, [])

  useEffect(() => {
    loadRuns()
    loadInterbankMatches()
    // enrich labels with branded display names from the partner registry
    api.get('/admin/partners-public').then(({ data }) => {
      const map = { ...DEFAULT_LABEL }
      ;(data || []).forEach(p => { if (p.slug) map[p.slug] = p.display_name })
      setLabels(map)
    }).catch(() => {})
  }, [])

  // Fetch live matching rules for the selected partner (admins only); fall back
  // to a static description otherwise or for module engines.
  useEffect(() => {
    setRules([])
    if (isCore) {
      api.get(`/admin/match-rules?partner=${partner}`)
        .then(({ data }) => setRules((data || []).filter(r => r.is_active !== false).map(r => r.name)))
        .catch(() => {})
    }
  }, [partner, isCore])

  const rulesToShow = rules.length
    ? rules
    : (STATIC_RULES[partner] || (isCore ? STATIC_RULES._core : STATIC_RULES[kind] || []))

  // ── Run handlers ─────────────────────────────────────────────────────────────
  const clearResults = () => { setResult(null); setRangeResult(null); setModuleResult(null) }

  const handleRun = async () => {
    setRunning(true); clearResults()
    try {
      const { data } = await api.post('/recon/run', { partner, recon_date: reconDate })
      setResult(data)
      const total = (data.matched || 0) + (data.unmatched_bank || 0)
      const rate  = total > 0 ? Math.round(data.matched / total * 100) : 0
      const flag  = rate < 85 ? ' ⚠️ LOW' : rate >= 95 ? ' ✅' : ''
      toast.success(`Recon done — ${data.matched} matched, ${data.unmatched_bank} open (${rate}%${flag})`,
        { duration: rate < 85 ? 8000 : 5000 })
      loadRuns()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Recon failed')
    } finally { setRunning(false) }
  }

  const handleRunRange = async () => {
    if (!range.date_from || !range.date_to) return toast.error('Set both From and To dates')
    setRunning(true); clearResults()
    try {
      const { data } = await api.post('/recon/run-range', { partner, ...range })
      setRangeResult(data)
      toast.success(`Done! ${data.dates_processed} dates · ${data.total_matched} total matched`)
      loadRuns()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Range recon failed')
    } finally { setRunning(false) }
  }

  const handleRunModule = async () => {
    setRunning(true); clearResults()
    try {
      if (kind === 'bbps') {
        const { data } = await api.post('/bbps/run-recon')
        setModuleResult({ kind: 'bbps', summary: data })
        toast.success(`BBPS recon done — ${data.matched ?? 0} matched`)
      } else if (kind === 'evalue') {
        const { data } = await api.post('/evalue/run-recon-all')
        setModuleResult({ kind: 'evalue', ...data })
        toast.success(`E-Value recon done — ${data.accounts ?? 0} account(s)`)
      }
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Recon failed')
    } finally { setRunning(false) }
  }

  const onRun = () => {
    if (kind === 'bbps' || kind === 'evalue') return handleRunModule()
    return dateMode === 'single' ? handleRun() : handleRunRange()
  }

  const handleNeft = async () => {
    setRunningNeft(true)
    try {
      const { data } = await api.post('/recon/run-neft-d1', { partner, recon_date: dateMode === 'range' ? range.date_to : reconDate })
      toast.success(`NEFT D+1: ${data.neft_d1_matched} matched from ${data.prev_date}`)
      loadRuns()
    } catch (err) { toast.error(err.response?.data?.detail || 'NEFT D+1 failed') }
    finally { setRunningNeft(false) }
  }

  const handleInternalMatch = async () => {
    setRunningInternal(true)
    try {
      const { data } = await api.post('/recon/run-internal-match', { partner, recon_date: dateMode === 'range' ? range.date_from : reconDate })
      toast.success(`Internal match: ${data.internal_matched} pair${data.internal_matched !== 1 ? 's' : ''} netted`)
      loadRuns()
    } catch (err) { toast.error(err.response?.data?.detail || 'Internal match failed') }
    finally { setRunningInternal(false) }
  }

  const handleInterbankMatch = async () => {
    setRunningInterbank(true)
    try {
      const { data } = await api.post('/recon/run-interbank-match')
      setInterbankResult(data)
      toast.success(`Interbank: ${data.interbank_matched} pair${data.interbank_matched !== 1 ? 's' : ''} matched`)
      loadInterbankMatches()
    } catch (err) { toast.error(err.response?.data?.detail || 'Interbank match failed') }
    finally { setRunningInterbank(false) }
  }

  // ── Render ───────────────────────────────────────────────────────────────────
  return (
    <div>
      <h1 className="text-xl font-bold text-gray-800 mb-1">Run Reconciliation</h1>
      <p className="text-sm text-gray-500 mb-6">
        Re-applies matching rules to uploaded data for any product. Use after new uploads, rule changes,
        or to process a full date range at once. Each upload already auto-reconciles — this is for re-runs.
      </p>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
        {/* Config */}
        <div className="card lg:col-span-1">
          <h2 className="font-semibold text-gray-700 mb-4">Configuration</h2>

          {/* Partner / product */}
          <div className="mb-4">
            <label className="text-xs font-medium text-gray-500 block mb-1">Product / Partner</label>
            <select className="select" value={partner} onChange={e => { setPartner(e.target.value); clearResults() }}>
              {GROUPS.map(g => (
                <optgroup key={g.label} label={g.label}>
                  {g.slugs.map(s => <option key={s} value={s}>{labelOf(s)}</option>)}
                </optgroup>
              ))}
            </select>
          </div>

          {/* Date controls — only for core ledger products (modules reconcile all loaded data) */}
          {isCore && (
            <>
              <div className="flex gap-1 mb-4 p-1 bg-gray-100 rounded-lg">
                {[{ v: 'single', label: 'Single Date' }, { v: 'range', label: 'Date Range' }].map(({ v, label }) => (
                  <button key={v} onClick={() => setDateMode(v)}
                    className={`flex-1 py-1.5 rounded-md text-xs font-medium transition-colors
                      ${dateMode === v ? 'bg-white shadow text-primary' : 'text-gray-500 hover:text-gray-700'}`}>
                    {label}
                  </button>
                ))}
              </div>
              {dateMode === 'single' ? (
                <div className="mb-1">
                  <label className="text-xs font-medium text-gray-500 block mb-1">Recon Date</label>
                  <input type="date" className="input" value={reconDate} onChange={e => setReconDate(e.target.value)} />
                </div>
              ) : (
                <div className="space-y-3 mb-1">
                  <div>
                    <label className="text-xs font-medium text-gray-500 block mb-1">From Date</label>
                    <input type="date" className="input" value={range.date_from} onChange={e => setRange(r => ({ ...r, date_from: e.target.value }))} />
                  </div>
                  <div>
                    <label className="text-xs font-medium text-gray-500 block mb-1">To Date</label>
                    <input type="date" className="input" value={range.date_to} onChange={e => setRange(r => ({ ...r, date_to: e.target.value }))} />
                  </div>
                </div>
              )}
            </>
          )}

          {kind === 'bbps' || kind === 'evalue' ? (
            <div className="flex items-start gap-2 p-2.5 bg-blue-50 border border-blue-100 rounded-lg mb-1">
              <Info size={13} className="text-blue-500 flex-shrink-0 mt-0.5" />
              <p className="text-xs text-blue-700">
                {labelOf(partner)} reconciles all uploaded data ({kind === 'evalue' ? 'every account' : 'internal ↔ bank'}) in one pass — no date needed.
              </p>
            </div>
          ) : null}

          {/* Actions */}
          {kind === 'sbi' ? (
            <div className="mt-5 p-3 bg-rose-50 border border-rose-100 rounded-lg">
              <p className="text-xs text-rose-700 mb-2">
                SBI Kiosk uses a 4-process reconciliation (P01–P04). Run each process from its dedicated window.
              </p>
              <Link to="/sbi-kiosk" className="btn-primary w-full text-sm flex items-center justify-center gap-2" style={{ background: '#e11d48' }}>
                Open SBI Kiosk Recon <ExternalLink size={13} />
              </Link>
            </div>
          ) : (
            <div className="mt-5 space-y-2">
              <button onClick={onRun} disabled={running}
                className="btn-primary w-full flex items-center justify-center gap-2">
                <Play size={15} />
                {running ? 'Running…' : isCore ? (dateMode === 'single' ? 'Run Auto Reconciliation' : 'Run for Date Range') : `Run ${labelOf(partner)} Recon`}
              </button>

              {isCore && (
                <>
                  <button onClick={handleNeft} disabled={runningNeft} className="btn-ghost w-full flex items-center justify-center gap-2">
                    <Zap size={15} /> {runningNeft ? 'Running…' : 'Run NEFT D+1 Match'}
                  </button>
                  <button onClick={handleInternalMatch} disabled={runningInternal}
                    className="btn-ghost w-full flex items-center justify-center gap-2 text-orange-600 hover:text-orange-700 hover:border-orange-200">
                    <RefreshCw size={15} /> {runningInternal ? 'Running…' : 'Run Internal-to-Internal Match'}
                  </button>
                  <p className="text-xs text-gray-400">
                    Internal match nets self-cancelling pairs within the dump — a debit and its reversing credit
                    (same Tracking / TID, equal amount, opposite DR/CR) plus Success + Refund pairs. Runs
                    automatically on every internal upload; use this to re-run.
                  </p>
                </>
              )}
            </div>
          )}

          {/* Rules panel — live where possible, else accurate static description */}
          <div className="mt-5 p-3 bg-blue-50 rounded-lg">
            <p className="text-xs font-semibold text-blue-700 mb-2">
              {rules.length ? 'Rules Applied' : 'Matching Logic'} · {labelOf(partner)}
            </p>
            {rulesToShow.length ? (
              <ul className="text-xs text-blue-600 space-y-1">
                {rulesToShow.map((r, i) => <li key={i}>{i + 1}. {r}</li>)}
              </ul>
            ) : (
              <p className="text-xs text-blue-500">No rules configured. Add them in Logic Builder.</p>
            )}
          </div>
        </div>

        {/* Result */}
        <div className="card lg:col-span-2">
          {result ? (
            <CoreResult result={result} reconDate={reconDate} />
          ) : rangeResult ? (
            <RangeResult rangeResult={rangeResult} range={range} />
          ) : moduleResult ? (
            <ModuleResult moduleResult={moduleResult} label={labelOf(partner)} />
          ) : (
            <div className="text-center py-12 text-gray-400">
              <Play size={36} className="mx-auto mb-3 opacity-30" />
              <p className="text-sm">Configure and run reconciliation to see results</p>
              <p className="text-xs mt-2 text-gray-300 max-w-xs mx-auto">
                Pick any product — DMT banks, BBPS, E-Value or SBI — then run.
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Interbank Fund Movement — DMT ledger concept (core only) */}
      {isCore && (
        <div className="card mt-5">
          <div className="flex items-start justify-between mb-4">
            <div>
              <h2 className="font-semibold text-gray-700 flex items-center gap-2">
                <ArrowLeftRight size={16} className="text-indigo-500" /> Interbank Fund Movement Match
              </h2>
              <p className="text-xs text-gray-400 mt-1">
                Matches <code className="bg-gray-100 px-1 rounded">fund_transfer</code> CR ↔ DR pairs across all banks using UTR.
                Cross-partner · Cross-date · No filters needed.
              </p>
            </div>
            <button onClick={handleInterbankMatch} disabled={runningInterbank}
              className="btn-primary flex items-center gap-2 whitespace-nowrap">
              <ArrowLeftRight size={14} /> {runningInterbank ? 'Running…' : 'Run Interbank Match'}
            </button>
          </div>

          {interbankResult && (
            <div className="grid grid-cols-3 gap-3 mb-4">
              {[
                { label: 'Pairs Matched', value: interbankResult.interbank_matched, cls: 'bg-green-50 text-green-600' },
                { label: 'Unmatched CR',  value: interbankResult.unmatched_cr,      cls: 'bg-red-50 text-red-500' },
                { label: 'Unmatched DR',  value: interbankResult.unmatched_dr,      cls: 'bg-orange-50 text-orange-600' },
              ].map(({ label, value, cls }) => (
                <div key={label} className={`p-3 rounded-lg ${cls.split(' ')[0]}`}>
                  <div className={`text-2xl font-bold ${cls.split(' ')[1]}`}>{value ?? 0}</div>
                  <div className="text-xs text-gray-500">{label}</div>
                </div>
              ))}
            </div>
          )}

          <div className="flex items-center justify-between mb-2">
            <p className="text-xs font-medium text-gray-500">Recent Interbank Matches</p>
            <button onClick={loadInterbankMatches} className="text-gray-400 hover:text-gray-600">
              <RefreshCw size={13} className={loadingIBT ? 'animate-spin' : ''} />
            </button>
          </div>

          {interbankMatches.length === 0 ? (
            <p className="text-xs text-gray-400 text-center py-4">
              {loadingIBT ? 'Loading…' : 'No interbank matches yet — click "Run Interbank Match" to start'}
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-gray-100">
                    <th className="table-th">Match ID</th><th className="table-th">Partner</th><th className="table-th">Date</th>
                    <th className="table-th">DR/CR</th><th className="table-th text-right">Amount</th><th className="table-th">UTR</th>
                  </tr>
                </thead>
                <tbody>
                  {interbankMatches.map(pair => pair.transactions.map((row, i) => (
                    <tr key={row.id} className={`border-b border-gray-50 hover:bg-gray-50 ${i === 0 ? 'border-t-2 border-t-indigo-100' : ''}`}>
                      <td className="table-td font-mono text-indigo-600">{i === 0 ? pair.match_id : ''}</td>
                      <td className="table-td capitalize">{row.partner}</td>
                      <td className="table-td font-mono">{row.recon_date}</td>
                      <td className="table-td">
                        {row.dr_cr && <span className={`text-xs font-bold px-1.5 py-0.5 rounded ${row.dr_cr === 'DR' ? 'bg-red-50 text-red-600' : 'bg-green-50 text-green-600'}`}>{row.dr_cr}</span>}
                      </td>
                      <td className="table-td text-right tabular-nums font-medium">{row.amount?.toLocaleString('en-IN', { minimumFractionDigits: 2 })}</td>
                      <td className="table-td font-mono text-gray-500">{row.utr_number || '—'}</td>
                    </tr>
                  )))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Run history (core ledger runs) */}
      <div className="card mt-5">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-semibold text-gray-700">Run History</h2>
          <button onClick={loadRuns} className="text-gray-400 hover:text-gray-600"><RefreshCw size={14} /></button>
        </div>
        {runs.length === 0 ? (
          <p className="text-sm text-gray-400 text-center py-4">No runs yet</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100">
                  <th className="table-th">Partner</th><th className="table-th">Date</th><th className="table-th">Run At</th>
                  <th className="table-th text-right">Bank</th><th className="table-th text-right">Internal</th>
                  <th className="table-th text-right">Matched</th><th className="table-th text-right">Open</th>
                </tr>
              </thead>
              <tbody>
                {runs.map(r => (
                  <tr key={r.id} className="hover:bg-gray-50">
                    <td className="table-td capitalize">{r.partner}</td>
                    <td className="table-td font-mono">{r.recon_date}</td>
                    <td className="table-td text-gray-400">{new Date(r.run_at).toLocaleString()}</td>
                    <td className="table-td text-right">{r.total_bank?.toLocaleString()}</td>
                    <td className="table-td text-right">{r.total_internal?.toLocaleString()}</td>
                    <td className="table-td text-right text-green-600">{r.matched?.toLocaleString()}</td>
                    <td className="table-td text-right text-red-500">{(r.unmatched_bank + r.unmatched_internal)?.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Result sub-components ─────────────────────────────────────────────────────
function StatCard({ label, value, highlight }) {
  const bg = { green: 'bg-green-50', red: 'bg-red-50', blue: 'bg-blue-50', orange: 'bg-orange-50' }[highlight] || 'bg-gray-50'
  const tx = { green: 'text-green-600', red: 'text-red-500', blue: 'text-blue-600', orange: 'text-orange-600' }[highlight] || 'text-gray-700'
  return (
    <div className={`p-3 rounded-lg ${bg}`}>
      <div className={`text-lg font-bold ${tx}`}>{value?.toLocaleString?.() ?? value}</div>
      <div className="text-xs text-gray-500">{label}</div>
    </div>
  )
}

function CoreResult({ result, reconDate }) {
  return (
    <>
      <div className="flex items-center gap-2 mb-4">
        <CheckCircle size={18} className="text-green-500" />
        <h2 className="font-semibold text-gray-700">Run Complete · {reconDate}</h2>
      </div>
      <div className="grid grid-cols-3 gap-3 mb-4">
        <StatCard label="Bank Txns" value={result.total_bank} />
        <StatCard label="Internal Txns" value={result.total_internal} />
        <StatCard label="Matched" value={result.matched} highlight="green" />
        <StatCard label="Unmatched Bank" value={result.unmatched_bank} highlight="red" />
        <StatCard label="Unmatched Internal" value={result.unmatched_internal} highlight="red" />
        <StatCard label="Duplicates Flagged" value={result.duplicate ?? 0} highlight={result.duplicate > 0 ? 'orange' : ''} />
        <StatCard label="Match Rate" value={result.total_bank ? `${((result.matched / result.total_bank) * 100).toFixed(1)}%` : '—'} highlight="blue" />
      </div>
      {result.rules_applied?.length > 0 && (
        <div className="p-3 bg-gray-50 rounded-lg">
          <p className="text-xs font-semibold text-gray-600 mb-1">Rules Used</p>
          {result.rules_applied.map((r, i) => <p key={i} className="text-xs text-gray-500">• {r}</p>)}
        </div>
      )}
    </>
  )
}

function RangeResult({ rangeResult, range }) {
  return (
    <>
      <div className="flex items-center gap-2 mb-4">
        <CheckCircle size={18} className="text-green-500" />
        <h2 className="font-semibold text-gray-700">Range Complete · {range.date_from} → {range.date_to}</h2>
      </div>
      <div className="grid grid-cols-2 gap-3 mb-4">
        <div className="p-3 bg-blue-50 rounded-lg"><div className="text-2xl font-bold text-blue-600">{rangeResult.dates_processed}</div><div className="text-xs text-gray-500">Dates Processed</div></div>
        <div className="p-3 bg-green-50 rounded-lg"><div className="text-2xl font-bold text-green-600">{rangeResult.total_matched?.toLocaleString()}</div><div className="text-xs text-gray-500">Total Matched</div></div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-gray-100">
              <th className="table-th">Date</th><th className="table-th text-right text-green-600">Matched</th>
              <th className="table-th text-right text-red-500">Open Bank</th><th className="table-th text-right text-orange-500">Open Internal</th>
            </tr>
          </thead>
          <tbody>
            {rangeResult.results.map(r => (
              <tr key={r.date} className="border-b border-gray-50 hover:bg-gray-50">
                <td className="table-td font-mono">{r.date}</td>
                <td className="table-td text-right text-green-600 font-medium">{r.matched}</td>
                <td className="table-td text-right text-red-500">{r.unmatched_bank}</td>
                <td className="table-td text-right text-orange-500">{r.unmatched_internal}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

// Generic module result — renders whatever counts the engine returns.
const NICE = (k) => k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
const COLOR_FOR = (k) => k === 'matched' ? 'green'
  : k.startsWith('unmatched') ? 'red'
  : k.includes('mismatch') || k.includes('refund') || k.includes('failed') ? 'orange' : ''

function ModuleResult({ moduleResult, label }) {
  if (moduleResult.kind === 'bbps') {
    const s = moduleResult.summary || {}
    const entries = Object.entries(s).filter(([, v]) => typeof v === 'number')
    return (
      <>
        <div className="flex items-center gap-2 mb-4"><CheckCircle size={18} className="text-green-500" /><h2 className="font-semibold text-gray-700">BBPS Recon Complete</h2></div>
        <div className="grid grid-cols-3 gap-3">
          {entries.map(([k, v]) => <StatCard key={k} label={NICE(k)} value={v} highlight={COLOR_FOR(k)} />)}
        </div>
      </>
    )
  }
  // E-Value: per-account results
  const results = moduleResult.results || []
  return (
    <>
      <div className="flex items-center gap-2 mb-4"><CheckCircle size={18} className="text-green-500" /><h2 className="font-semibold text-gray-700">E-Value Recon Complete · {moduleResult.accounts ?? results.length} account(s)</h2></div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-gray-100">
              <th className="table-th">Account</th><th className="table-th text-right text-green-600">Matched</th>
              <th className="table-th text-right text-red-500">Unmatched</th><th className="table-th">Note</th>
            </tr>
          </thead>
          <tbody>
            {results.map((r, i) => (
              <tr key={i} className="border-b border-gray-50 hover:bg-gray-50">
                <td className="table-td font-mono">{r.reco_acc_no}</td>
                <td className="table-td text-right text-green-600 font-medium">{r.matched ?? '—'}</td>
                <td className="table-td text-right text-red-500">{(r.unmatched_bank ?? 0) + (r.unmatched_load ?? 0) || '—'}</td>
                <td className="table-td text-gray-400">{r.error || ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
