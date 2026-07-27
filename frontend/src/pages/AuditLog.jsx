import React, { useState, useEffect, useCallback } from 'react'
import { RefreshCw, Search, Shield, Download } from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'

const ACTION_BADGE = {
  upload:             'bg-blue-50 text-blue-700',
  run_recon:          'bg-purple-50 text-purple-700',
  run_neft_d1:        'bg-purple-50 text-purple-700',
  interbank_match:    'bg-indigo-50 text-indigo-700',
  manual_match:       'bg-green-50 text-green-700',
  assign_src:         'bg-yellow-50 text-yellow-700',
  bulk_src:           'bg-yellow-50 text-yellow-700',
  tag_adhoc:          'bg-orange-50 text-orange-700',
  human_override:     'bg-red-50 text-red-700',
  human_unmatch:      'bg-red-50 text-red-700',
  human_rematch:      'bg-red-50 text-red-700',
  override_status:    'bg-red-50 text-red-700',
  clear:              'bg-red-50 text-red-700',
  delete_selected:    'bg-red-50 text-red-700',
}

// action_type icons — visually separates app automation from human decisions.
// FALLBACK is used for any unrecognised actor type (e.g. 'system' repairs/config
// tasks) so an unexpected value can NEVER crash the whole page render.
const ACTION_TYPE_BADGE = {
  app:    { label: '🤖 App',    cls: 'bg-gray-100 text-gray-500' },
  human:  { label: '👤 Human',  cls: 'bg-amber-100 text-amber-700 font-semibold' },
  system: { label: '⚙️ System', cls: 'bg-slate-100 text-slate-600' },
}
const ACTION_TYPE_FALLBACK = { label: '⚙️ System', cls: 'bg-slate-100 text-slate-600' }

export default function AuditLog() {
  const [logs, setLogs]       = useState([])
  const [total, setTotal]     = useState(0)
  const [page, setPage]       = useState(1)
  const [loading, setLoading] = useState(false)
  const [actions, setActions] = useState([])

  const [filters, setFilters] = useState({
    action:      '',
    action_type: '',   // '' = all | 'app' | 'human'
    from_date:   '',
    to_date:     '',
  })

  const load = useCallback(async (p = 1) => {
    setLoading(true)
    try {
      const params = { page: p, page_size: 50, ...Object.fromEntries(Object.entries(filters).filter(([,v]) => v)) }
      const { data } = await api.get('/audit/logs', { params })
      setLogs(data.logs); setTotal(data.total); setPage(p)
    } catch { toast.error('Failed to load audit log') }
    finally { setLoading(false) }
  }, [filters])

  useEffect(() => { load(1) }, [filters])

  const handleExport = async () => {
    try {
      const params = new URLSearchParams(Object.fromEntries(Object.entries(filters).filter(([,v]) => v)))
      const resp = await api.get('/audit/logs/export?' + params, { responseType: 'blob' })
      const url = URL.createObjectURL(resp.data)
      const a = document.createElement('a'); a.href = url; a.download = 'audit_log.xlsx'; a.click()
      toast.success('Exported')
    } catch { toast.error('Export failed') }
  }

  useEffect(() => {
    api.get('/audit/actions').then(r => setActions(r.data)).catch(() => {})
  }, [])

  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <div className="flex items-center gap-2">
          <Shield size={20} className="text-primary" />
          <h1 className="text-xl font-bold text-gray-800">Audit Log</h1>
        </div>
        <div className="flex gap-2">
          <button onClick={handleExport} className="btn-ghost flex items-center gap-1.5">
            <Download size={14} /> Export Excel
          </button>
          <button onClick={() => load(1)} className="btn-ghost flex items-center gap-1.5">
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> Refresh
          </button>
        </div>
      </div>
      <p className="text-sm text-gray-500 mb-5">{total.toLocaleString()} events recorded</p>

      {/* Filters */}
      <div className="card mb-5">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div>
            <label className="text-xs text-gray-400 block mb-1">Actor Type</label>
            <select className="select" value={filters.action_type} onChange={e => setFilters({...filters, action_type: e.target.value})}>
              <option value="">All (App + Human)</option>
              <option value="human">👤 Human only — see who changed what</option>
              <option value="app">🤖 App only — automated actions</option>
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">Action</label>
            <select className="select" value={filters.action} onChange={e => setFilters({...filters, action: e.target.value})}>
              <option value="">All</option>
              {actions.map(a => <option key={a} value={a}>{a}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">From Date</label>
            <input type="date" className="input" value={filters.from_date}
              onChange={e => setFilters({...filters, from_date: e.target.value})} />
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">To Date</label>
            <input type="date" className="input" value={filters.to_date}
              onChange={e => setFilters({...filters, to_date: e.target.value})} />
          </div>
        </div>
        <div className="flex gap-2 mt-3">
          <button onClick={() => load(1)} className="btn-primary flex items-center gap-1.5"><Search size={14} /> Filter</button>
          <button onClick={() => setFilters({ action: '', action_type: '', from_date: '', to_date: '' })} className="btn-ghost">Clear</button>
        </div>
      </div>

      {/* Table */}
      <div className="card p-0 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-100">
              <tr>
                <th className="table-th">Actor</th>
                <th className="table-th">Timestamp</th>
                <th className="table-th">User</th>
                <th className="table-th">Action</th>
                <th className="table-th">Entity</th>
                <th className="table-th">Details</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={6} className="text-center py-10 text-gray-400">Loading...</td></tr>
              ) : logs.length === 0 ? (
                <tr><td colSpan={6} className="text-center py-10 text-gray-400">No audit events yet</td></tr>
              ) : (
                logs.map(log => {
                  const atBadge = ACTION_TYPE_BADGE[log.action_type] || ACTION_TYPE_FALLBACK
                  return (
                  <tr key={log.id} className={`hover:bg-gray-50 border-b border-gray-50 ${log.action_type === 'human' ? 'bg-amber-50/30' : ''}`}>
                    <td className="table-td">
                      <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${atBadge.cls}`}>
                        {atBadge.label}
                      </span>
                    </td>
                    <td className="table-td font-mono text-xs text-gray-500 whitespace-nowrap">
                      {log.created_at?.replace('T', ' ').slice(0, 19)}
                    </td>
                    <td className="table-td font-medium">{log.username || '—'}</td>
                    <td className="table-td">
                      <span className={`text-xs px-2 py-0.5 rounded font-medium ${ACTION_BADGE[log.action] || 'bg-gray-100 text-gray-600'}`}>
                        {log.action}
                      </span>
                    </td>
                    <td className="table-td text-xs text-gray-500">
                      {log.entity_type && <span className="font-medium text-gray-700">{log.entity_type}</span>}
                      {log.entity_id && <span className="font-mono ml-1 text-gray-400 text-xs">{log.entity_id.slice(0, 8)}…</span>}
                    </td>
                    <td className="table-td text-xs text-gray-600 max-w-xs">
                      {log.detail
                        ? <details className="cursor-pointer">
                            <summary className="text-primary hover:underline">View details</summary>
                            <pre className="mt-1 bg-gray-50 rounded p-2 text-xs overflow-auto max-h-40 whitespace-pre-wrap">
                              {JSON.stringify(log.detail, null, 2)}
                            </pre>
                          </details>
                        : '—'}
                    </td>
                  </tr>
                  )
                })
              )}
            </tbody>
          </table>
        </div>

        {total > 50 && (
          <div className="flex items-center justify-between px-4 py-3 border-t border-gray-100 text-sm">
            <span className="text-gray-500">Showing {((page-1)*50)+1}–{Math.min(page*50, total)} of {total.toLocaleString()}</span>
            <div className="flex gap-2">
              <button onClick={() => load(page-1)} disabled={page === 1} className="btn-ghost px-3 py-1 text-xs disabled:opacity-40">Prev</button>
              <button onClick={() => load(page+1)} disabled={page*50 >= total} className="btn-ghost px-3 py-1 text-xs disabled:opacity-40">Next</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
