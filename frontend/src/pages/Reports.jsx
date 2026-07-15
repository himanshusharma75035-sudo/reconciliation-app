import React, { useState, useEffect } from 'react'
import { DMT_PARTNERS } from '../productRegistry'
import { Download, BarChart2, FileText, CreditCard, Layers, Tag, Clock, Wallet, Receipt, TrendingUp, ExternalLink } from 'lucide-react'
import { Link } from 'react-router-dom'
import toast from 'react-hot-toast'
import api from '../utils/api'
import ScheduledReports from './ScheduledReports'

// Fallback partner list — used if API is unavailable
// Mirrors backend _SBI_REPORTS (GET /sbi/report-options) — used until it loads.
const SBI_REPORT_FALLBACK = [
  { id: 'daily_pack',  label: 'Daily Recon Pack',             scope: 'date',  desc: 'One workbook for the day: overview, all four processes, exceptions, reversals, SRC & manual logs.' },
  { id: 'exceptions',  label: 'Exceptions / Work List',       scope: 'range', desc: 'Only what needs action: P01 not credited, P02/P03 unmatched or partial, P04 pending wallet actions.' },
  { id: 'summary',     label: 'MIS Summary (trend)',          scope: 'range', desc: 'Per date × process: totals, matched, open, match rate and amounts — management trend view.' },
  { id: 'ko_wise',     label: 'KO / CSP-wise Summary',        scope: 'range', desc: 'Per agent roll-up across P01–P03: volumes, matched vs open counts and amounts.' },
  { id: 'unified',     label: 'All Entries (unified ledger)', scope: 'date',  desc: 'Every bank and data entry with its status, which process reconciled it, and its counterpart.' },
  { id: 'p01_lines',   label: 'P01 Settlement Lines',         scope: 'date',  desc: 'Line-level P01: each KO withdrawal and each bank settlement line behind every KO total.' },
  { id: 'reversals',   label: 'Reversals Report',             scope: 'range', desc: 'All P02 reversal legs (same reference DR + CR) with success/fail state.' },
  { id: 'p04_actions', label: 'Wallet Action Tracker (P04)',  scope: 'range', desc: 'Deposit/withdrawal corrections with done vs pending status.' },
  { id: 'src_log',     label: 'SRC Disposition Log',          scope: 'range', desc: 'Every SRC tag on SBI rows: code, note, who and when.' },
  { id: 'manual_log',  label: 'Manual Match Log',             scope: 'range', desc: 'Every SBI manual match: key, counterpart, remark, who and when.' },
]

const FALLBACK_PARTNERS = [
  { slug: 'fino',      display_name: 'Fino'              },
  { slug: 'airtel',    display_name: 'Airtel'            },
  { slug: 'axis',      display_name: 'Axis'              },
  { slug: 'levin',     display_name: 'Levin'             },
  { slug: 'aeps',      display_name: 'AePS'              },
  { slug: 'pg',        display_name: 'PG (PayU)'         },
  { slug: 'qr',        display_name: 'QR Collection'     },
  { slug: 'digikhata', display_name: 'Digikhata (PPI)'  },
  { slug: 'indonepal', display_name: 'Indo-Nepal'        },
  { slug: 'kiosk',     display_name: 'SBI Kiosk'         },
]

export default function Reports() {
  const [tab, setTab] = useState('summary')

  // Shared filters — each tab uses what it needs
  const [filters, setFilters] = useState({
    partner: '',
    from_date: '', to_date: '',
    side: '', recon_status: 'open',
  })

  // Dynamic partner list — fetched from admin API on mount
  const [partnerList, setPartnerList] = useState([])

  const [summaryData, setSummaryData]     = useState(null)
  const [statementData, setStatementData] = useState(null)
  const [ageingData, setAgeingData]       = useState([])
  const [pgSettlement, setPgSettlement]   = useState(null)
  const [activeSrcCodes, setActiveSrcCodes] = useState([])
  const [srcCode, setSrcCode]             = useState('')
  const [loading, setLoading]             = useState(false)

  // Fetch active partners from admin API; fall back to hardcoded list on error
  useEffect(() => {
    api.get('/admin/partners-public')
      .then(({ data }) => setPartnerList(data?.length ? data : FALLBACK_PARTNERS))
      .catch(() => setPartnerList(FALLBACK_PARTNERS))
  }, [])

  // SBI Kiosk report library — served by the backend so new reports appear here
  // without a frontend change; static fallback keeps the tab useful offline.
  const [sbiReports, setSbiReports] = useState(SBI_REPORT_FALLBACK)
  useEffect(() => {
    api.get('/sbi/report-options')
      .then(({ data }) => { if (data?.reports?.length) setSbiReports(data.reports) })
      .catch(() => {})
  }, [])

  // Universal Report Library — every product (current AND future) gets its pack
  // from GET /api/reports/library; new products/reports appear with zero UI change.
  const [libProducts, setLibProducts] = useState([])
  const [libProduct, setLibProduct] = useState('')
  useEffect(() => {
    api.get('/reports/library')
      .then(({ data }) => {
        setLibProducts(data?.products || [])
        if (data?.products?.length && !libProduct) setLibProduct(data.products[0].id)
      })
      .catch(() => {})
  }, [])

  const downloadLibReport = async (prodId, rep) => {
    const params = new URLSearchParams({ product: prodId, type: rep.id })
    if (rep.scope === 'date') {
      if (filters.to_date) params.append('recon_date', filters.to_date)
    } else if (rep.scope === 'range') {
      if (filters.from_date) params.append('date_from', filters.from_date)
      if (filters.to_date)   params.append('date_to', filters.to_date)
    }
    try {
      const r = await api.get(`/reports/library/run?${params}`, { responseType: 'blob' })
      const used = r.headers['x-recon-date']
      if (used === 'none') { toast(`No data yet for "${rep.label}"`, { icon: 'ℹ️' }); return }
      _download(r.data, `${prodId}_${rep.id}_${used || 'export'}.xlsx`)
      if (rep.scope === 'date' && filters.to_date && used && used !== filters.to_date)
        toast(`No data for ${filters.to_date} — exported latest (${used})`, { icon: 'ℹ️' })
    } catch (e) {
      toast.error(e?.response?.data?.detail || 'Report failed')
    }
  }

  const downloadSbiReport = async (rep) => {
    const params = new URLSearchParams({ type: rep.id })
    if (rep.scope === 'date') {
      if (filters.to_date) params.append('recon_date', filters.to_date)
    } else {
      if (filters.from_date) params.append('date_from', filters.from_date)
      if (filters.to_date)   params.append('date_to', filters.to_date)
    }
    try {
      const r = await api.get(`/sbi/report?${params}`, { responseType: 'blob' })
      const used = r.headers['x-recon-date']
      if (used === 'none') { toast(`No data yet for "${rep.label}"`, { icon: 'ℹ️' }); return }
      _download(r.data, `sbi_${rep.id}_${used || 'export'}.xlsx`)
      if (rep.scope === 'date' && filters.to_date && used && used !== filters.to_date)
        toast(`No data for ${filters.to_date} — exported latest (${used})`, { icon: 'ℹ️' })
    } catch { toast.error('Report failed') }
  }

  // Resolved list — always non-empty.
  // E-Value, BBPS and SBI Kiosk reconcile in their OWN tables, so the core
  // report tabs (which read the transactions ledger) would always show zeros
  // for them — they are excluded here and served by their dedicated tabs.
  const MODULE_PRODUCTS = ['kiosk', 'evalue', 'bbps']
  const MODULE_SLUGS    = ['kiosk', 'sbi', 'evalue', 'bbps']
  // Reports are selected PRODUCT-wise. The DMT banks (Fino/Airtel/Axis/Levin)
  // collapse into one "DMT" option that the backend expands across all of them.
  // The other core products map 1:1 to a partner. (E-Value / BBPS / SBI have
  // their own report tabs since they reconcile in separate tables.)
  const DMT_SLUGS = DMT_PARTNERS
  const partners = (() => {
    const src = (partnerList.length > 0 ? partnerList : FALLBACK_PARTNERS)
      .filter(p => !MODULE_PRODUCTS.includes(p.product) && !MODULE_SLUGS.includes(p.slug))
    const hasDmt = src.some(p => DMT_SLUGS.includes(p.slug))
    const rest   = src.filter(p => !DMT_SLUGS.includes(p.slug))
    const out = []
    if (hasDmt) out.push({ slug: 'dmt', display_name: 'DMT (All Banks)' })
    return out.concat(rest)
  })()

  const upd = patch => setFilters(f => ({ ...f, ...patch }))
  const clearFilters = () => setFilters({ partner: '', from_date: '', to_date: '', side: '', recon_status: 'open' })

  // ── Summary ────────────────────────────────────────────────────────────────
  const loadSummary = async () => {
    setLoading(true)
    try {
      const { data } = await api.get('/reports/summary', {
        params: {
          partner:   filters.partner   || undefined,
          from_date: filters.from_date || undefined,
          to_date:   filters.to_date   || undefined,
        }
      })
      setSummaryData(data)
    } catch { toast.error('Failed to load summary') }
    finally { setLoading(false) }
  }

  const exportSummary = async () => {
    const params = new URLSearchParams()
    if (filters.partner)   params.append('partner',   filters.partner)
    if (filters.from_date) params.append('from_date', filters.from_date)
    if (filters.to_date)   params.append('to_date',   filters.to_date)
    try {
      const resp = await api.get(`/reports/summary/export?${params}`, { responseType: 'blob' })
      _download(resp.data, `summary_${filters.partner || 'all'}.xlsx`)
      toast.success('Downloaded')
    } catch { toast.error('Export failed') }
  }

  // ── Recon Statement ────────────────────────────────────────────────────────
  const loadStatement = async () => {
    if (!filters.partner) return toast.error('Select a partner')
    if (!filters.from_date && !filters.to_date) return toast.error('Set at least one date')
    setLoading(true)
    try {
      const { data } = await api.get('/reports/reconciliation-statement', {
        params: {
          partner:   filters.partner,
          from_date: filters.from_date || undefined,
          to_date:   filters.to_date   || undefined,
        }
      })
      setStatementData(data)
    } catch { toast.error('Failed to load statement') }
    finally { setLoading(false) }
  }

  // ── Ageing ─────────────────────────────────────────────────────────────────
  const loadAgeing = async () => {
    setLoading(true)
    try {
      const { data } = await api.get('/reports/ageing', {
        params: {
          partner:   filters.partner   || undefined,
          from_date: filters.from_date || undefined,
          to_date:   filters.to_date   || undefined,
        }
      })
      setAgeingData(data)
    } catch { toast.error('Failed to load ageing') }
    finally { setLoading(false) }
  }

  const exportAgeing = async () => {
    const params = new URLSearchParams()
    if (filters.partner)   params.append('partner',   filters.partner)
    if (filters.from_date) params.append('from_date', filters.from_date)
    if (filters.to_date)   params.append('to_date',   filters.to_date)
    try {
      const resp = await api.get(`/reports/ageing/export?${params}`, { responseType: 'blob' })
      _download(resp.data, `ageing_${filters.partner || 'all'}.xlsx`)
      toast.success('Downloaded')
    } catch { toast.error('Export failed') }
  }

  // ── Export tab helpers ─────────────────────────────────────────────────────
  const exportOpenItems = async () => {
    const params = new URLSearchParams()
    if (filters.partner)   params.append('partner',   filters.partner)
    if (filters.from_date) params.append('from_date', filters.from_date)
    if (filters.to_date)   params.append('to_date',   filters.to_date)
    if (filters.side)      params.append('side',      filters.side)
    params.append('recon_status', filters.recon_status || 'open')
    try {
      const resp = await api.get(`/reports/open-items/export?${params}`, { responseType: 'blob' })
      _download(resp.data, `open_items_${filters.partner || 'all'}.xlsx`)
      toast.success('Downloaded')
    } catch { toast.error('Export failed') }
  }

  const exportMatchedPairs = async () => {
    if (!filters.partner) return toast.error('Select a partner')
    const params = new URLSearchParams({ partner: filters.partner })
    if (filters.from_date) params.append('from_date', filters.from_date)
    if (filters.to_date)   params.append('to_date',   filters.to_date)
    try {
      const resp = await api.get(`/reports/export-matched?${params}`, { responseType: 'blob' })
      _download(resp.data, `matched_pairs_${filters.partner}.xlsx`)
      toast.success('Downloaded')
    } catch { toast.error('Export failed') }
  }

  const exportEod = async () => {
    if (!filters.partner || !filters.to_date) return toast.error('Partner and To Date required for EOD report')
    const params = new URLSearchParams({ partner: filters.partner, recon_date: filters.to_date })
    try {
      const resp = await api.get(`/reports/eod-summary/export?${params}`, { responseType: 'blob' })
      _download(resp.data, `eod_${filters.partner}_${filters.to_date}.xlsx`)
      toast.success('Downloaded')
    } catch { toast.error('Export failed') }
  }

  // ── PG Settlement ──────────────────────────────────────────────────────────
  const loadPgSettlement = async () => {
    setLoading(true)
    try {
      const { data } = await api.get('/recon/pg-settlement-summary', {
        params: {
          date_from: filters.from_date || undefined,
          date_to:   filters.to_date   || undefined,
        }
      })
      setPgSettlement(data)
    } catch { toast.error('Failed to load PG settlement') }
    finally { setLoading(false) }
  }

  const exportPgSettlement = async () => {
    const params = new URLSearchParams()
    if (filters.from_date) params.append('date_from', filters.from_date)
    if (filters.to_date)   params.append('date_to',   filters.to_date)
    try {
      const resp = await api.get(`/reports/pg-settlement/export?${params}`, { responseType: 'blob' })
      _download(resp.data, `pg_settlement_${filters.from_date || 'all'}_to_${filters.to_date || 'all'}.xlsx`)
      toast.success('Downloaded')
    } catch { toast.error('Export failed') }
  }

  // ── One-side full report ──────────────────────────────────────────────────
  const exportSideReport = async (side) => {
    const params = new URLSearchParams({ side })
    if (filters.partner)   params.append('partner',   filters.partner)
    if (filters.from_date) params.append('from_date', filters.from_date)
    if (filters.to_date)   params.append('to_date',   filters.to_date)
    try {
      const resp = await api.get(`/reports/side-report/export?${params}`, { responseType: 'blob' })
      _download(resp.data, `${side}_full_report_${filters.partner || 'all'}.xlsx`)
      toast.success('Downloaded')
    } catch { toast.error('Export failed') }
  }

  // ── SRC report ────────────────────────────────────────────────────────────
  const loadSrcCodes = async () => {
    try {
      const params = {}
      if (filters.partner)   params.partner   = filters.partner
      if (filters.from_date) params.from_date = filters.from_date
      if (filters.to_date)   params.to_date   = filters.to_date
      const { data } = await api.get('/reports/src-codes-active', { params })
      setActiveSrcCodes(data.src_codes || [])
    } catch { /* silent */ }
  }

  const exportSrcReport = async () => {
    const params = new URLSearchParams()
    if (srcCode)           params.append('src_code',  srcCode)
    if (filters.partner)   params.append('partner',   filters.partner)
    if (filters.from_date) params.append('from_date', filters.from_date)
    if (filters.to_date)   params.append('to_date',   filters.to_date)
    try {
      const resp = await api.get(`/reports/src-report/export?${params}`, { responseType: 'blob' })
      _download(resp.data, `src_report_${srcCode || 'all'}_${filters.partner || 'all'}.xlsx`)
      toast.success('Downloaded')
    } catch { toast.error('Export failed') }
  }

  // Load SRC codes whenever tab or filters change
  useEffect(() => {
    if (tab === 'src') loadSrcCodes()
  }, [tab, filters.partner, filters.from_date, filters.to_date])

  // ── Shared date range input row ─────────────────────────────────────────────
  const DateRangeRow = ({ showPartner = true, partnerAllOption = false, partnerFilter = null }) => {
    // partnerFilter: optional fn(p) to restrict which partners appear (e.g. DMT only)
    const visiblePartners = partnerFilter ? partners.filter(partnerFilter) : partners
    return (
      <div className="grid grid-cols-2 gap-3 mb-4">
        {showPartner && (
          <>
            <div>
              <label className="text-xs text-gray-400 block mb-1">Partner</label>
              <select className="select" value={filters.partner} onChange={e => upd({ partner: e.target.value })}>
                {partnerAllOption && <option value="">All</option>}
                {visiblePartners.map(p => (
                  <option key={p.slug} value={p.slug}>{p.display_name}</option>
                ))}
              </select>
              <p className="text-[11px] text-gray-400 mt-1">
                E-Value, BBPS &amp; SBI Kiosk reconcile separately — use their dedicated tabs above for those reports.
              </p>
            </div>
            <div />
          </>
        )}
        <div>
          <label className="text-xs text-gray-400 block mb-1">From Date</label>
          <input type="date" className="input" value={filters.from_date} onChange={e => upd({ from_date: e.target.value })} />
        </div>
        <div>
          <label className="text-xs text-gray-400 block mb-1">To Date</label>
          <input type="date" className="input" value={filters.to_date} onChange={e => upd({ to_date: e.target.value })} />
        </div>
      </div>
    )
  }

  function _download(blob, filename) {
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = filename; a.click()
    URL.revokeObjectURL(url)
  }

  const TABS = [
    { key: 'library',        label: 'Report Library',  icon: Layers     },
    { key: 'summary',        label: 'Summary',         icon: BarChart2  },
    { key: 'statement',      label: 'Recon Statement', icon: FileText   },
    { key: 'ageing',         label: 'Ageing',          icon: BarChart2  },
    { key: 'pg-settlement',  label: 'PG Settlement',   icon: CreditCard },
    { key: 'kiosk',          label: 'Kiosk Recon',     icon: Layers     },
    { key: 'side-report',    label: 'Side Reports',    icon: Layers     },
    { key: 'src',            label: 'SRC Reports',     icon: Tag        },
    { key: 'export',         label: 'Export Data',     icon: Download   },
    { key: 'evalue',         label: 'E-Value',         icon: Wallet     },
    { key: 'bbps',           label: 'BBPS',            icon: Receipt    },
    { key: 'insights',       label: 'Insights',        icon: TrendingUp },
    { key: 'scheduled',      label: 'Scheduled',       icon: Clock      },
  ]

  return (
    <div>
      <h1 className="text-xl font-bold text-gray-800 mb-1">Reports</h1>
      <p className="text-sm text-gray-500 mb-5">Download and analyse reconciliation data</p>

      <div className="flex items-center justify-between mb-5 flex-wrap gap-3">
        <div className="flex gap-2 flex-wrap">
          {TABS.map(({ key, label, icon: Icon }) => (
            <button key={key} onClick={() => setTab(key)}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors
                ${tab === key ? 'bg-primary text-white' : 'bg-white border border-gray-200 text-gray-600 hover:bg-gray-50'}`}>
              <Icon size={15} />{label}
            </button>
          ))}
        </div>
        {(filters.partner || filters.from_date || filters.to_date) && (
          <button onClick={clearFilters}
            className="text-xs text-gray-500 hover:text-red-500 border border-gray-200 rounded-lg px-3 py-1.5 bg-white hover:border-red-200 transition-colors flex items-center gap-1">
            ✕ Clear Filters
          </button>
        )}
      </div>

      {/* ── Summary ── */}
      {tab === 'summary' && (
        <div className="card max-w-xl">
          <h2 className="font-semibold text-gray-700 mb-4">Summary Report</h2>
          <DateRangeRow showPartner partnerAllOption />
          <div className="flex gap-2">
            <button onClick={loadSummary} disabled={loading} className="btn-primary">
              {loading ? 'Loading…' : 'Generate'}
            </button>
            {summaryData && (
              <button onClick={exportSummary} className="btn-ghost flex items-center gap-1.5">
                <Download size={14} /> Export Excel
              </button>
            )}
          </div>

          {summaryData && (
            <div className="mt-5 space-y-3">
              <div className="grid grid-cols-2 gap-3">
                {[
                  { label: 'Total Transactions', value: summaryData.total,        color: 'text-gray-800' },
                  { label: 'Matched',            value: summaryData.matched,      color: 'text-green-600' },
                  { label: 'Unmatched',          value: summaryData.unmatched,    color: 'text-red-500' },
                  { label: 'SRC Assigned',       value: summaryData.src_assigned, color: 'text-yellow-600' },
                  { label: 'Match Rate',         value: `${summaryData.match_rate}%`, color: 'text-blue-600' },
                ].map(({ label, value, color }) => (
                  <div key={label} className="bg-gray-50 rounded-lg p-3">
                    <div className={`text-xl font-bold ${color}`}>{value?.toLocaleString?.() ?? value}</div>
                    <div className="text-xs text-gray-500">{label}</div>
                  </div>
                ))}
              </div>
              {Object.keys(summaryData.src_breakdown).length > 0 && (
                <div className="bg-yellow-50 rounded-lg p-3">
                  <p className="text-xs font-semibold text-yellow-700 mb-2">SRC Breakdown</p>
                  {Object.entries(summaryData.src_breakdown).map(([code, count]) => (
                    <div key={code} className="flex justify-between text-xs text-yellow-700 py-0.5">
                      <span>{code.replace(/_/g, ' ')}</span>
                      <span className="font-medium">{count}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── Recon Statement ── */}
      {tab === 'statement' && (
        <div className="card max-w-xl">
          <h2 className="font-semibold text-gray-700 mb-4">Bank Reconciliation Statement</h2>
          <DateRangeRow showPartner />
          <button onClick={loadStatement} disabled={loading} className="btn-primary">
            {loading ? 'Loading…' : 'Generate'}
          </button>

          {statementData && (
            <div className="mt-5 border border-gray-200 rounded-lg overflow-hidden">
              <div className="bg-primary text-white text-center py-2 text-sm font-semibold">
                Bank Reconciliation Statement · {statementData.partner?.toUpperCase()} · {statementData.recon_date}
              </div>
              <table className="w-full text-sm">
                <tbody>
                  {[
                    { label: 'Balance as per Simplibank (SMB)',    value: statementData.balance_as_per_simplibank,   bold: true },
                    { label: 'Less: Debited in SMB but not Bank',  value: -statementData.less_debited_in_smb_not_bank, indent: true },
                    { label: 'Add: Credited in SMB but not Bank',  value: statementData.add_credited_in_smb_not_bank,  indent: true },
                    { label: 'Less: Debited in Bank but not SMB',  value: -statementData.less_debited_in_bank_not_smb, indent: true },
                    { label: 'Add: Credited in Bank but not SMB',  value: statementData.add_credited_in_bank_not_smb,  indent: true },
                    { label: 'Balance Should Be As Per Bank',      value: statementData.balance_should_be_as_per_bank, bold: true },
                    { label: 'Balance as per Bank',                value: statementData.balance_as_per_bank,           bold: true },
                    { label: 'Difference',                         value: statementData.difference,                    bold: true, highlight: Math.abs(statementData.difference) < 0.01 },
                  ].map(({ label, value, bold, indent, highlight }) => (
                    <tr key={label} className={`border-t border-gray-100 ${highlight === true ? 'bg-green-50' : ''}`}>
                      <td className={`px-4 py-2 ${indent ? 'pl-8' : ''} ${bold ? 'font-semibold' : ''} text-gray-700`}>{label}</td>
                      <td className={`px-4 py-2 text-right tabular-nums ${bold ? 'font-semibold' : ''} ${value < 0 ? 'text-red-500' : 'text-gray-800'}`}>
                        ₹{Math.abs(value).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ── Ageing ── */}
      {tab === 'ageing' && (
        <div className="card">
          <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
            <h2 className="font-semibold text-gray-700">Ageing Analysis — Unmatched Items by Date</h2>
            <div className="flex gap-2 items-center flex-wrap">
              <select className="select w-32" value={filters.partner} onChange={e => upd({ partner: e.target.value })}>
                <option value="">All</option>
                {partners.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}
              </select>
              <input type="date" className="input w-36" placeholder="From" value={filters.from_date}
                onChange={e => upd({ from_date: e.target.value })} />
              <input type="date" className="input w-36" placeholder="To" value={filters.to_date}
                onChange={e => upd({ to_date: e.target.value })} />
              <button onClick={loadAgeing} disabled={loading} className="btn-primary px-4">
                {loading ? '…' : 'Load'}
              </button>
              {ageingData.length > 0 && (
                <button onClick={exportAgeing} className="btn-ghost flex items-center gap-1.5">
                  <Download size={14} /> Excel
                </button>
              )}
            </div>
          </div>

          {ageingData.length === 0 ? (
            <p className="text-sm text-gray-400 text-center py-8">Click Load to see ageing report</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-100">
                    <th className="table-th">Partner</th>
                    <th className="table-th">Date</th>
                    <th className="table-th text-right">Bank Open</th>
                    <th className="table-th text-right">Internal Open</th>
                  </tr>
                </thead>
                <tbody>
                  {ageingData.map((row, i) => (
                    <tr key={i} className="hover:bg-gray-50 border-b border-gray-50">
                      <td className="table-td capitalize">{row.partner}</td>
                      <td className="table-td font-mono">{row.date}</td>
                      <td className="table-td text-right text-red-500">{row.bank}</td>
                      <td className="table-td text-right text-orange-500">{row.internal}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ── PG Settlement ── */}
      {tab === 'pg-settlement' && (
        <div className="card max-w-3xl">
          <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
            <h2 className="font-semibold text-gray-700">PG Settlement — Accept Payment (PayU)</h2>
            <div className="flex gap-2 items-center flex-wrap">
              <input type="date" className="input w-36" placeholder="From" value={filters.from_date}
                onChange={e => upd({ from_date: e.target.value })} />
              <input type="date" className="input w-36" placeholder="To" value={filters.to_date}
                onChange={e => upd({ to_date: e.target.value })} />
              <button onClick={loadPgSettlement} disabled={loading} className="btn-primary px-4">
                {loading ? '…' : 'Load'}
              </button>
              {pgSettlement?.rows?.length > 0 && (
                <button onClick={exportPgSettlement} className="btn-ghost flex items-center gap-1.5">
                  <Download size={14} /> Excel
                </button>
              )}
            </div>
          </div>

          {/* Summary totals */}
          {pgSettlement?.summary && (
            <div className="grid grid-cols-4 gap-3 mb-5">
              {[
                { label: 'Transactions',   value: pgSettlement.summary.txn_count.toLocaleString(), color: 'text-gray-800' },
                { label: 'Gross Amount',   value: `₹${Number(pgSettlement.summary.gross_total).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`, color: 'text-gray-700' },
                { label: 'Gateway Fees',   value: `₹${Number(pgSettlement.summary.fees_total).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`,  color: 'text-red-500'  },
                { label: 'Net Settlement', value: `₹${Number(pgSettlement.summary.net_total).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`,   color: 'text-purple-700 font-bold' },
              ].map(({ label, value, color }) => (
                <div key={label} className="bg-gray-50 rounded-lg p-3 text-center">
                  <div className={`text-lg font-bold ${color}`}>{value}</div>
                  <div className="text-xs text-gray-500 mt-0.5">{label}</div>
                </div>
              ))}
            </div>
          )}

          {/* Date-wise table */}
          {!pgSettlement ? (
            <p className="text-sm text-gray-400 text-center py-8">Set a date range and click Load</p>
          ) : pgSettlement.rows.length === 0 ? (
            <p className="text-sm text-gray-400 text-center py-8">No PG data for the selected range</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-200">
                    <th className="table-th">Date</th>
                    <th className="table-th text-right">Transactions</th>
                    <th className="table-th text-right">Gross Amount</th>
                    <th className="table-th text-right text-red-500">Gateway Fees</th>
                    <th className="table-th text-right text-purple-700">Net Settlement</th>
                  </tr>
                </thead>
                <tbody>
                  {pgSettlement.rows.map(r => (
                    <tr key={r.date} className="hover:bg-purple-50 border-b border-gray-100">
                      <td className="table-td font-mono">{r.date}</td>
                      <td className="table-td text-right">{r.txn_count.toLocaleString()}</td>
                      <td className="table-td text-right text-gray-700">₹{Number(r.gross_total).toLocaleString('en-IN', { minimumFractionDigits: 2 })}</td>
                      <td className="table-td text-right text-red-500">₹{Number(r.fees_total).toLocaleString('en-IN', { minimumFractionDigits: 2 })}</td>
                      <td className="table-td text-right font-semibold text-purple-700">₹{Number(r.net_total).toLocaleString('en-IN', { minimumFractionDigits: 2 })}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ── Side Reports ── */}
      {tab === 'side-report' && (
        <div className="space-y-4 max-w-lg">
          <div className="card bg-blue-50 border border-blue-100">
            <p className="text-xs font-semibold text-blue-700 mb-3">Filters — applies to both reports below</p>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-xs text-gray-500 block mb-1">Partner</label>
                <select className="select" value={filters.partner} onChange={e => upd({ partner: e.target.value })}>
                  <option value="">All</option>
                  {partners.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}
                </select>
              </div>
              <div />
              <div>
                <label className="text-xs text-gray-500 block mb-1">From Date</label>
                <input type="date" className="input" value={filters.from_date} onChange={e => upd({ from_date: e.target.value })} />
              </div>
              <div>
                <label className="text-xs text-gray-500 block mb-1">To Date</label>
                <input type="date" className="input" value={filters.to_date} onChange={e => upd({ to_date: e.target.value })} />
              </div>
            </div>
          </div>

          <div className="card">
            <h2 className="font-semibold text-gray-700 mb-1">Bank Side — Full Report</h2>
            <p className="text-xs text-gray-400 mb-4">
              Every bank-side transaction: matched, unmatched, failed, duplicates, fees, fund transfers — all statuses, all row types.
            </p>
            <button onClick={() => exportSideReport('bank')} disabled={loading}
              className="btn-primary w-full flex items-center justify-center gap-2">
              <Download size={15} /> Download Bank Full Report
            </button>
          </div>

          <div className="card">
            <h2 className="font-semibold text-gray-700 mb-1">Internal Side — Full Report</h2>
            <p className="text-xs text-gray-400 mb-4">
              Every internal dump transaction: matched, unmatched, failed, internally matched, duplicates — all statuses.
            </p>
            <button onClick={() => exportSideReport('internal')} disabled={loading}
              className="btn-primary w-full flex items-center justify-center gap-2">
              <Download size={15} /> Download Internal Full Report
            </button>
          </div>
        </div>
      )}

      {/* ── Report Library (all products, backend-driven) ── */}
      {tab === 'library' && (() => {
        const prod = libProducts.find(p => p.id === libProduct) || libProducts[0]
        return (
          <div className="space-y-5 max-w-3xl">
            <div className="card bg-blue-50 border border-blue-100">
              <p className="text-xs font-semibold text-blue-700 mb-3">Filters — Report Library</p>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-gray-500 block mb-1">From Date</label>
                  <input type="date" className="input" value={filters.from_date} onChange={e => upd({ from_date: e.target.value })} />
                </div>
                <div>
                  <label className="text-xs text-gray-500 block mb-1">To Date</label>
                  <input type="date" className="input" value={filters.to_date} onChange={e => upd({ to_date: e.target.value })} />
                </div>
              </div>
              <p className="text-[11px] text-gray-400 mt-2">
                <strong>date</strong> reports use To&nbsp;Date (falling back to the latest date with data) ·{' '}
                <strong>range</strong> reports use From/To (blank = all dates)
              </p>
            </div>

            {/* Product chips — served by the backend, so future products appear automatically */}
            <div className="flex gap-2 flex-wrap">
              {libProducts.map(p => (
                <button key={p.id} onClick={() => setLibProduct(p.id)}
                  className={`px-3.5 py-1.5 rounded-full text-sm font-medium transition-colors border
                    ${(prod?.id === p.id)
                      ? 'bg-primary text-white border-primary'
                      : 'bg-white border-gray-200 text-gray-600 hover:bg-gray-50'}`}>
                  {p.label}
                  {p.kind === 'module' && <span className="ml-1.5 text-[10px] opacity-70">module</span>}
                </button>
              ))}
              {libProducts.length === 0 && (
                <span className="text-sm text-gray-400">Loading report library…</span>
              )}
            </div>

            {prod && (
              <div className="card">
                <h2 className="font-semibold text-gray-700 mb-1">{prod.label} — Reports</h2>
                <p className="text-xs text-gray-400 mb-3">
                  {prod.kind === 'core'
                    ? 'Built from the core reconciliation ledger for this product’s partners.'
                    : 'Built from this product’s own reconciliation tables.'}
                </p>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {prod.reports.map(rep => (
                    <button key={rep.id} onClick={() => downloadLibReport(prod.id, rep)}
                      className="border border-gray-100 rounded-lg p-3 text-left hover:border-primary/40 hover:shadow-sm transition-all group">
                      <div className="flex items-center gap-2">
                        <Download size={13} className="text-gray-300 group-hover:text-primary shrink-0" />
                        <span className="text-sm font-medium text-gray-700">{rep.label}</span>
                        <span className={`ml-auto text-[10px] px-1.5 py-0.5 rounded-full font-semibold shrink-0
                          ${rep.scope === 'date' ? 'bg-blue-50 text-blue-600'
                            : rep.scope === 'range' ? 'bg-purple-50 text-purple-600'
                            : 'bg-gray-100 text-gray-500'}`}>
                          {rep.scope === 'all' ? 'snapshot' : rep.scope}
                        </span>
                      </div>
                      <div className="text-[11px] text-gray-400 mt-1 leading-snug">{rep.desc}</div>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        )
      })()}

      {/* ── SRC Reports ── */}
      {/* ── Kiosk Recon ── */}
      {tab === 'kiosk' && (
        <div className="space-y-5 max-w-2xl">
          <div className="card bg-blue-50 border border-blue-100">
            <p className="text-xs font-semibold text-blue-700 mb-3">Filters — SBI Kiosk Reports</p>
            <div className="grid grid-cols-3 gap-3">
              <div>
                <label className="text-xs text-gray-500 block mb-1">From Date</label>
                <input type="date" className="input" value={filters.from_date} onChange={e => upd({ from_date: e.target.value })} />
              </div>
              <div>
                <label className="text-xs text-gray-500 block mb-1">To Date</label>
                <input type="date" className="input" value={filters.to_date} onChange={e => upd({ to_date: e.target.value })} />
              </div>
              <div>
                <label className="text-xs text-gray-500 block mb-1">Side</label>
                <select className="select" value={filters.side} onChange={e => upd({ side: e.target.value })}>
                  <option value="">Both</option>
                  <option value="bank">Bank</option>
                  <option value="internal">Internal</option>
                </select>
              </div>
            </div>
          </div>

          {/* SBI Kiosk reconciles in its OWN result tables (P01–P04), not the core
              transactions ledger — so the old generic ?partner=kiosk exports came back
              blank. These export the real recon results, by process. */}
          <div className="card">
            <h2 className="font-semibold text-gray-700 mb-1">SBI Kiosk Recon Exports</h2>
            <p className="text-xs text-gray-400 mb-3">
              Each process exports its real reconciliation results. Uses <strong>To Date</strong> as the
              recon date when set; otherwise exports all dates.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {[
                { p: 'p01', label: 'Bank vs Settlement (P01)',  file: 'sbi_bank_vs_settlement' },
                { p: 'p02', label: 'Bank vs Transaction (P02)', file: 'sbi_bank_vs_transaction' },
                { p: 'p03', label: 'CSP · Txn · Bank (P03)',    file: 'sbi_csp_txn_bank' },
                { p: 'p04', label: 'Wallet Balance (P04)',      file: 'sbi_wallet_balance' },
              ].map(({ p, label, file }) => (
                <button key={p} onClick={() => {
                  const params = new URLSearchParams({ process: p })
                  if (filters.to_date) params.append('recon_date', filters.to_date)
                  api.get(`/sbi/export?${params}`, { responseType: 'blob' })
                    .then(r => {
                      const used = r.headers['x-recon-date']
                      if (used === 'none') { toast(`No ${p.toUpperCase()} data uploaded/reconciled yet`, { icon: 'ℹ️' }); return }
                      _download(r.data, `${file}${used && used !== 'none' ? '_' + used : ''}.xlsx`)
                      if (filters.to_date && used && used !== filters.to_date)
                        toast(`No ${p.toUpperCase()} data for ${filters.to_date} — exported latest (${used})`, { icon: 'ℹ️' })
                    })
                    .catch(() => toast.error('Export failed'))
                }} className="btn-ghost flex items-center gap-2 justify-start">
                  <Download size={14} /> {label}
                </button>
              ))}
            </div>
            <button onClick={() => {
              const params = new URLSearchParams({ process: 'all' })
              if (filters.to_date) params.append('recon_date', filters.to_date)
              api.get(`/sbi/export?${params}`, { responseType: 'blob' })
                .then(r => {
                  const used = r.headers['x-recon-date']
                  _download(r.data, `sbi_all_processes${used && used !== 'none' ? '_' + used : ''}.xlsx`)
                  if (used === 'none') toast('No SBI recon results yet — run P01–P04 first', { icon: 'ℹ️' })
                  else if (filters.to_date && used && used !== filters.to_date)
                    toast(`No data for ${filters.to_date} — exported latest (${used})`, { icon: 'ℹ️' })
                })
                .catch(() => toast.error('Export failed'))
            }} className="btn-primary flex items-center gap-2 mt-3">
              <Download size={14} /> Download All Processes (multi-sheet)
            </button>
          </div>

          {/* Report library — served by GET /sbi/report-options; each tile is one
              ready-made workbook from GET /sbi/report?type=… */}
          <div className="card">
            <h2 className="font-semibold text-gray-700 mb-1">Report Library</h2>
            <p className="text-xs text-gray-400 mb-3">
              Ready-made SBI Kiosk reports. <strong>date</strong> reports use To&nbsp;Date
              (falling back to the latest date with data); <strong>range</strong> reports use
              From/To — leave blank for all dates.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {sbiReports.map(rep => (
                <button key={rep.id} onClick={() => downloadSbiReport(rep)}
                  className="border border-gray-100 rounded-lg p-3 text-left hover:border-primary/40 hover:shadow-sm transition-all group">
                  <div className="flex items-center gap-2">
                    <Download size={13} className="text-gray-300 group-hover:text-primary shrink-0" />
                    <span className="text-sm font-medium text-gray-700">{rep.label}</span>
                    <span className={`ml-auto text-[10px] px-1.5 py-0.5 rounded-full font-semibold shrink-0
                      ${rep.scope === 'date' ? 'bg-blue-50 text-blue-600' : 'bg-purple-50 text-purple-600'}`}>
                      {rep.scope}
                    </span>
                  </div>
                  <div className="text-[11px] text-gray-400 mt-1 leading-snug">{rep.desc}</div>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {tab === 'src' && (
        <div className="card max-w-lg">
          <h2 className="font-semibold text-gray-700 mb-4">SRC Code Report</h2>
          <div className="grid grid-cols-2 gap-3 mb-4">
            <div>
              <label className="text-xs text-gray-400 block mb-1">Partner</label>
              <select className="select" value={filters.partner} onChange={e => upd({ partner: e.target.value })}>
                <option value="">All</option>
                {partners.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}
              </select>
            </div>
            <div>
              <label className="text-xs text-gray-400 block mb-1">SRC Code</label>
              <select className="select" value={srcCode} onChange={e => setSrcCode(e.target.value)}>
                <option value="">All SRC-assigned</option>
                {activeSrcCodes.map(c => <option key={c} value={c}>{c.replace(/_/g, ' ')}</option>)}
              </select>
            </div>
            <div>
              <label className="text-xs text-gray-400 block mb-1">From Date</label>
              <input type="date" className="input" value={filters.from_date} onChange={e => upd({ from_date: e.target.value })} />
            </div>
            <div>
              <label className="text-xs text-gray-400 block mb-1">To Date</label>
              <input type="date" className="input" value={filters.to_date} onChange={e => upd({ to_date: e.target.value })} />
            </div>
          </div>

          {activeSrcCodes.length === 0 ? (
            <p className="text-xs text-gray-400 mb-4 italic">No SRC-assigned transactions found for the selected filters.</p>
          ) : (
            <p className="text-xs text-gray-500 mb-4">{activeSrcCodes.length} SRC code{activeSrcCodes.length > 1 ? 's' : ''} in use: {activeSrcCodes.join(', ')}</p>
          )}

          <button onClick={exportSrcReport}
            className="btn-primary w-full flex items-center justify-center gap-2">
            <Download size={15} /> Download SRC Report
          </button>
        </div>
      )}

      {/* ── Export Data ── */}
      {tab === 'export' && (
        <div className="space-y-4 max-w-lg">

          {/* Shared date range / partner for all export cards */}
          <div className="card bg-blue-50 border border-blue-100">
            <p className="text-xs font-semibold text-blue-700 mb-3">Shared Filters — applies to all exports below</p>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-xs text-gray-500 block mb-1">Partner</label>
                <select className="select" value={filters.partner} onChange={e => upd({ partner: e.target.value })}>
                  <option value="">All</option>
                  {partners.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}
                </select>
              </div>
              <div />
              <div>
                <label className="text-xs text-gray-500 block mb-1">From Date</label>
                <input type="date" className="input" value={filters.from_date} onChange={e => upd({ from_date: e.target.value })} />
              </div>
              <div>
                <label className="text-xs text-gray-500 block mb-1">To Date</label>
                <input type="date" className="input" value={filters.to_date} onChange={e => upd({ to_date: e.target.value })} />
              </div>
            </div>
          </div>

          {/* Open Items */}
          <div className="card">
            <h2 className="font-semibold text-gray-700 mb-1">Export Open Items</h2>
            <p className="text-xs text-gray-400 mb-4">Unmatched / SRC-assigned / matched rows as Excel</p>
            <div className="grid grid-cols-2 gap-3 mb-4">
              <div>
                <label className="text-xs text-gray-400 block mb-1">Side</label>
                <select className="select" value={filters.side} onChange={e => upd({ side: e.target.value })}>
                  <option value="">Both</option>
                  <option value="bank">Bank</option>
                  <option value="internal">Internal</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-gray-400 block mb-1">Status</label>
                <select className="select" value={filters.recon_status} onChange={e => upd({ recon_status: e.target.value })}>
                  <option value="open">Open / Unmatched</option>
                  <option value="matched">Matched</option>
                  <option value="all">All</option>
                </select>
              </div>
            </div>
            <button onClick={exportOpenItems} className="btn-primary w-full flex items-center justify-center gap-2">
              <Download size={15} /> Download Open Items
            </button>
          </div>

          {/* Matched Pairs */}
          <div className="card">
            <h2 className="font-semibold text-gray-700 mb-1">Export Matched Pairs</h2>
            <p className="text-xs text-gray-400 mb-4">Bank row + Internal row side-by-side per match. Requires a partner.</p>
            <button
              disabled={!filters.partner}
              onClick={exportMatchedPairs}
              className="btn-primary w-full flex items-center justify-center gap-2 disabled:opacity-50">
              <Download size={15} /> Download Matched Pairs
            </button>
          </div>

          {/* EOD Summary */}
          <div className="card">
            <h2 className="font-semibold text-gray-700 mb-1">EOD Summary Report</h2>
            <p className="text-xs text-gray-400 mb-4">3-sheet Excel: headline numbers, open items, SRC breakdown. Uses <strong>To Date</strong> as the EOD date.</p>
            <button
              disabled={!filters.partner || !filters.to_date}
              onClick={exportEod}
              className="btn-primary w-full flex items-center justify-center gap-2 disabled:opacity-50">
              <Download size={15} /> Download EOD Summary
            </button>
          </div>
        </div>
      )}

      {/* ── Scheduled (self-service automated reports) ── */}
      {tab === 'evalue' && (
        <div className="space-y-4 max-w-2xl">
          <div className="card">
            <div className="flex items-center gap-2 mb-1"><Wallet size={16} className="text-primary" /><h2 className="font-semibold text-gray-700">E-Value Reports</h2></div>
            <p className="text-xs text-gray-400 mb-4">Per-account & all-accounts summary, matched pairs, exceptions (twice-credit, wrong-amount), unmatched, fees/debits, interbank and full classified exports.</p>
            <div className="flex gap-2 flex-wrap">
              <button onClick={() => api.get('/evalue/report/export?report=summary', { responseType: 'blob' }).then(r => _download(r.data, 'evalue_summary.xlsx')).catch(() => toast.error('Export failed'))} className="btn-primary flex items-center gap-2"><Download size={14} /> Summary</button>
              <button onClick={() => api.get('/evalue/report/export?report=exceptions', { responseType: 'blob' }).then(r => _download(r.data, 'evalue_exceptions.xlsx')).catch(() => toast.error('Export failed'))} className="btn-ghost flex items-center gap-2"><Download size={14} /> Exceptions</button>
              <Link to="/evalue" className="btn-ghost flex items-center gap-2"><ExternalLink size={14} /> Open E-Value Recon</Link>
            </div>
          </div>
        </div>
      )}
      {tab === 'bbps' && (
        <div className="space-y-4 max-w-2xl">
          <div className="card">
            <div className="flex items-center gap-2 mb-1"><Receipt size={16} className="text-primary" /><h2 className="font-semibold text-gray-700">BBPS Reports</h2></div>
            <p className="text-xs text-gray-400 mb-4">Operator vs internal reconciliation across Moneyart &amp; Levin, with failed/refund exception breakdown. Two-sheet classified export.</p>
            <div className="flex gap-2 flex-wrap">
              <button onClick={() => api.get('/bbps/export', { responseType: 'blob' }).then(r => _download(r.data, 'bbps_recon.xlsx')).catch(() => toast.error('Export failed'))} className="btn-primary flex items-center gap-2"><Download size={14} /> Export Recon</button>
              <Link to="/bbps" className="btn-ghost flex items-center gap-2"><ExternalLink size={14} /> Open BBPS Recon</Link>
            </div>
          </div>
        </div>
      )}
      {tab === 'insights' && (
        <div className="space-y-4 max-w-2xl">
          <div className="card">
            <div className="flex items-center gap-2 mb-1"><TrendingUp size={16} className="text-primary" /><h2 className="font-semibold text-gray-700">Insights & Manager Reports</h2></div>
            <p className="text-xs text-gray-400 mb-4">3-month match-rate trend, D+7 ageing escalation with raise-with-partner email drafts, monthly reconciliation certificate (PDF), pre-recon sanity check and settlement carry-forward.</p>
            <Link to="/insights" className="btn-primary flex items-center gap-2"><ExternalLink size={14} /> Open Insights & Controls</Link>
          </div>
        </div>
      )}
      {tab === 'scheduled' && <ScheduledReports partners={partners} />}
    </div>
  )
}
