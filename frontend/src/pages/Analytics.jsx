// Executive Analytics — CEO/management view of daily reconciliation activity
// across every product. Date-filterable; charts switch type on demand (bar / line
// / pie / donut). Read-only; data from GET /reports/analytics (core/analytics.py).
import React, { useState, useEffect, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import api from '../utils/api'
import toast from 'react-hot-toast'
import { BarChart3, CheckCircle2, XCircle, Percent, Wallet, RefreshCw, Link2, Maximize2, Scale, LogOut, AlertTriangle, Clock } from 'lucide-react'
import { BarChart, LineChart, PieChart, ChartCard, PALETTE } from '../components/Charts'
import { isViewer } from '../utils/permissions'

const C = { matched: '#10b981', unmatched: '#ef4444', mismatch: '#f59e0b', other: '#94a3b8' }
// Bank vs Internal share the matched/open meaning (green/red) but differ by SHADE
// so two same-meaning slices in one pie are never the same colour: Bank = full
// strength, Internal = lighter tint of the same hue.
const SIDE_C = {
  bank: { matched: '#059669', unmatched: '#dc2626' },      // emerald-600 / red-600
  internal: { matched: '#6ee7b7', unmatched: '#fca5a5' },  // emerald-300 / red-300
}
const inr = n => `₹${Number(n || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`
const cr = n => {
  n = Number(n || 0)
  if (Math.abs(n) >= 1e7) return `₹${(n / 1e7).toFixed(2)} Cr`
  if (Math.abs(n) >= 1e5) return `₹${(n / 1e5).toFixed(2)} L`
  return inr(n)
}
const monthStart = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-01` }
const today = () => new Date().toISOString().split('T')[0]
// Build an app URL under the deploy base path (/recon/ in prod, / in dev).
const withBase = p => (import.meta.env.BASE_URL + p).replace(/\/{2,}/g, '/')

// `standalone` renders a chrome-free, full-screen view (the /exec route) — same
// data, no app sidebar — for sharing with management or putting on a display.
// "Other" cell — a clickable count that opens a breakdown of WHAT those non-reconcilable rows
// are (Failed / Duplicate / Fee / Reversal …), each with its count and share, so "42 other"
// becomes legible ("30 Failed · 12 Duplicate").
function OtherCell({ count, statuses }) {
  const [open, setOpen] = useState(false)
  const ref = React.useRef()
  useEffect(() => {
    const h = e => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [])
  const n = Number(count || 0)
  if (!n) return <span className="text-gray-300">0</span>
  const entries = Object.entries(statuses || {}).sort((a, b) => Number(b[1]) - Number(a[1]))
  const total = entries.reduce((s, [, v]) => s + Number(v), 0) || n
  return (
    <span className="relative inline-block" ref={ref}>
      <button onClick={() => setOpen(o => !o)} disabled={!entries.length}
        className="tabular-nums text-gray-500 hover:text-primary underline decoration-dotted decoration-gray-300 underline-offset-2 cursor-pointer disabled:no-underline disabled:cursor-default">
        {n.toLocaleString('en-IN')}
      </button>
      {open && entries.length > 0 && (
        <div className="absolute right-0 z-40 mt-1 w-60 rounded-lg border border-gray-200 bg-white shadow-xl p-3 text-left font-normal">
          <div className="text-[10px] uppercase tracking-wide text-gray-400 mb-2">No reconciliation needed — {n.toLocaleString('en-IN')} rows</div>
          {entries.map(([k, v]) => {
            const pct = Math.round(Number(v) / total * 100)
            return (
              <div key={k} className="mb-2 last:mb-0">
                <div className="flex justify-between text-[11px] text-gray-600 mb-0.5"><span>{k}</span><span className="tabular-nums text-gray-500">{Number(v).toLocaleString('en-IN')} · {pct}%</span></div>
                <div className="h-1.5 rounded-full bg-gray-100"><div className="h-1.5 rounded-full bg-gray-400" style={{ width: pct + '%' }} /></div>
              </div>
            )
          })}
        </div>
      )}
    </span>
  )
}

export default function Analytics({ standalone = false }) {
  // Filters live in the URL query string so any view is shareable / bookmarkable
  // exactly as filtered (from / to / product / side).
  const [searchParams, setSearchParams] = useSearchParams()
  const [from, setFrom] = useState(searchParams.has('from') ? searchParams.get('from') : monthStart())
  const [to, setTo] = useState(searchParams.has('to') ? searchParams.get('to') : today())
  const [product, setProduct] = useState(searchParams.get('product') || '')
  const [side, setSide] = useState(searchParams.get('side') || '')
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const { data: res } = await api.get('/reports/analytics', {
        params: { date_from: from || undefined, date_to: to || undefined, product: product || undefined, side: side || undefined },
      })
      setData(res)
    } catch { toast.error('Could not load analytics') } finally { setLoading(false) }
  }, [from, to, product, side])
  useEffect(() => { load() }, [load])

  // Mirror the filters into the URL (replace, so we don't flood history). from/to
  // are always written (even empty = "All dates") so a shared link reproduces the
  // exact view; product/side only when set to keep the URL tidy.
  useEffect(() => {
    const next = { from, to }
    if (product) next.product = product
    if (side) next.side = side
    setSearchParams(next, { replace: true })
  }, [from, to, product, side, setSearchParams])

  const copyLink = async () => {
    try { await navigator.clipboard.writeText(window.location.href); toast.success('Dashboard link copied') }
    catch { toast.error('Copy failed — copy the URL from the address bar') }
  }
  const logout = () => {
    // Viewers (incl. @eko.co.in email-code sessions) return to the /exec gate;
    // operators go to the normal login. Hard redirect = clean session reset.
    const dest = isViewer() ? 'exec' : 'login'
    localStorage.removeItem('token'); localStorage.removeItem('user')
    window.location.href = withBase(dest)
  }

  const preset = (f, t) => { setFrom(f); setTo(t) }
  const t = data?.totals || {}
  const daily = data?.daily || []
  const byProduct = data?.by_product || []
  const byGroup = data?.by_group || []
  const kioskProcs = data?.kiosk_processes || []
  const byStatus = data?.by_status || []
  const bySide = data?.by_side || []

  // chart inputs
  const dailyCats = daily.map(d => d.date.slice(5))
  const dailySeries = [
    { name: 'Matched', color: C.matched, values: daily.map(d => d.matched) },
    { name: 'Unmatched', color: C.unmatched, values: daily.map(d => d.unmatched) },
  ]
  const rateSeries = [{ name: 'Match rate %', color: PALETTE[3], values: daily.map(d => d.match_rate) }]
  const statusSlices = byStatus.map(s => ({ label: s.label, value: s.count, color: C[s.status] || '#94a3b8' }))
  // Chart shows PRODUCT groups (DMT rolled up from Axis/Fino/Levin/Airtel).
  const prodCats = byGroup.map(g => g.label)
  const prodSeries = [
    { name: 'Matched', color: C.matched, values: byGroup.map(g => g.matched) },
    { name: 'Unmatched', color: C.unmatched, values: byGroup.map(g => g.unmatched) },
  ]
  const sideCats = bySide.map(s => s.side === 'bank' ? 'Bank side' : 'Internal side')
  const sideSeries = [
    { name: 'Matched', color: C.matched, values: bySide.map(s => s.matched) },
    { name: 'Unmatched', color: C.unmatched, values: bySide.map(s => s.unmatched) },
  ]
  // ── Money (₹) view: matched vs still-open rupee volume per product ──
  const prodVolSeries = [
    { name: 'Matched ₹', color: C.matched, values: byGroup.map(g => g.matched_volume) },
    { name: 'Open ₹', color: C.unmatched, values: byGroup.map(g => g.open_volume) },
  ]
  // ── Needs-attention: products below 85% match, worst first ──
  const attention = byGroup
    .filter(g => g.transactions > 0 && g.match_rate < 85)
    .sort((a, b) => a.match_rate - b.match_rate)
  // ── Open-items ageing ──
  const openAgeing = data?.open_ageing || { buckets: [], total_count: 0, total_value: 0 }

  const Kpi = ({ icon: Icon, label, value, sub, tint }) => (
    <div className="card p-4">
      <div className="flex items-center justify-between mb-1.5">
        <span className={`w-8 h-8 rounded-lg grid place-items-center ${tint}`}><Icon size={16} /></span>
      </div>
      <div className="text-2xl font-extrabold text-gray-800 tabular-nums">{value}</div>
      <div className="text-xs text-gray-500 font-medium">{label}</div>
      {sub != null && <div className="text-[11px] text-gray-400 mt-0.5">{sub}</div>}
    </div>
  )

  return (
    <div className="space-y-5">
      {standalone && (
        <div className="flex items-center gap-2.5 pb-3 border-b border-gray-100">
          <div className="w-8 h-8 rounded-lg bg-emerald-600/10 ring-1 ring-emerald-600/20 grid place-items-center flex-shrink-0">
            <Scale size={16} className="text-emerald-600" />
          </div>
          <div className="leading-tight">
            <div className="font-bold text-gray-800 text-sm">Eko Bharat Ventures</div>
            <div className="text-[10px] uppercase tracking-widest text-gray-400">Executive Dashboard</div>
          </div>
          <div className="ml-auto flex items-center gap-1.5">
            {!isViewer() && <a href={withBase('analytics') + window.location.search} className="btn-ghost text-xs">Open full app ↗</a>}
            <button onClick={logout} className="btn-ghost text-xs flex items-center gap-1"><LogOut size={13} /> Logout</button>
          </div>
        </div>
      )}
      <div className="flex items-center gap-3 flex-wrap">
        <div>
          <h1 className="text-xl font-bold text-gray-800 flex items-center gap-2"><BarChart3 size={20} className="text-emerald-600" /> Reconciliation Analytics</h1>
          <p className="text-sm text-gray-500">Daily reconciliation activity across all products {data && <>· {data.date_from || '…'} → {data.date_to || '…'}</>}</p>
        </div>
        <div className="ml-auto flex items-center gap-1.5">
          <button onClick={copyLink} className="btn-ghost text-xs flex items-center gap-1" title="Copy a link to this exact filtered view"><Link2 size={13} /> Copy link</button>
          {!standalone && (
            <a href={withBase('exec') + window.location.search} target="_blank" rel="noreferrer" className="btn-ghost text-xs flex items-center gap-1" title="Open a full-screen, sidebar-free view (for the CEO / a display)"><Maximize2 size={13} /> Full screen</a>
          )}
          <button onClick={load} className="btn-ghost text-xs flex items-center gap-1"><RefreshCw size={13} className={loading ? 'animate-spin' : ''} /> Refresh</button>
        </div>
      </div>

      {/* Filters */}
      <div className="card p-4 flex items-end gap-3 flex-wrap">
        <div><div className="text-[11px] text-gray-500 mb-0.5">From</div><input type="date" className="input" value={from} onChange={e => setFrom(e.target.value)} /></div>
        <div><div className="text-[11px] text-gray-500 mb-0.5">To</div><input type="date" className="input" value={to} onChange={e => setTo(e.target.value)} /></div>
        <div>
          <div className="text-[11px] text-gray-500 mb-0.5">Product</div>
          <select className="input" value={product} onChange={e => setProduct(e.target.value)}>
            <option value="">All products</option>
            {(data?.products || []).map(p => (
              <option key={p.product} value={p.product}>
                {p.group_label && p.group_label !== p.label ? `${p.group_label} · ${p.label}` : p.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <div className="text-[11px] text-gray-500 mb-0.5">Side</div>
          <select className="input" value={side} onChange={e => setSide(e.target.value)}>
            <option value="">Both sides</option><option value="bank">Bank</option><option value="internal">Internal</option>
          </select>
        </div>
        <div className="flex items-center gap-1.5 ml-auto">
          <button onClick={() => preset(monthStart(), today())} className="btn-ghost text-xs">This month</button>
          <button onClick={() => { const d = new Date(); const f = new Date(d.getFullYear(), d.getMonth() - 1, 1); const l = new Date(d.getFullYear(), d.getMonth(), 0); preset(f.toISOString().split('T')[0], l.toISOString().split('T')[0]) }} className="btn-ghost text-xs">Last month</button>
          <button onClick={() => preset('', '')} className="btn-ghost text-xs">All dates</button>
        </div>
      </div>

      {/* KPI cards */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <Kpi icon={BarChart3} tint="bg-slate-100 text-slate-600" label="Total transactions" value={Number(t.transactions || 0).toLocaleString('en-IN')} sub={`${daily.length} day(s)`} />
        <Kpi icon={CheckCircle2} tint="bg-emerald-100 text-emerald-600" label="Matched" value={Number(t.matched || 0).toLocaleString('en-IN')} sub={cr(t.matched_volume)} />
        <Kpi icon={XCircle} tint="bg-red-100 text-red-500" label="Unmatched (open)" value={Number(t.unmatched || 0).toLocaleString('en-IN')} sub={cr(t.open_volume)} />
        <Kpi icon={Percent} tint="bg-indigo-100 text-indigo-600" label="Match rate" value={`${t.match_rate || 0}%`} sub={t.mismatch ? `${t.mismatch} amount-mismatch` : 'both sides'} />
        <Kpi icon={Wallet} tint="bg-amber-100 text-amber-600" label="Matched volume" value={cr(t.matched_volume)} sub={`open ${cr(t.open_volume)}`} />
      </div>

      {/* Needs attention — products below 85% match, with the open ₹ at risk */}
      {attention.length > 0 && (
        <div className="card p-4 border-l-4 border-amber-400">
          <div className="flex items-center gap-2 mb-2.5">
            <AlertTriangle size={15} className="text-amber-500" />
            <h3 className="font-bold text-gray-800 text-sm">Needs attention</h3>
            <span className="text-xs text-gray-400">— products below 85% match rate</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {attention.map(g => (
              <div key={g.group} className="rounded-lg border border-gray-100 bg-gray-50/60 px-3 py-2 min-w-[160px]">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-sm font-semibold text-gray-700">{g.label}</span>
                  <span className="text-sm font-bold tabular-nums" style={{ color: g.match_rate >= 50 ? '#d97706' : '#dc2626' }}>{g.match_rate}%</span>
                </div>
                <div className="text-[11px] text-gray-500 mt-0.5">
                  {Number(g.unmatched).toLocaleString('en-IN')} open · <span className="text-red-500/90 font-medium">{cr(g.open_volume)}</span> at risk
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {loading && !data ? <div className="card p-10 text-center text-gray-400 text-sm">Loading…</div> : (
        <>
          {/* Daily trend + status */}
          <div className="grid lg:grid-cols-2 gap-4">
            <ChartCard title="Daily reconciliation" subtitle="Matched vs unmatched per day"
              types={['line', 'stacked', 'bar']} defaultType="line"
              legend={[{ name: 'Matched', color: C.matched }, { name: 'Unmatched', color: C.unmatched }]}>
              {type => daily.length === 0 ? <Empty />
                : type === 'line' ? <LineChart categories={dailyCats} series={dailySeries} />
                  : <BarChart categories={dailyCats} series={dailySeries} stacked={type === 'stacked'} />}
            </ChartCard>

            <ChartCard title="Status distribution" subtitle="Where every transaction landed"
              types={['donut', 'pie', 'bar']} defaultType="donut">
              {type => statusSlices.length === 0 ? <Empty />
                : type === 'bar'
                  ? <BarChart categories={byStatus.map(s => s.label)}
                      series={[{ name: 'Count', colors: byStatus.map(s => C[s.status] || '#94a3b8'), values: byStatus.map(s => s.count) }]} />
                  : <PieChart slices={statusSlices} donut={type === 'donut'} />}
            </ChartCard>
          </div>

          {/* Per-product + bank vs internal */}
          <div className="grid lg:grid-cols-2 gap-4">
            <ChartCard title="By product" subtitle="Each product's matched vs unmatched share — toggle for raw volume"
              types={['share', 'barh', 'stacked']} defaultType="share"
              legend={[{ name: 'Matched', color: C.matched }, { name: 'Unmatched', color: C.unmatched }]}>
              {type => byGroup.length === 0 ? <Empty />
                : <BarChart categories={prodCats} series={prodSeries} horizontal
                    stacked={type === 'stacked'} normalize={type === 'share'}
                    height={Math.max(180, prodCats.length * 34 + 28)} />}
            </ChartCard>

            <ChartCard title="Both sides — bank vs internal" subtitle="How many records matched on each side · darker = Bank, lighter = Internal"
              types={['bar', 'pie']} defaultType="bar"
              legend={[
                { name: 'Bank matched', color: SIDE_C.bank.matched }, { name: 'Bank open', color: SIDE_C.bank.unmatched },
                { name: 'Internal matched', color: SIDE_C.internal.matched }, { name: 'Internal open', color: SIDE_C.internal.unmatched }]}>
              {type => bySide.length === 0 ? <Empty />
                : type === 'pie'
                  ? <PieChart slices={bySide.flatMap(s => {
                      const c = SIDE_C[s.side] || SIDE_C.bank
                      const name = s.side === 'bank' ? 'Bank' : 'Internal'
                      return [
                        { label: `${name} · matched`, value: s.matched, color: c.matched },
                        { label: `${name} · open`, value: s.unmatched, color: c.unmatched }]
                    })} donut />
                  : <BarChart categories={sideCats} series={sideSeries} />}
            </ChartCard>
          </div>

          {/* Money (₹) at stake + open-items ageing */}
          <div className="grid lg:grid-cols-2 gap-4">
            <ChartCard title="Value at stake (₹) by product" subtitle="Matched vs still-open rupee volume per product"
              types={['stacked', 'share']} defaultType="stacked"
              legend={[{ name: 'Matched ₹', color: C.matched }, { name: 'Open ₹', color: C.unmatched }]}>
              {type => byGroup.length === 0 ? <Empty />
                : <BarChart categories={prodCats} series={prodVolSeries} horizontal
                    stacked={type === 'stacked'} normalize={type === 'share'} valueFmt={cr}
                    height={Math.max(180, prodCats.length * 34 + 28)} />}
            </ChartCard>

            <div className="card p-0 overflow-hidden">
              <div className="px-4 py-3 border-b border-gray-100 bg-gray-50/50 flex items-center gap-2">
                <Clock size={15} className="text-gray-400" />
                <div>
                  <h3 className="font-bold text-gray-800 text-sm">Open items ageing</h3>
                  <p className="text-[11px] text-gray-400">
                    How long unmatched items have been open · {Number(openAgeing.total_count).toLocaleString('en-IN')} open · {cr(openAgeing.total_value)}
                  </p>
                </div>
              </div>
              <div className="p-4 space-y-3">
                {openAgeing.buckets.map(b => {
                  const pct = openAgeing.total_value ? Math.round(b.value / openAgeing.total_value * 100) : 0
                  const isOld = b.bucket === 'd7'
                  const barC = isOld ? '#dc2626' : b.bucket === 'd3' ? '#f59e0b' : b.bucket === 'd1' ? '#84cc16' : '#10b981'
                  return (
                    <div key={b.bucket}>
                      <div className="flex justify-between text-xs mb-1">
                        <span className={isOld ? 'font-semibold text-red-600' : 'text-gray-600'}>{b.label}</span>
                        <span className="tabular-nums text-gray-500">{Number(b.count).toLocaleString('en-IN')} · {cr(b.value)}</span>
                      </div>
                      <div className="h-2 rounded-full bg-gray-100"><div className="h-2 rounded-full" style={{ width: pct + '%', background: barC }} /></div>
                    </div>
                  )
                })}
                {openAgeing.total_count === 0 && <div className="text-center text-gray-400 text-sm py-8">No open items 🎉</div>}
                {openAgeing.total_count > 0 && (
                  <p className="text-[11px] text-gray-400 pt-1">7+ day items are the priority to chase — that money has been open a full week.</p>
                )}
              </div>
            </div>
          </div>

          {/* Per-product table — grouped by product, banks shown within */}
          <div className="card p-0 overflow-hidden">
            <div className="px-4 py-3 border-b border-gray-100 bg-gray-50/50"><h3 className="font-bold text-gray-800 text-sm">Product breakdown</h3></div>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead><tr className="text-gray-400 border-b border-gray-100 bg-gray-50/40">
                  <th className="text-left py-2 px-4">Product</th>
                  <th className="text-left py-2 px-3">Bank / partner</th>
                  <th className="text-right py-2 px-3">Transactions</th>
                  <th className="text-right py-2 px-3">Matched</th>
                  <th className="text-right py-2 px-3">Unmatched</th>
                  <th className="text-right py-2 px-3">Mismatch</th>
                  <th className="text-right py-2 px-3" title="Rows that need no reconciliation — failed, duplicate, fees, reversals, bank debits. Hover a value for the reason breakdown. Excluded from the match rate.">Other</th>
                  <th className="text-right py-2 px-3">Match rate</th>
                  <th className="text-right py-2 px-3 text-emerald-600/70">Matched volume</th>
                  <th className="text-right py-2 px-4 text-red-400/80">Unmatched volume</th>
                </tr></thead>
                <tbody>
                  {byGroup.map(g => {
                    const banks = byProduct.filter(p => p.group === g.group)
                    // SBI Kiosk expands into its 4 process recons (P01–P04); DMT into its banks.
                    const isKiosk = g.group === 'kiosk'
                    const subRows = isKiosk
                      ? kioskProcs.map(p => ({ ...p, label: p.process }))
                      : (banks.length > 1 ? banks : [])
                    const subLabel = isKiosk ? '4 processes' : (banks.length > 1 ? `${banks.length} banks` : '')
                    const rateColor = r => (r >= 85 ? '#059669' : r >= 50 ? '#d97706' : '#dc2626')
                    return (
                      <React.Fragment key={g.group}>
                        {/* product-level row (bold) */}
                        <tr className="border-b border-gray-100 bg-gray-50/40">
                          <td className="py-2 px-4 font-bold text-gray-800">{g.label}</td>
                          <td className="py-2 px-3 text-gray-400">{subLabel}</td>
                          <td className="py-2 px-3 text-right tabular-nums font-semibold text-gray-700">{g.transactions.toLocaleString('en-IN')}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-emerald-600 font-bold">{g.matched.toLocaleString('en-IN')}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-red-500 font-semibold">{g.unmatched.toLocaleString('en-IN')}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-amber-500">{g.mismatch.toLocaleString('en-IN')}</td>
                          <td className="py-2 px-3 text-right"><OtherCell count={g.other} statuses={g.other_statuses} /></td>
                          <td className="py-2 px-3 text-right tabular-nums font-bold" style={{ color: rateColor(g.match_rate) }}>{g.match_rate}%</td>
                          <td className="py-2 px-3 text-right tabular-nums font-semibold text-emerald-700">{cr(g.matched_volume)}</td>
                          <td className="py-2 px-4 text-right tabular-nums font-semibold text-red-500/90">{cr(g.open_volume)}</td>
                        </tr>
                        {/* sub-rows: DMT banks OR the 4 SBI Kiosk process recons */}
                        {subRows.map((p, i) => (
                          <tr key={(p.product || p.process || i)} className="border-b border-gray-50 hover:bg-emerald-50/40">
                            <td className="py-1.5 px-4"></td>
                            <td className="py-1.5 px-3 text-gray-600 pl-6">↳ {p.label}</td>
                            <td className="py-1.5 px-3 text-right tabular-nums text-gray-500">{Number(p.transactions).toLocaleString('en-IN')}</td>
                            <td className="py-1.5 px-3 text-right tabular-nums text-emerald-600">{Number(p.matched).toLocaleString('en-IN')}</td>
                            <td className="py-1.5 px-3 text-right tabular-nums text-red-400">{Number(p.unmatched).toLocaleString('en-IN')}</td>
                            <td className="py-1.5 px-3 text-right tabular-nums text-amber-400">{Number(p.mismatch).toLocaleString('en-IN')}</td>
                            <td className="py-1.5 px-3 text-right"><OtherCell count={p.other} statuses={p.other_statuses} /></td>
                            <td className="py-1.5 px-3 text-right tabular-nums" style={{ color: rateColor(p.match_rate) }}>{p.match_rate}%</td>
                            <td className="py-1.5 px-3 text-right tabular-nums text-emerald-700/80">{cr(p.matched_volume)}</td>
                            <td className="py-1.5 px-4 text-right tabular-nums text-red-500/80">{cr(p.open_volume)}</td>
                          </tr>
                        ))}
                      </React.Fragment>
                    )
                  })}
                  {byGroup.length === 0 && <tr><td colSpan="10" className="py-6 text-center text-gray-400 italic">No data for this range.</td></tr>}
                </tbody>
              </table>
            </div>
          </div>
          <p className="text-[11px] text-gray-400 px-1">Read-only view. "Both sides" compares bank vs internal records for ledger products (core + E-Value); SBI Kiosk and other module products appear in totals, status and per-product breakdowns.</p>
        </>
      )}
    </div>
  )
}

function Empty() { return <div className="h-48 grid place-items-center text-gray-400 text-sm italic">No data for this range.</div> }
