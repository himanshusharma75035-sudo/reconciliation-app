import React, { useState, useEffect, useMemo, useCallback } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  TrendingUp, AlertTriangle, CheckCircle, RefreshCw, Trash2,
  X, Filter, CreditCard, ArrowRight, ChevronRight, ArrowUpRight,
  ArrowDownRight, Clock, Zap, BarChart2, Wallet, Receipt, Landmark
} from 'lucide-react'
import api from '../utils/api'
import toast from 'react-hot-toast'
import { PRODUCTS } from '../productRegistry'
import { filterProductsByAccess } from '../utils/permissions'

// ── Formatters ─────────────────────────────────────────────────────────────────

function fmtINR(n) {
  if (n == null || isNaN(n)) return '₹0'
  const abs = Math.abs(Number(n))
  if (abs >= 10000000) return `₹${(abs / 10000000).toFixed(2)}Cr`
  if (abs >= 100000)   return `₹${(abs / 100000).toFixed(2)}L`
  if (abs >= 1000)     return `₹${(abs / 1000).toFixed(1)}K`
  return `₹${abs.toLocaleString('en-IN', { minimumFractionDigits: 0 })}`
}

function thisMonth() {
  const now = new Date()
  const y = now.getFullYear(), m = String(now.getMonth() + 1).padStart(2, '0')
  return { from: `${y}-${m}-01`, to: new Date(y, now.getMonth() + 1, 0).toISOString().split('T')[0] }
}
function lastMonth() {
  const now = new Date(), d = new Date(now.getFullYear(), now.getMonth(), 0)
  const y = d.getFullYear(), m = String(d.getMonth() + 1).padStart(2, '0')
  return { from: `${y}-${m}-01`, to: d.toISOString().split('T')[0] }
}
function rangeForYearMonth(year, month) {
  const y = parseInt(year), m = parseInt(month)
  const mm = String(m).padStart(2, '0')
  const lastDay = new Date(y, m, 0).getDate()
  return { from: `${y}-${mm}-01`, to: `${y}-${mm}-${lastDay}` }
}

const MONTHS = ['January','February','March','April','May','June',
                'July','August','September','October','November','December']
const YEAR_OPTIONS = Array.from({ length: 4 }, (_, i) => new Date().getFullYear() - i)

// ── Aggregation ────────────────────────────────────────────────────────────────

function sumRows(rows = []) {
  return rows.reduce((acc, r) => ({
    txn:          acc.txn          + (r.txn_total      || 0),
    matched:      acc.matched      + (r.matched        || 0),
    open:         acc.open         + (r.unmatched      || 0),
    failed:       acc.failed       + (r.failed         || 0),
    src:          acc.src          + (r.src_assigned   || 0),
    amtMatched:   acc.amtMatched   + (r.amount_matched || 0),
    amtOpen:      acc.amtOpen      + (r.amount_open    || 0),
  }), { txn: 0, matched: 0, open: 0, failed: 0, src: 0, amtMatched: 0, amtOpen: 0 })
}

function RateBar({ rate, size = 'md' }) {
  const color = rate >= 95 ? '#16a34a' : rate >= 75 ? '#d97706' : '#ef4444'
  const bg    = rate >= 95 ? '#dcfce7' : rate >= 75 ? '#fef3c7' : '#fee2e2'
  const h = size === 'sm' ? 'h-1' : 'h-1.5'
  return (
    <div className="flex items-center gap-2">
      <div className={`flex-1 ${h} rounded-full bg-gray-100`}>
        <div className={`${h} rounded-full transition-all`} style={{ width: `${Math.min(rate, 100)}%`, background: color }} />
      </div>
      <span className="text-xs font-semibold tabular-nums" style={{ color, minWidth: 36 }}>
        {rate.toFixed(1)}%
      </span>
    </div>
  )
}

// ── Zone 1 — KPI Cards ─────────────────────────────────────────────────────────

function KpiStrip({ data, allPartners }) {
  const totals = useMemo(() => {
    let txn = 0, matched = 0, open = 0, amtMatched = 0, amtOpen = 0
    for (const p of allPartners) {
      const s = sumRows(data?.internal?.[p])
      txn       += s.txn
      matched   += s.matched
      open      += s.open
      amtMatched += s.amtMatched
      amtOpen   += s.amtOpen
    }
    const rate = txn > 0 ? (matched / txn) * 100 : 0
    return { txn, matched, open, amtMatched, amtOpen, rate }
  }, [data, allPartners])

  const kpis = [
    {
      label: 'Total Transactions',
      value: totals.txn.toLocaleString(),
      sub: fmtINR(totals.amtMatched + totals.amtOpen) + ' volume',
      icon: BarChart2,
      iconBg: 'bg-primary/10',
      iconColor: 'text-primary',
      color: 'text-gray-900',
    },
    {
      label: 'Matched',
      value: totals.matched.toLocaleString(),
      sub: fmtINR(totals.amtMatched),
      icon: CheckCircle,
      iconBg: 'bg-green-50',
      iconColor: 'text-green-600',
      color: 'text-green-700',
      href: '/open-items?recon_status=matched_all',
    },
    {
      label: 'Open Items',
      value: totals.open.toLocaleString(),
      sub: fmtINR(totals.amtOpen) + ' at risk',
      icon: AlertTriangle,
      iconBg: 'bg-red-50',
      iconColor: 'text-red-500',
      color: 'text-red-600',
      href: '/open-items?recon_status=unmatched',
      urgent: totals.open > 0,
    },
    {
      label: 'Match Rate',
      value: `${totals.rate.toFixed(1)}%`,
      sub: totals.rate >= 95 ? 'Excellent' : totals.rate >= 75 ? 'Needs attention' : 'Critical — act now',
      icon: TrendingUp,
      iconBg: totals.rate >= 95 ? 'bg-green-50' : totals.rate >= 75 ? 'bg-amber-50' : 'bg-red-50',
      iconColor: totals.rate >= 95 ? 'text-green-600' : totals.rate >= 75 ? 'text-amber-600' : 'text-red-600',
      color: totals.rate >= 95 ? 'text-green-700' : totals.rate >= 75 ? 'text-amber-700' : 'text-red-600',
    },
  ]

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-5">
      {kpis.map(({ label, value, sub, icon: Icon, iconBg, iconColor, color, href, urgent }) => {
        const inner = (
          <div className="card p-4 h-full">
            <div className="flex items-start justify-between mb-3">
              <div className={`p-2 rounded-lg ${iconBg}`}>
                <Icon size={17} className={iconColor} />
              </div>
              {urgent && (
                <span className="text-xs font-medium text-red-500 bg-red-50 px-1.5 py-0.5 rounded-full animate-pulse">
                  Action needed
                </span>
              )}
              {href && !urgent && (
                <ArrowUpRight size={13} className="text-gray-300 group-hover:text-gray-500 transition-colors" />
              )}
            </div>
            <div className={`text-2xl font-bold ${color} tabular-nums`}>{value}</div>
            <div className="text-xs text-gray-400 mt-0.5 font-medium">{label}</div>
            <div className="text-xs text-gray-300 mt-1">{sub}</div>
          </div>
        )
        return href ? (
          <Link key={label} to={href} className="group block h-full hover:shadow-md transition-shadow rounded-xl">
            {inner}
          </Link>
        ) : (
          <div key={label}>{inner}</div>
        )
      })}
    </div>
  )
}

// ── Zone 2 — Product Tabs ──────────────────────────────────────────────────────

// Derived from the central product registry (single source of truth — audit F5).
// Adding a product to productRegistry.js automatically adds its tab here.
const PRODUCT_DEFS = PRODUCTS.map(p => ({
  key: p.id,
  label: p.label,
  fallback: p.partners || [],
  module: p.kind === 'module',   // kiosk/evalue/bbps — render their own /summary breakdown, not the core ledger
}))

function PartnerRow({ slug, displayName, data }) {
  const navigate = useNavigate()
  const intl = sumRows(data?.internal?.[slug])
  const bank = sumRows(data?.bank?.[slug])
  const rate = intl.txn > 0 ? (intl.matched / intl.txn) * 100 : 0
  // Zero-data banks are shown as muted rows (not hidden) so every configured bank
  // for a product — e.g. all of DMT's Fino / Airtel / Axis / Levin — is always listed.
  const isEmpty = intl.txn === 0 && bank.txn === 0

  const rateColor = rate >= 95 ? 'text-green-600' : rate >= 75 ? 'text-amber-600' : 'text-red-500'
  const rateBg    = rate >= 95 ? 'bg-green-50'    : rate >= 75 ? 'bg-amber-50'    : 'bg-red-50'

  return (
    <tr
      className={`border-b border-gray-50 hover:bg-gray-50/80 transition-colors cursor-pointer group ${isEmpty ? 'opacity-50' : ''}`}
      onClick={() => navigate(`/open-items?partner=${slug}&recon_status=all`)}
    >
      <td className="py-3 px-5">
        <div className="font-semibold text-gray-800 text-sm">{displayName}</div>
        {intl.open > 0 && (
          <div className="text-xs text-red-400 mt-0.5">{intl.open.toLocaleString()} open · {fmtINR(intl.amtOpen)}</div>
        )}
      </td>
      <td className="py-3 px-4 text-right">
        <div className="text-sm font-medium text-gray-700 tabular-nums">{intl.txn.toLocaleString()}</div>
        {intl.amtMatched + intl.amtOpen > 0 && (
          <div className="text-xs text-gray-400">{fmtINR(intl.amtMatched + intl.amtOpen)}</div>
        )}
      </td>
      <td className="py-3 px-4 text-right">
        <div className="text-sm font-medium text-gray-500 tabular-nums">{bank.txn.toLocaleString()}</div>
      </td>
      <td className="py-3 px-4 text-right">
        <div className="text-sm font-semibold text-green-600 tabular-nums">{intl.matched.toLocaleString()}</div>
        {intl.amtMatched > 0 && <div className="text-xs text-green-400">{fmtINR(intl.amtMatched)}</div>}
      </td>
      <td className="py-3 px-4 text-right">
        <div className={`text-sm font-semibold tabular-nums ${intl.open > 0 ? 'text-red-500' : 'text-gray-300'}`}>
          {intl.open.toLocaleString()}
        </div>
        {intl.failed > 0 && <div className="text-xs text-gray-400">{intl.failed} failed</div>}
      </td>
      <td className="py-3 px-5">
        <div className="min-w-[100px]">
          <RateBar rate={rate} size="sm" />
        </div>
      </td>
      <td className="py-3 px-3 text-right text-gray-200 group-hover:text-gray-400 transition-colors">
        <ChevronRight size={14} />
      </td>
    </tr>
  )
}

function ProductTable({ partners, partnerNames, data }) {
  const visible = partners.filter(slug => {
    const i = sumRows(data?.internal?.[slug])
    const b = sumRows(data?.bank?.[slug])
    return i.txn > 0 || b.txn > 0
  })

  if (visible.length === 0) {
    return (
      <div className="text-center py-12">
        <div className="text-3xl mb-3">📂</div>
        <div className="text-sm font-medium text-gray-500">No data for this product yet</div>
        <div className="text-xs text-gray-400 mt-1">Upload bank statements and internal dumps to begin</div>
        <Link to="/upload" className="inline-flex items-center gap-1.5 mt-3 text-xs text-primary hover:underline font-medium">
          Upload Files <ArrowRight size={11} />
        </Link>
      </div>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="border-b border-gray-100 bg-gray-50/50">
            <th className="text-left py-2.5 px-5 text-xs font-semibold text-gray-400 uppercase tracking-wide">Partner</th>
            <th className="text-right py-2.5 px-4 text-xs font-semibold text-gray-400 uppercase tracking-wide">Internal</th>
            <th className="text-right py-2.5 px-4 text-xs font-semibold text-gray-400 uppercase tracking-wide">Bank</th>
            <th className="text-right py-2.5 px-4 text-xs font-semibold text-green-500 uppercase tracking-wide">Matched</th>
            <th className="text-right py-2.5 px-4 text-xs font-semibold text-red-400 uppercase tracking-wide">Open</th>
            <th className="py-2.5 px-5 text-xs font-semibold text-gray-400 uppercase tracking-wide">Match Rate</th>
            <th className="py-2.5 px-3" />
          </tr>
        </thead>
        <tbody>
          {partners.map(slug => (
            <PartnerRow
              key={slug}
              slug={slug}
              displayName={partnerNames[slug] || slug.charAt(0).toUpperCase() + slug.slice(1)}
              data={data}
            />
          ))}
        </tbody>
      </table>
    </div>
  )
}

// Breakdown table for the dedicated modules (E-Value per account, BBPS per
// operator) — same column layout as ProductTable, fed by their summary endpoints.
function ModuleBreakdown({ module, summary }) {
  const navigate = useNavigate()
  if (summary === undefined) return <div className="text-center py-10 text-sm text-gray-400">Loading {module === 'evalue' ? 'E-Value' : 'BBPS'} summary…</div>

  let rows = []
  if (module === 'evalue') {
    rows = (summary?.accounts || []).map(a => {
      const matched = (a.matched_online || 0) + (a.matched_cash || 0) + (a.matched_manual || 0) + (a.interbank_matched || 0)
      const bank = a.bank_credits || 0
      return { name: a.account_number || a.reco_acc_no || a.bank_name || 'Account', sub: a.bank_name, internal: a.loads ?? null, bank, matched, open: Math.max(bank - matched, 0), to: '/evalue' }
    })
  } else {
    rows = (summary?.by_provider || []).map(p => {
      const bank = p.bank_rows || 0, matched = p.reconciled || 0
      return { name: (p.provider || '').replace(/^./, c => c.toUpperCase()), sub: 'operator', internal: null, bank, matched, open: Math.max(bank - matched, 0), to: '/bbps' }
    })
  }
  const tot = rows.reduce((a, r) => ({ bank: a.bank + r.bank, matched: a.matched + r.matched, open: a.open + r.open }), { bank: 0, matched: 0, open: 0 })
  const rate = tot.bank > 0 ? Math.round(tot.matched / tot.bank * 100) : 0
  const rateColor = rate >= 95 ? 'text-green-600' : rate >= 75 ? 'text-amber-600' : 'text-red-500'

  if (rows.length === 0) return (
    <div className="text-center py-12">
      <div className="text-3xl mb-3">📂</div>
      <div className="text-sm font-medium text-gray-500">No {module === 'evalue' ? 'E-Value' : 'BBPS'} data yet</div>
      <Link to={module === 'evalue' ? '/evalue' : '/bbps'} className="inline-flex items-center gap-1.5 mt-3 text-xs text-primary hover:underline font-medium">Open {module === 'evalue' ? 'E-Value' : 'BBPS'} <ArrowRight size={11} /></Link>
    </div>
  )

  return (
    <>
      <div className="flex items-center gap-6 px-5 py-2.5 bg-gray-50 border-b border-gray-100 text-xs text-gray-600 flex-wrap">
        <span className="font-semibold text-gray-500 uppercase tracking-wide text-xs">{module === 'evalue' ? 'E-Value' : 'BBPS'} Totals</span>
        <span>{module === 'evalue' ? 'Credits' : 'Operator'}: <strong className="text-gray-800">{tot.bank.toLocaleString()}</strong></span>
        <span>Matched: <strong className="text-green-600">{tot.matched.toLocaleString()}</strong></span>
        <span>Open: <strong className={tot.open > 0 ? 'text-red-500' : 'text-gray-400'}>{tot.open.toLocaleString()}</strong></span>
        <span>Match Rate: <strong className={rateColor}>{rate}%</strong></span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="border-b border-gray-100 bg-gray-50/50">
              <th className="text-left py-2.5 px-5 text-xs font-semibold text-gray-400 uppercase tracking-wide">{module === 'evalue' ? 'Account' : 'Operator'}</th>
              <th className="text-right py-2.5 px-4 text-xs font-semibold text-gray-400 uppercase tracking-wide">Loads</th>
              <th className="text-right py-2.5 px-4 text-xs font-semibold text-gray-400 uppercase tracking-wide">{module === 'evalue' ? 'Credits' : 'Operator'}</th>
              <th className="text-right py-2.5 px-4 text-xs font-semibold text-green-500 uppercase tracking-wide">Matched</th>
              <th className="text-right py-2.5 px-4 text-xs font-semibold text-red-400 uppercase tracking-wide">Open</th>
              <th className="py-2.5 px-5 text-xs font-semibold text-gray-400 uppercase tracking-wide">Match Rate</th>
              <th className="py-2.5 px-3" />
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => {
              const rr = r.bank > 0 ? (r.matched / r.bank) * 100 : 0
              return (
                <tr key={i} className="border-b border-gray-50 hover:bg-gray-50/80 transition-colors cursor-pointer group" onClick={() => navigate(r.to)}>
                  <td className="py-3 px-5"><div className="font-semibold text-gray-800 text-sm">{r.name}</div>{r.sub && <div className="text-xs text-gray-400 mt-0.5">{r.sub}</div>}</td>
                  <td className="py-3 px-4 text-right text-sm text-gray-700 tabular-nums">{r.internal != null ? r.internal.toLocaleString() : '—'}</td>
                  <td className="py-3 px-4 text-right text-sm text-gray-500 tabular-nums">{r.bank.toLocaleString()}</td>
                  <td className="py-3 px-4 text-right text-sm font-semibold text-green-600 tabular-nums">{r.matched.toLocaleString()}</td>
                  <td className={`py-3 px-4 text-right text-sm font-semibold tabular-nums ${r.open > 0 ? 'text-red-500' : 'text-gray-300'}`}>{r.open.toLocaleString()}</td>
                  <td className="py-3 px-5"><div className="min-w-[100px]"><RateBar rate={rr} size="sm" /></div></td>
                  <td className="py-3 px-3 text-right text-gray-200 group-hover:text-gray-400 transition-colors"><ChevronRight size={14} /></td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}

// SBI Kiosk breakdown for Zone 2 — SBI reconciles in its OWN tables across four
// processes (P01 settlement · P02 bank↔txn · P03 CSP↔txn↔bank · P04 wallet balance),
// never the core ledger, so it is fed by /sbi/summary (not dashboard-summary, which
// is why the tab used to show "No data" despite data being present). Same column
// layout as ProductTable, one row per process.
const SBI_PROC_META = [
  { key: 'p01', name: 'P01 · Settlement', sub: 'Wallet withdrawals ↔ bank settlements' },
  { key: 'p02', name: 'P02 · Bank ↔ Txn', sub: 'Bank statement ↔ txn report (by ref)' },
  { key: 'p03', name: 'P03 · CSP flow',   sub: 'Money out ↔ in (CSP + amount)' },
  { key: 'p04', name: 'P04 · Wallet',     sub: 'Wallet balance → action' },
]
function SbiBreakdown({ summary }) {
  const navigate = useNavigate()
  if (summary === undefined) return <div className="text-center py-10 text-sm text-gray-400">Loading SBI Kiosk summary…</div>

  const counts   = summary?.counts || {}
  const uploaded = Object.values(counts).reduce((a, b) => a + (b || 0), 0)
  const procs    = summary?.processes || {}
  const rows     = SBI_PROC_META.map(m => ({ ...m, ...(procs[m.key] || { total: 0, matched: 0, exceptions: 0 }) }))
  const tot      = { total: summary?.total || 0, matched: summary?.matched || 0, open: summary?.exceptions || 0 }
  const rate     = summary?.match_rate
  const rateColor = rate == null ? 'text-gray-400' : rate >= 95 ? 'text-green-600' : rate >= 75 ? 'text-amber-600' : 'text-red-500'

  // Nothing uploaded at all → genuine empty state.
  if (!summary || (!summary.has_data && tot.total === 0)) return (
    <div className="text-center py-12">
      <div className="text-3xl mb-3">📂</div>
      <div className="text-sm font-medium text-gray-500">No SBI Kiosk data yet</div>
      <div className="text-xs text-gray-400 mt-1">Upload bank statement, txn report &amp; KO limits to begin</div>
      <Link to="/sbi-kiosk" className="inline-flex items-center gap-1.5 mt-3 text-xs text-primary hover:underline font-medium">Open SBI Kiosk <ArrowRight size={11} /></Link>
    </div>
  )

  // Data uploaded but the four processes haven't been run yet (no result rows).
  if (tot.total === 0) return (
    <>
      <div className="flex items-center gap-6 px-5 py-2.5 bg-gray-50 border-b border-gray-100 text-xs text-gray-600 flex-wrap">
        <span className="font-semibold text-gray-500 uppercase tracking-wide text-xs">SBI Kiosk</span>
        <span>Uploaded records: <strong className="text-gray-800">{uploaded.toLocaleString()}</strong></span>
        <span className="text-amber-600 font-medium">Not yet reconciled — run P01–P04</span>
      </div>
      <div className="px-5 py-8 text-center">
        <div className="text-sm text-gray-600 mb-1">Data is uploaded but the four processes haven't been run.</div>
        <div className="text-xs text-gray-400 mb-3">
          Bank {(counts.bank_statement || 0).toLocaleString()} · Txn {(counts.txn_reports || 0).toLocaleString()} · KO limits {(counts.ko_limits || 0).toLocaleString()} · CSP {(counts.csp_master || 0).toLocaleString()}
        </div>
        <Link to="/sbi-kiosk" className="inline-flex items-center gap-1.5 text-xs text-primary hover:underline font-medium">Open SBI Kiosk &amp; run processes <ArrowRight size={11} /></Link>
      </div>
    </>
  )

  // Reconciled — per-process breakdown.
  return (
    <>
      <div className="flex items-center gap-6 px-5 py-2.5 bg-gray-50 border-b border-gray-100 text-xs text-gray-600 flex-wrap">
        <span className="font-semibold text-gray-500 uppercase tracking-wide text-xs">SBI Kiosk Totals</span>
        <span>Entries: <strong className="text-gray-800">{tot.total.toLocaleString()}</strong></span>
        <span>Matched: <strong className="text-green-600">{tot.matched.toLocaleString()}</strong></span>
        <span>Open: <strong className={tot.open > 0 ? 'text-red-500' : 'text-gray-400'}>{tot.open.toLocaleString()}</strong></span>
        <span>Match Rate: <strong className={rateColor}>{rate == null ? '—' : rate + '%'}</strong></span>
        {uploaded > 0 && <span className="text-gray-400">Uploaded records: {uploaded.toLocaleString()}</span>}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="border-b border-gray-100 bg-gray-50/50">
              <th className="text-left py-2.5 px-5 text-xs font-semibold text-gray-400 uppercase tracking-wide">Process</th>
              <th className="text-right py-2.5 px-4 text-xs font-semibold text-gray-400 uppercase tracking-wide">Entries</th>
              <th className="text-right py-2.5 px-4 text-xs font-semibold text-green-500 uppercase tracking-wide">Matched</th>
              <th className="text-right py-2.5 px-4 text-xs font-semibold text-red-400 uppercase tracking-wide">Open</th>
              <th className="py-2.5 px-5 text-xs font-semibold text-gray-400 uppercase tracking-wide">Match Rate</th>
              <th className="py-2.5 px-3" />
            </tr>
          </thead>
          <tbody>
            {rows.map(r => {
              const rr = r.total > 0 ? (r.matched / r.total) * 100 : 0
              return (
                <tr key={r.key} className={`border-b border-gray-50 hover:bg-gray-50/80 transition-colors cursor-pointer group ${r.total === 0 ? 'opacity-50' : ''}`} onClick={() => navigate('/sbi-kiosk')}>
                  <td className="py-3 px-5"><div className="font-semibold text-gray-800 text-sm">{r.name}</div><div className="text-xs text-gray-400 mt-0.5">{r.sub}</div></td>
                  <td className="py-3 px-4 text-right text-sm text-gray-700 tabular-nums">{r.total.toLocaleString()}</td>
                  <td className="py-3 px-4 text-right text-sm font-semibold text-green-600 tabular-nums">{r.matched.toLocaleString()}</td>
                  <td className={`py-3 px-4 text-right text-sm font-semibold tabular-nums ${r.exceptions > 0 ? 'text-red-500' : 'text-gray-300'}`}>{r.exceptions.toLocaleString()}</td>
                  <td className="py-3 px-5"><div className="min-w-[100px]"><RateBar rate={rr} size="sm" /></div></td>
                  <td className="py-3 px-3 text-right text-gray-200 group-hover:text-gray-400 transition-colors"><ChevronRight size={14} /></td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}

function ProductTabs({ partnerConfigs, data }) {
  const [activeTab, setActiveTab] = useState('dmt')
  const [modSummary, setModSummary] = useState({})  // { evalue: {...}, bbps: {...}, kiosk: {...} }
  useEffect(() => {
    api.get('/evalue/summary').then(({ data }) => setModSummary(s => ({ ...s, evalue: data }))).catch(() => setModSummary(s => ({ ...s, evalue: null })))
    api.get('/bbps/summary').then(({ data }) => setModSummary(s => ({ ...s, bbps: data }))).catch(() => setModSummary(s => ({ ...s, bbps: null })))
    api.get('/sbi/summary').then(({ data }) => setModSummary(s => ({ ...s, kiosk: data }))).catch(() => setModSummary(s => ({ ...s, kiosk: null })))
  }, [])

  const { productMap, nameMap } = useMemo(() => {
    const nameMap = {}
    const productMap = {}

    // Always start each product with its canonical fallback banks, then merge in
    // any configured partners. This guarantees every bank (e.g. DMT's Airtel) shows
    // a row even when it has no data yet or its partner config is inactive.
    const DEFAULT_NAMES = {
      fino: 'Fino Payments Bank', airtel: 'Airtel Payments Bank', axis: 'Axis Bank', levin: 'Levin',
    }
    for (const def of PRODUCT_DEFS) productMap[def.key] = [...def.fallback]
    for (const slug of new Set(Object.values(productMap).flat())) {
      if (DEFAULT_NAMES[slug]) nameMap[slug] = DEFAULT_NAMES[slug]
    }
    for (const p of partnerConfigs) {
      nameMap[p.slug] = p.display_name
      const prod = p.product || 'dmt'
      if (!productMap[prod]) productMap[prod] = []
      if (!productMap[prod].includes(p.slug)) productMap[prod].push(p.slug)
    }

    // Also include any partner from data that isn't in nameMap
    for (const slug of [...Object.keys(data?.internal || {}), ...Object.keys(data?.bank || {})]) {
      if (!nameMap[slug]) nameMap[slug] = slug.charAt(0).toUpperCase() + slug.slice(1)
    }

    return { productMap, nameMap }
  }, [partnerConfigs, data])

  const openCounts = useMemo(() => {
    const counts = {}
    for (const def of PRODUCT_DEFS) {
      // Module products keep their open counts in their own tables, so the tab
      // badge reads from the /summary endpoints — not the core-ledger rows.
      if (def.key === 'kiosk') { counts.kiosk = modSummary.kiosk?.exceptions || 0; continue }
      if (def.key === 'bbps')  { counts.bbps  = modSummary.bbps?.overall?.exceptions || 0; continue }
      if (def.key === 'evalue') {
        counts.evalue = (modSummary.evalue?.accounts || []).reduce((s, a) =>
          s + Math.max((a.bank_credits || 0) -
            ((a.matched_online || 0) + (a.matched_cash || 0) + (a.matched_manual || 0) + (a.interbank_matched || 0)), 0), 0)
        continue
      }
      const partners = productMap[def.key] || def.fallback
      counts[def.key] = partners.reduce((sum, slug) => sum + sumRows(data?.internal?.[slug]).open, 0)
    }
    return counts
  }, [productMap, data, modSummary])

  return (
    <div className="card overflow-hidden p-0 mb-5">
      {/* Tab bar */}
      <div className="flex items-center border-b border-gray-100 bg-gray-50/50">
        <div className="flex">
          {PRODUCT_DEFS.map(({ key, label }) => {
            const open = openCounts[key] || 0
            const isActive = activeTab === key
            return (
              <button
                key={key}
                onClick={() => setActiveTab(key)}
                className={`flex items-center gap-2 px-6 py-3.5 text-sm font-medium border-b-2 transition-all
                  ${isActive
                    ? 'border-primary text-primary bg-white'
                    : 'border-transparent text-gray-500 hover:text-gray-700'
                  }`}
              >
                {label}
                {open > 0 && (
                  <span className={`text-xs px-1.5 py-0.5 rounded-full font-semibold
                    ${isActive ? 'bg-red-100 text-red-600' : 'bg-gray-200 text-gray-500'}`}>
                    {open.toLocaleString()}
                  </span>
                )}
              </button>
            )
          })}
        </div>
        <div className="ml-auto flex items-center gap-3 px-5 text-xs text-gray-400">
          <span className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-green-500 inline-block" />Matched
          </span>
          <span className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-red-400 inline-block" />Open
          </span>
          <span className="hidden sm:block text-gray-300">Click row → Open Items</span>
        </div>
      </div>

      {/* Module products (SBI Kiosk, E-Value, BBPS) reconcile in their OWN tables,
          so they render a bespoke breakdown from their /summary endpoints instead of
          the core-ledger ProductTable (which would always be empty for them). */}
      {activeTab === 'kiosk' ? (
        <SbiBreakdown summary={modSummary.kiosk} />
      ) : (activeTab === 'evalue' || activeTab === 'bbps') ? (
        <ModuleBreakdown module={activeTab} summary={modSummary[activeTab]} />
      ) : (
      <>
      {/* Totals bar — aggregates all partners in the active product tab */}
      {(() => {
        const partners = productMap[activeTab] || []
        const totIntl = partners.reduce((a, s) => {
          const r = sumRows(data?.internal?.[s])
          return { txn: a.txn + r.txn, matched: a.matched + r.matched, open: a.open + r.open, amtMatched: a.amtMatched + r.amtMatched, amtOpen: a.amtOpen + r.amtOpen }
        }, { txn: 0, matched: 0, open: 0, amtMatched: 0, amtOpen: 0 })
        const totBank = partners.reduce((a, s) => {
          const r = sumRows(data?.bank?.[s])
          return { txn: a.txn + r.txn, matched: a.matched + r.matched, open: a.open + r.open }
        }, { txn: 0, matched: 0, open: 0 })
        const rate = totIntl.txn > 0 ? Math.round(totIntl.matched / totIntl.txn * 100) : 0
        const rateColor = rate >= 95 ? 'text-green-600' : rate >= 75 ? 'text-amber-600' : 'text-red-500'
        if (partners.length === 0) return null
        return (
          <div className="flex items-center gap-6 px-5 py-2.5 bg-gray-50 border-b border-gray-100 text-xs text-gray-600 flex-wrap">
            <span className="font-semibold text-gray-500 uppercase tracking-wide text-xs">
              {PRODUCT_DEFS.find(d => d.key === activeTab)?.label} Totals
            </span>
            <span>Internal: <strong className="text-gray-800">{totIntl.txn.toLocaleString()}</strong></span>
            <span>Bank: <strong className="text-gray-800">{totBank.txn.toLocaleString()}</strong></span>
            <span>Matched: <strong className="text-green-600">{totIntl.matched.toLocaleString()}</strong></span>
            <span>Open: <strong className={totIntl.open > 0 ? 'text-red-500' : 'text-gray-400'}>{totIntl.open.toLocaleString()}</strong></span>
            <span>Match Rate: <strong className={rateColor}>{rate}%</strong></span>
            <span>₹ Matched: <strong className="text-gray-700">{fmtINR(totIntl.amtMatched)}</strong></span>
            <span>₹ Open: <strong className={totIntl.amtOpen > 0 ? 'text-red-500' : 'text-gray-400'}>{fmtINR(totIntl.amtOpen)}</strong></span>
          </div>
        )
      })()}

      <ProductTable
        partners={productMap[activeTab] || []}
        partnerNames={nameMap}
        data={data}
      />
      </>
      )}
    </div>
  )
}

// ── Funds Position (EOD) — director/CFO view ──────────────────────────────────
// Latest statement balance per bank account ≤ the chosen date (T+1 aware: the
// statement date is always shown next to the number). Fed by BankBalanceSnapshot,
// written automatically on every bank upload. Reporting only.
function FundsPositionPanel() {
  const [asOf, setAsOf] = useState(() => new Date().toISOString().split('T')[0])
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async (d) => {
    setLoading(true)
    try {
      const { data: res } = await api.get('/reports/funds-position', { params: { as_of: d } })
      setData(res)
    } catch { /* silent */ } finally { setLoading(false) }
  }, [])
  useEffect(() => { load(asOf) }, [])

  const dl = async () => {
    try {
      const r = await api.get('/reports/funds-position/export', { params: { as_of: asOf }, responseType: 'blob' })
      const url = URL.createObjectURL(r.data)
      const a = document.createElement('a'); a.href = url; a.download = `funds_position_${asOf}.xlsx`; a.click()
      URL.revokeObjectURL(url)
    } catch { toast.error('Export failed') }
  }

  const fmt = n => n == null ? '—' : `₹${Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`
  const rows = data?.rows || []
  const grand = rows.reduce((s, r) => s + (r.closing_balance || 0), 0)

  return (
    <div className="card overflow-hidden p-0 mb-5">
      <div className="flex items-center gap-2.5 px-5 py-4 border-b border-gray-100 bg-gray-50/50 flex-wrap">
        <Landmark size={17} className="text-emerald-600" />
        <h2 className="font-bold text-gray-800 text-sm">Funds Position (EOD)</h2>
        {rows.length > 0 && (
          <span className="text-xs text-gray-500">Total closing: <strong className="text-emerald-700">{fmt(grand)}</strong></span>
        )}
        <div className="ml-auto flex items-center gap-2">
          <input type="date" className="input" value={asOf} onChange={e => setAsOf(e.target.value)} />
          <button onClick={() => load(asOf)} className="btn-ghost text-xs">{loading ? 'Loading…' : 'Apply'}</button>
          <button onClick={dl} className="btn-primary text-xs">⬇ Report</button>
        </div>
      </div>
      {rows.length === 0 ? (
        <div className="px-5 py-6 text-center text-xs text-gray-400 italic">
          No balance snapshots yet — they are recorded automatically from the next bank statement upload onward.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-gray-100 text-gray-400 font-medium bg-gray-50/50">
                <th className="text-left py-2 px-4">Product</th>
                <th className="text-left py-2 px-3">Bank / Partner</th>
                <th className="text-left py-2 px-3">Account</th>
                <th className="text-left py-2 px-3">Statement date</th>
                <th className="text-right py-2 px-3">Opening</th>
                <th className="text-right py-2 px-3">Total DR</th>
                <th className="text-right py-2 px-3">Total CR</th>
                <th className="text-right py-2 px-3 font-semibold">Closing</th>
                <th className="text-right py-2 px-4">Δ vs prev</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className="border-b border-gray-50 hover:bg-emerald-50/40">
                  <td className="py-2 px-4 font-semibold text-gray-700 uppercase">{r.product}</td>
                  <td className="py-2 px-3 text-gray-600 capitalize">{r.partner}</td>
                  <td className="py-2 px-3 font-mono text-gray-500">{r.bank_account || '—'}</td>
                  <td className="py-2 px-3">
                    <span className={r.stale ? 'text-amber-600 font-medium' : 'text-gray-600'}>
                      {r.statement_date}{r.stale && ' ⚠'}
                    </span>
                  </td>
                  <td className="py-2 px-3 text-right text-gray-500 tabular-nums">{fmt(r.opening_balance)}</td>
                  <td className="py-2 px-3 text-right text-red-400 tabular-nums">{fmt(r.total_dr)}</td>
                  <td className="py-2 px-3 text-right text-green-500 tabular-nums">{fmt(r.total_cr)}</td>
                  <td className="py-2 px-3 text-right font-bold text-emerald-700 tabular-nums">{fmt(r.closing_balance)}</td>
                  <td className={`py-2 px-4 text-right tabular-nums ${r.delta > 0 ? 'text-green-600' : r.delta < 0 ? 'text-red-500' : 'text-gray-400'}`}>
                    {r.delta == null ? '—' : (r.delta > 0 ? '+' : '') + fmt(r.delta).replace('₹', '₹')}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="px-5 py-2 text-[11px] text-gray-400 border-t border-gray-50">
            ⚠ = statement older than the selected date (last known balance shown, as per T+1 data availability).
          </div>
        </div>
      )}
    </div>
  )
}

// ── Zone 4 — PG Settlement Panel ──────────────────────────────────────────────

function PGSettlementPanel({ dateRange }) {
  const [rows, setRows]       = useState([])
  const [summary, setSummary] = useState(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = {}
      if (dateRange.from) params.date_from = dateRange.from
      if (dateRange.to)   params.date_to   = dateRange.to
      const { data } = await api.get('/recon/pg-settlement-summary', { params })
      setRows(data.rows || [])
      setSummary(data.summary || null)
    } catch { /* silent */ } finally { setLoading(false) }
  }, [dateRange.from, dateRange.to])

  useEffect(() => { load() }, [load])

  const fmt = n => n == null ? '—'
    : `₹${Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`

  return (
    <div className="card overflow-hidden p-0">
      <div className="flex items-center gap-2.5 px-5 py-4 border-b border-gray-100 bg-gray-50/50">
        <CreditCard size={17} className="text-purple-500" />
        <h2 className="font-bold text-gray-800 text-sm">PG Settlement — Accept Payment (PayU)</h2>
        <Link to="/reports" className="ml-auto text-xs text-purple-500 hover:underline flex items-center gap-1 font-medium">
          Full Report <ArrowRight size={11} />
        </Link>
      </div>
      {summary && (
        <div className="grid grid-cols-4 divide-x divide-gray-100 border-b border-gray-100">
          {[
            { label: 'Transactions',   value: summary.txn_count.toLocaleString(), color: 'text-gray-800' },
            { label: 'Gross Amount',   value: fmt(summary.gross_total),           color: 'text-gray-700' },
            { label: 'Gateway Fees',   value: fmt(summary.fees_total),            color: 'text-red-500'  },
            { label: 'Net Settlement', value: fmt(summary.net_total),             color: 'text-purple-700 font-bold' },
          ].map(({ label, value, color }) => (
            <div key={label} className="px-4 py-3 text-center">
              <div className={`text-lg font-bold ${color}`}>{value}</div>
              <div className="text-xs text-gray-400 mt-0.5">{label}</div>
            </div>
          ))}
        </div>
      )}
      <div className="px-5 py-4">
        {loading ? (
          <div className="text-center py-5 text-gray-400 text-xs">Loading…</div>
        ) : rows.length === 0 ? (
          <div className="text-center py-6 text-gray-400 text-xs italic">
            No PG data — upload a PayU settlement report to begin
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-gray-100 text-gray-400 font-medium">
                  <th className="text-left py-2 px-2">Date</th>
                  <th className="text-right py-2 px-2">Txns</th>
                  <th className="text-right py-2 px-2">Gross</th>
                  <th className="text-right py-2 px-2 text-red-400">Fees</th>
                  <th className="text-right py-2 px-2 text-purple-600">Net Settlement</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(r => (
                  <tr key={r.date} className="border-b border-gray-50 hover:bg-purple-50 cursor-pointer"
                    onClick={() => window.location.href = `${import.meta.env.BASE_URL}open-items?partner=pg&recon_date=${r.date}&recon_status=all`}>
                    <td className="py-2 px-2 font-mono">{r.date}</td>
                    <td className="py-2 px-2 text-right">{r.txn_count.toLocaleString()}</td>
                    <td className="py-2 px-2 text-right text-gray-600">{fmt(r.gross_total)}</td>
                    <td className="py-2 px-2 text-right text-red-400">{fmt(r.fees_total)}</td>
                    <td className="py-2 px-2 text-right font-semibold text-purple-700">{fmt(r.net_total)}</td>
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

// ── Clear Modal ────────────────────────────────────────────────────────────────

function ClearModal({ onClose, onDone, partnerSlugs }) {
  const [filters, setFilters]   = useState({ partner: '', recon_date: '', side: '', recon_status: '' })
  const [step, setStep]         = useState(1)
  const [clearing, setClearing] = useState(false)
  const active = Object.entries(filters).filter(([, v]) => v)

  const handleClear = async () => {
    setClearing(true)
    try {
      const params = new URLSearchParams(Object.fromEntries(active))
      const { data } = await api.delete(`/upload/clear?${params}`)
      toast.success(`Cleared ${data.deleted_transactions} transactions`)
      onDone(); onClose()
    } catch { toast.error('Failed to clear data') } finally { setClearing(false) }
  }

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md p-6">
        <div className="flex items-center justify-between mb-5">
          <div className="flex items-center gap-2"><Trash2 size={17} className="text-red-500" /><h3 className="font-semibold text-gray-800">Clear Data</h3></div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X size={17} /></button>
        </div>
        {step === 1 && (
          <>
            <p className="text-sm text-gray-500 mb-4">Apply filters to delete a specific subset, or leave all blank to clear everything.</p>
            <div className="space-y-3">
              <div><label className="text-xs font-medium text-gray-500 block mb-1">Partner</label>
                <select className="select" value={filters.partner} onChange={e => setFilters({ ...filters, partner: e.target.value })}>
                  <option value="">All partners</option>
                  {partnerSlugs.map(s => <option key={s} value={s}>{s.charAt(0).toUpperCase() + s.slice(1)}</option>)}
                </select>
              </div>
              <div><label className="text-xs font-medium text-gray-500 block mb-1">Recon Date</label>
                <input type="date" className="input" value={filters.recon_date} onChange={e => setFilters({ ...filters, recon_date: e.target.value })} />
              </div>
              <div><label className="text-xs font-medium text-gray-500 block mb-1">Side</label>
                <select className="select" value={filters.side} onChange={e => setFilters({ ...filters, side: e.target.value })}>
                  <option value="">Both sides</option><option value="bank">Bank only</option><option value="internal">Internal only</option>
                </select>
              </div>
            </div>
            {active.length === 0 && (
              <div className="mt-4 bg-red-50 border border-red-100 rounded-lg p-3 text-xs text-red-700">
                ⚠️ No filters — this will delete <strong>all transactions</strong>.
              </div>
            )}
            <div className="flex gap-3 mt-5">
              <button onClick={onClose} className="btn-ghost flex-1">Cancel</button>
              <button onClick={() => setStep(2)} className="flex-1 py-2 px-4 rounded-lg font-medium text-sm bg-red-500 text-white hover:bg-red-600 transition-colors">Review →</button>
            </div>
          </>
        )}
        {step === 2 && (
          <>
            <div className="bg-gray-50 rounded-lg p-4 mb-5 text-sm">
              <p className="font-semibold text-gray-700 mb-2">You are about to delete:</p>
              {active.length === 0
                ? <p className="text-red-600 font-medium">ALL transactions and upload sessions</p>
                : active.map(([k, v]) => <div key={k} className="flex gap-2"><span className="text-gray-400 w-28 capitalize">{k.replace('_',' ')}</span><span className="font-medium text-gray-700">{v}</span></div>)
              }
            </div>
            <p className="text-xs text-red-500 mb-5">This cannot be undone.</p>
            <div className="flex gap-3">
              <button onClick={() => setStep(1)} className="btn-ghost flex-1">← Back</button>
              <button onClick={handleClear} disabled={clearing} className="flex-1 py-2 px-4 rounded-lg font-medium text-sm bg-red-500 text-white hover:bg-red-600 disabled:opacity-50 transition-colors">
                {clearing ? 'Deleting…' : 'Yes, Delete'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

// ── Main Dashboard ─────────────────────────────────────────────────────────────

// ── Dedicated product reconciliations (own engines: E-Value, BBPS, SBI Kiosk) ──
function DedicatedReconPanel({ data = { internal: {}, bank: {} }, partnerConfigs = [] }) {
  const [ev, setEv] = useState(null)
  const [bb, setBb] = useState(null)
  const [sbi, setSbi] = useState(null)
  const [pinned, setPinned] = useState(() => {
    try { return new Set(JSON.parse(localStorage.getItem('pinnedProducts') || '[]')) } catch { return new Set() }
  })
  const [showAll, setShowAll] = useState(false)
  const togglePin = (id) => setPinned(s => {
    const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id)
    try { localStorage.setItem('pinnedProducts', JSON.stringify([...n])) } catch {}
    return n
  })
  useEffect(() => {
    api.get('/evalue/summary').then(({ data }) => setEv(data)).catch(() => {})
    api.get('/bbps/summary').then(({ data }) => setBb(data)).catch(() => {})
    api.get('/sbi/summary').then(({ data }) => setSbi(data)).catch(() => {})
  }, [])
  const evAgg = ev && ev.accounts ? ev.accounts.reduce((a, r) => ({
    credits: a.credits + (r.bank_credits || 0),
    matched: a.matched + (r.matched_online || 0) + (r.matched_cash || 0) + (r.matched_manual || 0) + (r.interbank_matched || 0),
  }), { credits: 0, matched: 0 }) : null

  // product id -> partner slugs (registry partners + any partner mapped to this product)
  const productMap = useMemo(() => {
    const m = {}
    PRODUCTS.forEach(p => { m[p.id] = [...(p.partners || [])] })
    partnerConfigs.forEach(pc => {
      const prod = pc.product || 'dmt'
      if (!m[prod]) m[prod] = []
      if (!m[prod].includes(pc.slug)) m[prod].push(pc.slug)
    })
    return m
  }, [partnerConfigs])

  // Stats per product card. E-Value / BBPS use their own summary endpoints; the
  // rest aggregate the dashboard-summary rows for their partners.
  const statFor = (id) => {
    if (id === 'evalue') return {
      rate: evAgg && evAgg.credits ? Math.round(evAgg.matched / evAgg.credits * 100) : null,
      sub:  evAgg ? `${evAgg.matched}/${evAgg.credits} credits` : 'No data',
    }
    if (id === 'bbps') return {
      rate: bb && bb.overall ? bb.overall.match_rate : null,
      sub:  bb && bb.overall ? `${bb.overall.reconciled}/${bb.overall.bank_rows} ops · ${bb.overall.exceptions} exc` : 'No data',
    }
    if (id === 'kiosk') {
      if (sbi && sbi.total > 0) return { rate: sbi.match_rate, sub: `${sbi.matched}/${sbi.total} matched · ${sbi.exceptions} exc` }
      if (sbi && sbi.has_data) return { rate: null, sub: 'Uploaded — run P01–P04' }
      return { rate: null, sub: 'No data' }
    }
    const slugs = productMap[id] || []
    const agg = slugs.reduce((a, slug) => {
      const s = sumRows(data?.internal?.[slug])
      return { matched: a.matched + s.matched, open: a.open + s.open }
    }, { matched: 0, open: 0 })
    const tot = agg.matched + agg.open
    return {
      rate: tot > 0 ? Math.round(agg.matched / tot * 100) : null,
      sub:  tot > 0 ? `${agg.matched} matched · ${agg.open} open` : 'No data',
    }
  }

  const accessible = filterProductsByAccess(PRODUCTS)
  const hasPins = pinned.size > 0
  const visible = (hasPins && !showAll) ? accessible.filter(p => pinned.has(p.id)) : accessible

  return (
    <div className="card mb-5">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-semibold text-gray-800 text-sm">Product Reconciliations</h3>
        {hasPins && (
          <button onClick={() => setShowAll(s => !s)} className="text-xs text-primary hover:underline">
            {showAll ? 'Show pinned only' : `Show all (${PRODUCTS.length})`}
          </button>
        )}
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        {visible.map(p => {
          const { rate, sub } = statFor(p.id)
          const isPinned = pinned.has(p.id)
          return (
            <div key={p.id} className="relative rounded-xl border border-gray-100 p-4 hover:shadow-md hover:border-primary/30 transition-all">
              <button
                onClick={(e) => { e.preventDefault(); togglePin(p.id) }}
                title={isPinned ? 'Unpin' : 'Pin to dashboard'}
                className={`absolute top-2 right-2 text-sm leading-none ${isPinned ? 'text-amber-400' : 'text-gray-300 hover:text-amber-400'}`}>
                {isPinned ? '★' : '☆'}
              </button>
              <Link to={p.route} className="block">
                <div className="flex items-center gap-2 mb-2 pr-5"><span className="text-lg">{p.icon}</span><span className="font-semibold text-gray-800 text-sm">{p.label}</span></div>
                {rate != null
                  ? <div className={`text-2xl font-bold ${rate >= 90 ? 'text-green-600' : rate >= 50 ? 'text-amber-600' : 'text-red-500'}`}>{rate}%</div>
                  : <div className="text-sm text-gray-400 mt-1">—</div>}
                <div className="text-[11px] text-gray-400 mt-1">{sub}</div>
              </Link>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default function Dashboard() {
  const now = new Date()
  const defaultRange = thisMonth()  // default to current month for relevance

  const [data, setData]             = useState({ internal: {}, bank: {} })
  const [partnerConfigs, setPartnerConfigs] = useState([])
  const [loading, setLoading]       = useState(true)
  const [showClear, setShowClear]   = useState(false)
  const [dateRange, setDateRange]   = useState(defaultRange)
  const [selYear,  setSelYear]      = useState(String(now.getFullYear()))
  const [selMonth, setSelMonth]     = useState(String(now.getMonth() + 1))

  const allPartners = useMemo(() => {
    if (partnerConfigs.length > 0) return partnerConfigs.map(p => p.slug)
    return Array.from(new Set([
      ...Object.keys(data.internal || {}),
      ...Object.keys(data.bank     || {}),
    ]))
  }, [partnerConfigs, data])

  const load = useCallback(async (range = dateRange) => {
    setLoading(true)
    try {
      const params = {}
      if (range.from) params.date_from = range.from
      if (range.to)   params.date_to   = range.to
      const { data: d } = await api.get('/recon/dashboard-summary', { params })
      setData(d)
    } catch { toast.error('Failed to load dashboard') } finally { setLoading(false) }
  }, [dateRange])

  const applyPreset = (preset) => { setDateRange(preset); load(preset) }

  useEffect(() => {
    api.get('/admin/partners-public')
      .then(({ data }) => setPartnerConfigs(data || []))
      .catch(() => {})
    load(defaultRange)
  }, [])

  const activeFilter = dateRange.from || dateRange.to

  return (
    <div>
      {/* ── Header ─────────────────────────────────────────── */}
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-xl font-bold text-gray-800">Dashboard</h1>
          <p className="text-sm text-gray-400">
            {activeFilter
              ? `${dateRange.from || '…'} → ${dateRange.to || '…'}`
              : 'All dates'
            }
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => load(dateRange)}
            className="btn-ghost flex items-center gap-1.5 text-sm">
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            {loading ? 'Loading…' : 'Refresh'}
          </button>
          <button onClick={() => setShowClear(true)}
            className="btn-ghost flex items-center gap-1.5 text-sm text-red-500 hover:text-red-600 hover:border-red-200">
            <Trash2 size={14} /> Clear
          </button>
        </div>
      </div>

      {/* ── Date filter bar ────────────────────────────────── */}
      <div className="card mb-5">
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="text-xs text-gray-400 block mb-1">Year</label>
            <select className="select" value={selYear} onChange={e => setSelYear(e.target.value)}>
              {YEAR_OPTIONS.map(y => <option key={y} value={y}>{y}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">Month</label>
            <select className="select" value={selMonth} onChange={e => setSelMonth(e.target.value)}>
              {MONTHS.map((m, i) => <option key={i+1} value={i+1}>{m}</option>)}
            </select>
          </div>
          <button
            onClick={() => { const r = rangeForYearMonth(selYear, selMonth); setDateRange(r); load(r) }}
            className="btn-primary">Go
          </button>
          <div className="w-px h-8 bg-gray-200 mx-1 self-center hidden sm:block" />
          <div>
            <label className="text-xs text-gray-400 block mb-1">From</label>
            <input type="date" className="input" value={dateRange.from}
              onChange={e => setDateRange(r => ({ ...r, from: e.target.value }))} />
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">To</label>
            <input type="date" className="input" value={dateRange.to}
              onChange={e => setDateRange(r => ({ ...r, to: e.target.value }))} />
          </div>
          <button onClick={() => load(dateRange)} className="btn-ghost">Apply</button>
          <div className="flex gap-2 ml-auto">
            {[
              { label: 'This Month', fn: thisMonth },
              { label: 'Last Month', fn: lastMonth },
              { label: 'All Dates',  fn: () => ({ from: '', to: '' }) },
            ].map(({ label, fn }) => {
              const r = fn()
              const isActive = r.from === dateRange.from && r.to === dateRange.to
              return (
                <button key={label} onClick={() => applyPreset(r)}
                  className={`text-xs px-3 py-1.5 rounded-lg border transition-colors font-medium
                    ${isActive ? 'border-primary text-primary bg-primary/5' : 'border-gray-200 text-gray-500 hover:bg-gray-50'}`}>
                  {label}
                </button>
              )
            })}
          </div>
        </div>
        {activeFilter && (
          <div className="mt-2 flex items-center gap-2 text-xs text-primary">
            <Filter size={11} />
            {dateRange.from || '…'} → {dateRange.to || '…'}
            <button onClick={() => applyPreset({ from: '', to: '' })} className="text-gray-400 hover:text-gray-600 ml-1">✕ Clear</button>
          </div>
        )}
      </div>

      {/* ── Zone 1: KPI strip ────────────────────────────── */}
      <KpiStrip data={data} allPartners={allPartners} />

      {/* ── Funds Position (EOD) — director/CFO view ─────── */}
      <FundsPositionPanel />

      {/* ── Zone 2: Product tabs ─────────────────────────── */}
      <ProductTabs partnerConfigs={partnerConfigs} data={data} />

      {/* ── Zone 3: Dedicated product reconciliations ────── */}
      <DedicatedReconPanel data={data} partnerConfigs={partnerConfigs} />

      {/* ── Zone 4: PG Settlement ────────────────────────── */}
      <div className="mb-5">
        <PGSettlementPanel dateRange={dateRange} />
      </div>

      {/* ── Quick Actions ────────────────────────────────── */}
      <div className="card">
        <h3 className="font-semibold text-gray-800 text-sm mb-3">Quick Actions</h3>
        <div className="flex flex-wrap gap-3">
          {[
            { to: '/upload',     label: 'Upload Files',       style: 'btn-primary' },
            { to: '/run-recon',  label: 'Run Reconciliation', style: 'btn-primary' },
            { to: '/open-items', label: 'View Open Items',    style: 'btn-ghost'   },
            { to: '/reports',    label: 'Download Reports',   style: 'btn-ghost'   },
          ].map(({ to, label, style }) => (
            <Link key={to} to={to} className={`${style} flex items-center gap-1.5 text-sm`}>
              {label} <ArrowRight size={13} />
            </Link>
          ))}
        </div>
      </div>

      {showClear && (
        <ClearModal
          onClose={() => setShowClear(false)}
          onDone={() => load()}
          partnerSlugs={allPartners}
        />
      )}
    </div>
  )
}
