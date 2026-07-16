import React, { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { AlertOctagon, CheckCircle, Search, TrendingDown } from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'
import { hasPermission } from '../utils/permissions'
import { isCoreLedgerPartner } from '../productRegistry'

export default function Mismatches() {
  const [filters, setFilters] = useState({ partner: '', date_from: '', date_to: '' })
  const [partnerList, setPartnerList] = useState([])

  useEffect(() => {
    api.get('/admin/partners-public')
      .then(({ data }) => { const core = (data || []).filter(isCoreLedgerPartner); if (core.length) setPartnerList(core) })
      .catch(() => {})
  }, [])
  const [rows, setRows]       = useState([])
  const [loading, setLoading] = useState(false)
  const [resolving, setResolving] = useState(null)

  const canResolve = hasPermission('src_assign')

  const upd = patch => setFilters(f => ({ ...f, ...patch }))

  const load = async () => {
    setLoading(true)
    try {
      const { data } = await api.get('/recon/mismatches', {
        params: {
          partner:   filters.partner   || undefined,
          date_from: filters.date_from || undefined,
          date_to:   filters.date_to   || undefined,
        }
      })
      setRows(data)
    } catch { toast.error('Failed to load mismatches') }
    finally { setLoading(false) }
  }

  const resolve = async (bankTxnId, matchId) => {
    if (!window.confirm(`Accept the amount discrepancy for ${matchId}?\nThis will mark both sides as Matched.`)) return
    setResolving(bankTxnId)
    try {
      await api.post(`/recon/mismatches/${bankTxnId}/resolve`)
      toast.success('Resolved — pair marked as Matched')
      setRows(r => r.filter(x => x.bank_txn_id !== bankTxnId))
    } catch { toast.error('Resolve failed') }
    finally { setResolving(null) }
  }

  const totalDelta = rows.reduce((s, r) => s + Math.abs(r.delta), 0)

  return (
    <div>
      <h1 className="text-xl font-bold text-gray-800 mb-1">Amount Mismatches</h1>
      <p className="text-sm text-gray-500 mb-5">
        Pairs where IDs matched but amounts differ — across <b>every product</b>. Core-ledger
        pairs can be accepted here; E-Value / BBPS pairs are resolved in their own window.
      </p>

      {/* Filter bar */}
      <div className="card mb-5">
        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className="text-xs text-gray-400 block mb-1">Partner</label>
            <select className="select" value={filters.partner} onChange={e => upd({ partner: e.target.value })}>
              <option value="">All</option>
              {partnerList.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">From Date</label>
            <input type="date" className="input" value={filters.date_from}
              onChange={e => upd({ date_from: e.target.value })} />
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">To Date</label>
            <input type="date" className="input" value={filters.date_to}
              onChange={e => upd({ date_to: e.target.value })} />
          </div>
        </div>
        <button onClick={load} disabled={loading}
          className="btn-primary mt-3 flex items-center gap-2">
          <Search size={14} /> {loading ? 'Loading…' : 'Load Mismatches'}
        </button>
      </div>

      {/* Summary bar */}
      {rows.length > 0 && (
        <div className="grid grid-cols-3 gap-4 mb-5">
          <div className="card p-4 bg-orange-50 border border-orange-100">
            <div className="text-2xl font-bold text-orange-600">{rows.length}</div>
            <div className="text-xs text-gray-500">Mismatch Pairs</div>
          </div>
          <div className="card p-4 bg-red-50 border border-red-100">
            <div className="text-2xl font-bold text-red-600">
              ₹{totalDelta.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
            </div>
            <div className="text-xs text-gray-500">Total Discrepancy</div>
          </div>
          <div className="card p-4 bg-blue-50 border border-blue-100">
            <div className="text-2xl font-bold text-blue-600">
              ₹{rows.length > 0 ? (totalDelta / rows.length).toLocaleString('en-IN', { minimumFractionDigits: 2 }) : '0'}
            </div>
            <div className="text-xs text-gray-500">Avg Delta per Pair</div>
          </div>
        </div>
      )}

      {/* Table */}
      <div className="card p-0 overflow-hidden">
        {rows.length === 0 ? (
          <div className="text-center py-12 text-gray-400">
            <AlertOctagon size={36} className="mx-auto mb-3 opacity-30" />
            <p className="text-sm">No mismatches found — use the filter above to load</p>
            <p className="text-xs mt-1 text-gray-300">Amount mismatches occur when TIDs match but amounts differ by more than ₹1</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b border-gray-100">
                <tr>
                  <th className="table-th">Match ID</th>
                  <th className="table-th">Product</th>
                  <th className="table-th">Date</th>
                  <th className="table-th">Eko TID</th>
                  <th className="table-th text-right text-blue-600">Bank Amt</th>
                  <th className="table-th text-right text-green-600">Internal Amt</th>
                  <th className="table-th text-right text-red-500">Delta</th>
                  <th className="table-th">Bank Description</th>
                  <th className="table-th">Internal CSP</th>
                  <th className="table-th">Action</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(row => (
                  <tr key={row.bank_txn_id} className="hover:bg-orange-50/30 border-b border-gray-50">
                    <td className="table-td font-mono text-xs text-primary">{row.match_id}</td>
                    <td className="table-td">
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${row.core ? 'bg-gray-100 text-gray-600' : 'bg-purple-50 text-purple-700'}`}>
                        {row.product || row.partner}
                      </span>
                    </td>
                    <td className="table-td font-mono text-xs">{row.recon_date}</td>
                    <td className="table-td font-mono text-xs">
                      <span title={`Bank: ${row.bank_eko_tid}\nInternal: ${row.internal_eko_tid}`}>
                        {row.bank_eko_tid}
                      </span>
                    </td>
                    <td className="table-td text-right tabular-nums text-blue-700 font-medium">
                      ₹{(row.bank_amount || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                    </td>
                    <td className="table-td text-right tabular-nums text-green-700 font-medium">
                      ₹{(row.internal_amount || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                    </td>
                    <td className="table-td text-right tabular-nums">
                      <span className={`font-semibold ${Math.abs(row.delta) > 100 ? 'text-red-600' : 'text-orange-500'}`}>
                        {row.delta > 0 ? '+' : ''}{(row.delta || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                      </span>
                    </td>
                    <td className="table-td max-w-[16rem]">
                      <span className="text-xs text-gray-500 block truncate" title={row.bank_description || ''}>{row.bank_description || '—'}</span>
                    </td>
                    <td className="table-td max-w-[12rem]">
                      {(row.internal_csp_code || row.internal_csp_name)
                        ? <span className="block leading-tight text-xs">
                            <span className="block font-mono text-gray-600 truncate" title={row.internal_csp_code || ''}>{row.internal_csp_code || '—'}</span>
                            {row.internal_csp_name && <span className="block text-[10px] text-gray-400 truncate" title={row.internal_csp_name}>{row.internal_csp_name}</span>}
                          </span>
                        : <span className="text-gray-300">—</span>}
                    </td>
                    <td className="table-td">
                      {!row.core ? (
                        <Link to={row.resolve_in || '#'} className="text-xs text-primary hover:underline whitespace-nowrap">
                          Resolve in {row.product} →
                        </Link>
                      ) : canResolve ? (
                        <button
                          onClick={() => resolve(row.bank_txn_id, row.match_id)}
                          disabled={resolving === row.bank_txn_id}
                          className="flex items-center gap-1 text-xs text-green-600 hover:text-green-800 disabled:opacity-50 border border-green-200 rounded-lg px-2 py-0.5 hover:bg-green-50">
                          <CheckCircle size={12} />
                          {resolving === row.bank_txn_id ? 'Resolving…' : 'Accept'}
                        </button>
                      ) : <span className="text-gray-300 text-xs">—</span>}
                    </td>
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
