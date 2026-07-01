import React, { useState, useEffect } from 'react'
import { Search, GitMerge, X, CheckCircle, Plus, Trash2, ArrowRight } from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'
import { isCoreLedgerPartner } from '../productRegistry'

// Amount gap between the two sides of a pending pair. Beyond ₹1 the backend flags the
// match `amount_mismatch` (parity with the auto-matcher), so we warn before submit.
const amtDiff = (a, b) => Math.abs((Number(a?.amount) || 0) - (Number(b?.amount) || 0))
const fmtRs   = (n) => `₹${(Number(n) || 0).toLocaleString('en-IN')}`

export default function ManualMatch() {
  const [filters, setFilters]         = useState({ partner: '', date_from: '', date_to: '', eko_tid: '' })
  const [partnerList, setPartnerList] = useState([])
  const [bankItems, setBankItems]     = useState([])
  const [internalItems, setInternalItems] = useState([])
  const [loading, setLoading]         = useState(false)
  const [submitting, setSubmitting]   = useState(false)

  // Queue of pending pairs — each entry: { bankItem, internalItem }
  const [queue, setQueue]             = useState([])
  // Currently selected items (for building next pair)
  const [pendingBank, setPendingBank] = useState(null)
  const [pendingInternal, setPendingInternal] = useState(null)

  useEffect(() => {
    api.get('/admin/partners-public')
      .then(({ data }) => { const core = (data || []).filter(isCoreLedgerPartner); if (core.length) setPartnerList(core) })
      .catch(() => {})
  }, [])

  const loadItems = async () => {
    setLoading(true)
    try {
      const params = {
        partner: filters.partner || undefined,
        page_size: 200,
        ...(filters.date_from ? { date_from: filters.date_from } : {}),
        ...(filters.date_to   ? { date_to:   filters.date_to   } : {}),
        ...(filters.eko_tid   ? { eko_tid:   filters.eko_tid   } : {}),
      }
      const [bankRes, intRes] = await Promise.all([
        api.get('/recon/open-items', { params: { ...params, side: 'bank' } }),
        api.get('/recon/open-items', { params: { ...params, side: 'internal' } })
      ])
      setBankItems(bankRes.data.items ?? [])
      setInternalItems(intRes.data.items ?? [])
      setPendingBank(null); setPendingInternal(null)
      setQueue([])
    } catch { toast.error('Load failed') }
    finally { setLoading(false) }
  }

  // When both sides selected, add to queue
  const handleSelectBank     = (item) => {
    const isInQueue = queue.some(p => p.bankItem.id === item.id)
    if (isInQueue) return toast.error('Already in queue')
    setPendingBank(prev => prev?.id === item.id ? null : item)
  }
  const handleSelectInternal = (item) => {
    const isInQueue = queue.some(p => p.internalItem.id === item.id)
    if (isInQueue) return toast.error('Already in queue')
    setPendingInternal(prev => prev?.id === item.id ? null : item)
  }

  const addToQueue = () => {
    if (!pendingBank || !pendingInternal) return toast.error('Select one from each side first')
    setQueue(q => [...q, { bankItem: pendingBank, internalItem: pendingInternal }])
    setPendingBank(null)
    setPendingInternal(null)
  }

  const removeFromQueue = (idx) => setQueue(q => q.filter((_, i) => i !== idx))

  const submitAll = async () => {
    if (queue.length === 0) return toast.error('Queue is empty')
    setSubmitting(true)
    try {
      const pairs = queue.map(p => ({ bank_txn_id: p.bankItem.id, internal_txn_id: p.internalItem.id }))
      const { data } = await api.post('/recon/manual-match-bulk', { pairs })
      const mism = data.mismatch || 0
      const msg = `Matched ${data.matched} pair(s)`
        + (mism > 0 ? ` — ${mism} flagged as amount mismatch` : '')
        + (data.errors > 0 ? `, ${data.errors} failed` : '')
      if (mism > 0) toast(msg, { icon: '⚠️', duration: 5000 })
      else toast.success(msg)
      // Remove successfully matched items from the panels
      const matchedBankIds     = data.results.filter(r => r.status === 'ok').map(r => r.bank_txn_id)
      const matchedInternalIds = data.results.filter(r => r.status === 'ok').map(r => r.internal_txn_id)
      setBankItems(prev => prev.filter(i => !matchedBankIds.includes(i.id)))
      setInternalItems(prev => prev.filter(i => !matchedInternalIds.includes(i.id)))
      setQueue([])
    } catch (err) { toast.error(err.response?.data?.detail || 'Submit failed') }
    finally { setSubmitting(false) }
  }

  const queuedBankIds     = new Set(queue.map(p => p.bankItem.id))
  const queuedInternalIds = new Set(queue.map(p => p.internalItem.id))

  const TxnRow = ({ item, isSelected, isQueued, onSelect, color, showDesc = false, showCsp = false }) => (
    <tr
      onClick={() => !isQueued && onSelect(item)}
      className={`border-b border-gray-50 transition-colors text-xs
        ${isQueued    ? 'bg-gray-100 opacity-50 cursor-not-allowed' : 'cursor-pointer'}
        ${isSelected  ? `bg-${color}-100 ring-1 ring-inset ring-${color}-300` : `hover:bg-${color}-50`}`}
    >
      <td className="px-2 py-2 w-5">
        <div className={`w-4 h-4 rounded-full border-2 flex items-center justify-center
          ${isSelected ? `border-${color}-500 bg-${color}-500` : 'border-gray-300'}`}>
          {isSelected && <div className="w-1.5 h-1.5 bg-white rounded-full" />}
          {isQueued   && <div className="w-2 h-2 bg-gray-400 rounded-full" />}
        </div>
      </td>
      <td className="px-2 py-2 font-mono text-gray-700">{item.eko_tid || '—'}</td>
      <td className="px-2 py-2 font-mono text-gray-500">{item.tracking_number || '—'}</td>
      <td className="px-2 py-2 font-mono text-gray-400">{item.utr_number || '—'}</td>
      <td className="px-2 py-2 text-right tabular-nums font-medium">{item.amount?.toLocaleString('en-IN') || '—'}</td>
      <td className="px-2 py-2 font-mono text-gray-400 text-xs">{item.recon_date || '—'}</td>
      {showDesc && (
        <td className="px-2 py-2 text-gray-500 max-w-[14rem]">
          <span className="block truncate" title={item.bank_description || ''}>{item.bank_description || '—'}</span>
        </td>
      )}
      {showCsp && (
        <td className="px-2 py-2 text-gray-500 max-w-[12rem]">
          {(item.csp_code || item.csp_name)
            ? <span className="block leading-tight">
                <span className="block font-mono text-gray-600 truncate" title={item.csp_code || ''}>{item.csp_code || '—'}</span>
                {item.csp_name && <span className="block text-[10px] text-gray-400 truncate" title={item.csp_name}>{item.csp_name}</span>}
              </span>
            : '—'}
        </td>
      )}
    </tr>
  )

  return (
    <div>
      <h1 className="text-xl font-bold text-gray-800 mb-1">Manual Match</h1>
      <p className="text-sm text-gray-500 mb-5">Select pairs and queue them — submit all at once</p>

      {/* Filters */}
      <div className="card mb-5">
        <div className="grid grid-cols-4 gap-3">
          <div>
            <label className="text-xs text-gray-400 block mb-1">Partner</label>
            <select className="select" value={filters.partner}
              onChange={e => setFilters({ ...filters, partner: e.target.value })}>
              <option value="">Select partner…</option>
              {partnerList.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">From Date</label>
            <input type="date" className="input" value={filters.date_from}
              onChange={e => setFilters({ ...filters, date_from: e.target.value })} />
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">To Date</label>
            <input type="date" className="input" value={filters.date_to}
              onChange={e => setFilters({ ...filters, date_to: e.target.value })} />
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">Eko TID</label>
            <input className="input" placeholder="Partial TID…" value={filters.eko_tid}
              onChange={e => setFilters({ ...filters, eko_tid: e.target.value })} />
          </div>
        </div>
        <button onClick={loadItems} disabled={loading}
          className="btn-primary mt-3 flex items-center gap-2">
          <Search size={14} /> {loading ? 'Loading…' : 'Load Open Items'}
        </button>
      </div>

      {/* Pending pair bar */}
      {(pendingBank || pendingInternal) && (() => {
        const diff = (pendingBank && pendingInternal) ? amtDiff(pendingBank, pendingInternal) : 0
        const mism = diff > 1
        return (
        <div className={`${mism ? 'bg-amber-500' : 'bg-primary'} text-white rounded-xl px-4 py-3 mb-4 flex items-center justify-between gap-3 flex-wrap`}>
          <div className="flex items-center gap-3 text-sm flex-wrap">
            <span>Bank: <span className="font-semibold">{pendingBank?.eko_tid || pendingBank?.tracking_number || '—'}</span>{pendingBank && <span className="opacity-80"> · {fmtRs(pendingBank.amount)}</span>}</span>
            <GitMerge size={16} />
            <span>Internal: <span className="font-semibold">{pendingInternal?.eko_tid || pendingInternal?.tracking_number || '—'}</span>{pendingInternal && <span className="opacity-80"> · {fmtRs(pendingInternal.amount)}</span>}</span>
            {mism && <span className="bg-white/25 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap">⚠ differ by {fmtRs(diff)} → will flag as Amount Mismatch</span>}
          </div>
          <div className="flex gap-2">
            <button onClick={() => { setPendingBank(null); setPendingInternal(null) }}
              className="text-white/70 hover:text-white text-xs flex items-center gap-1">
              <X size={14} /> Clear
            </button>
            <button onClick={addToQueue}
              disabled={!pendingBank || !pendingInternal}
              className="bg-white text-gray-800 text-xs px-3 py-1 rounded-lg font-medium hover:bg-white/90 disabled:opacity-50 flex items-center gap-1">
              <Plus size={14} /> Add to Queue
            </button>
          </div>
        </div>
      )})()}

      {/* Match queue */}
      {queue.length > 0 && (
        <div className="card mb-5">
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-semibold text-gray-700 text-sm flex items-center gap-2">
              <GitMerge size={15} className="text-primary" />
              Match Queue — {queue.length} pair(s)
            </h3>
            <button onClick={submitAll} disabled={submitting}
              className="btn-primary flex items-center gap-2 text-sm">
              <CheckCircle size={14} /> {submitting ? 'Submitting…' : `Submit All ${queue.length} Pairs`}
            </button>
          </div>
          <div className="space-y-1.5">
            {queue.map((pair, idx) => {
              const diff = amtDiff(pair.bankItem, pair.internalItem)
              const mism = diff > 1
              return (
              <div key={idx} className={`flex items-center gap-3 rounded-lg px-3 py-2 text-xs ${mism ? 'bg-amber-50 border border-amber-200' : 'bg-primary/5'}`}>
                <span className="text-gray-500 w-4">{idx + 1}.</span>
                <span className="font-mono text-blue-700 flex-1">
                  {pair.bankItem.eko_tid || pair.bankItem.tracking_number || pair.bankItem.id.slice(0,8)}
                  {' · '}{fmtRs(pair.bankItem.amount)}
                </span>
                <ArrowRight size={12} className="text-gray-400 shrink-0" />
                <span className="font-mono text-green-700 flex-1">
                  {pair.internalItem.eko_tid || pair.internalItem.tracking_number || pair.internalItem.id.slice(0,8)}
                  {' · '}{fmtRs(pair.internalItem.amount)}
                </span>
                {mism && <span title="Amounts differ — this pair will be recorded as Amount Mismatch (needs review), not a clean match"
                  className="text-amber-700 bg-amber-100 rounded-full px-2 py-0.5 font-medium whitespace-nowrap">⚠ Δ {fmtRs(diff)}</span>}
                <button onClick={() => removeFromQueue(idx)}
                  className="text-gray-400 hover:text-red-500 ml-2">
                  <Trash2 size={12} />
                </button>
              </div>
            )})}
          </div>
          <p className="text-[11px] text-gray-400 mt-2">Pairs marked <span className="text-amber-700 font-medium">⚠ Δ</span> have different amounts — they’re still linked but recorded as <span className="font-medium">Amount Mismatch</span> (needs review), not a clean match.</p>
        </div>
      )}

      {/* Two panels */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        {/* Bank Side */}
        <div className="card p-0 overflow-hidden">
          <div className="px-4 py-3 bg-blue-50 border-b border-blue-100 flex items-center justify-between">
            <h3 className="font-semibold text-blue-800 text-sm">Bank Statement ({bankItems.length})</h3>
            {pendingBank && <span className="text-xs text-blue-600 font-medium">1 selected</span>}
          </div>
          <div className="overflow-auto max-h-96">
            <table className="w-full">
              <thead className="bg-gray-50 sticky top-0">
                <tr className="text-xs">
                  <th className="px-2 py-2 w-5"></th>
                  <th className="px-2 py-2 text-left text-gray-500">Eko TID</th>
                  <th className="px-2 py-2 text-left text-gray-500">Tracking</th>
                  <th className="px-2 py-2 text-left text-gray-500">UTR</th>
                  <th className="px-2 py-2 text-right text-gray-500">Amount</th>
                  <th className="px-2 py-2 text-left text-gray-500">Date</th>
                  <th className="px-2 py-2 text-left text-gray-500">Description</th>
                </tr>
              </thead>
              <tbody>
                {bankItems.length === 0
                  ? <tr><td colSpan={7} className="text-center py-8 text-gray-400 text-sm">No items — load first</td></tr>
                  : bankItems.map(item => (
                    <TxnRow key={item.id} item={item}
                      isSelected={pendingBank?.id === item.id}
                      isQueued={queuedBankIds.has(item.id)}
                      onSelect={handleSelectBank}
                      color="blue" showDesc />
                  ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* Internal Side */}
        <div className="card p-0 overflow-hidden">
          <div className="px-4 py-3 bg-green-50 border-b border-green-100 flex items-center justify-between">
            <h3 className="font-semibold text-green-800 text-sm">Internal / Simplibank ({internalItems.length})</h3>
            {pendingInternal && <span className="text-xs text-green-600 font-medium">1 selected</span>}
          </div>
          <div className="overflow-auto max-h-96">
            <table className="w-full">
              <thead className="bg-gray-50 sticky top-0">
                <tr className="text-xs">
                  <th className="px-2 py-2 w-5"></th>
                  <th className="px-2 py-2 text-left text-gray-500">Eko TID</th>
                  <th className="px-2 py-2 text-left text-gray-500">Tracking</th>
                  <th className="px-2 py-2 text-left text-gray-500">UTR</th>
                  <th className="px-2 py-2 text-right text-gray-500">Amount</th>
                  <th className="px-2 py-2 text-left text-gray-500">Date</th>
                  <th className="px-2 py-2 text-left text-gray-500">CSP</th>
                </tr>
              </thead>
              <tbody>
                {internalItems.length === 0
                  ? <tr><td colSpan={7} className="text-center py-8 text-gray-400 text-sm">No items — load first</td></tr>
                  : internalItems.map(item => (
                    <TxnRow key={item.id} item={item}
                      isSelected={pendingInternal?.id === item.id}
                      isQueued={queuedInternalIds.has(item.id)}
                      onSelect={handleSelectInternal}
                      color="green" showCsp />
                  ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  )
}
