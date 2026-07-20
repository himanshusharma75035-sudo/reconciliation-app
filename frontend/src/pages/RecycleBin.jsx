import React, { useState, useEffect } from 'react'
import { Trash2, RotateCcw, X, AlertTriangle, Clock, Database } from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'

// Recycle Bin — every Clear action captures its rows here before deleting, so an
// accidental clear is undone in one click instead of a restore-from-backup.
export default function RecycleBin() {
  const [batches, setBatches] = useState([])
  const [retention, setRetention] = useState(30)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')          // batch_id currently restoring/purging

  const load = async () => {
    setLoading(true)
    try {
      const { data } = await api.get('/recycle-bin', { params: { include_restored: true } })
      setBatches(data.batches || [])
      setRetention(data.retention_days || 30)
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Could not load the recycle bin')
    } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const restore = async (b) => {
    if (!window.confirm(`Restore ${b.total_rows.toLocaleString()} row(s) deleted by ${b.deleted_by || 'someone'}?\nRows that already exist are skipped — this can't create duplicates.`)) return
    setBusy(b.batch_id)
    try {
      const { data } = await api.post(`/recycle-bin/${b.batch_id}/restore`)
      const n = Object.values(data.restored || {}).reduce((a, v) => a + v, 0)
      const s = Object.values(data.skipped_existing || {}).reduce((a, v) => a + v, 0)
      toast.success(`Restored ${n.toLocaleString()} row(s)${s ? ` (${s.toLocaleString()} already present, skipped)` : ''}`)
      load()
    } catch (e) { toast.error(e.response?.data?.detail || 'Restore failed') }
    finally { setBusy('') }
  }

  const purge = async (b) => {
    if (!window.confirm(`Permanently remove this batch from the recycle bin?\nThe ${b.total_rows.toLocaleString()} row(s) are already deleted — this only discards the ability to restore them. This cannot be undone.`)) return
    setBusy(b.batch_id)
    try {
      await api.delete(`/recycle-bin/${b.batch_id}`)
      toast.success('Removed from recycle bin')
      load()
    } catch (e) { toast.error(e.response?.data?.detail || 'Purge failed') }
    finally { setBusy('') }
  }

  const fmtWhen = (s) => s ? new Date(s).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }) : '—'
  const parseFilters = (f) => { try { return JSON.parse(f || '{}') } catch { return {} } }

  return (
    <div>
      <div className="flex items-center gap-2 mb-1">
        <Trash2 size={20} className="text-gray-700" />
        <h1 className="text-xl font-bold text-gray-800">Recycle Bin</h1>
      </div>
      <p className="text-sm text-gray-500 mb-5">
        Data removed by a <b>Clear</b> action is held here and can be restored. Entries are kept for {retention} days.
        Restoring never creates duplicates — rows that already exist are skipped.
      </p>

      {loading ? (
        <div className="text-center py-16 text-gray-400 text-sm">Loading…</div>
      ) : batches.length === 0 ? (
        <div className="card text-center py-16">
          <Trash2 size={34} className="mx-auto mb-3 text-gray-300" />
          <div className="text-sm font-medium text-gray-500">The recycle bin is empty</div>
          <div className="text-xs text-gray-400 mt-1">Nothing has been cleared recently.</div>
        </div>
      ) : (
        <div className="space-y-3">
          {batches.map(b => {
            const restored = !!b.restored_at
            const filters = parseFilters(b.filters)
            const hasFilters = filters && Object.keys(filters).length > 0
            return (
              <div key={b.batch_id} className={`card ${restored ? 'opacity-60' : ''}`}>
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-xs px-2 py-0.5 rounded-full bg-primary/10 text-primary font-semibold uppercase tracking-wide">{b.module || 'core'}</span>
                      <span className="text-sm font-semibold text-gray-800">{b.total_rows.toLocaleString()} rows</span>
                      {restored && <span className="text-xs px-2 py-0.5 rounded-full bg-green-50 text-green-700 font-medium">Restored</span>}
                    </div>
                    <div className="flex items-center gap-1.5 text-xs text-gray-500 mt-1.5">
                      <Clock size={12} /> {fmtWhen(b.deleted_at)} · by <b className="text-gray-600">{b.deleted_by || 'unknown'}</b>
                    </div>
                    {b.reason && <div className="text-xs text-gray-500 mt-1 italic">“{b.reason}”</div>}
                    {hasFilters && (
                      <div className="text-[11px] text-gray-400 mt-1">
                        scope: {Object.entries(filters).map(([k, v]) => `${k}=${v}`).join(' · ')}
                      </div>
                    )}
                    <div className="flex items-center gap-1.5 flex-wrap mt-2">
                      <Database size={12} className="text-gray-300" />
                      {Object.entries(b.tables || {}).map(([t, n]) => (
                        <span key={t} className="text-[11px] px-1.5 py-0.5 rounded bg-gray-50 text-gray-500 border border-gray-100 font-mono">
                          {t}: {n.toLocaleString()}
                        </span>
                      ))}
                    </div>
                    {restored && (
                      <div className="text-[11px] text-green-600 mt-2">Restored {fmtWhen(b.restored_at)} by {b.restored_by || '—'}</div>
                    )}
                  </div>
                  <div className="flex flex-col gap-2 flex-shrink-0">
                    {!restored && (
                      <button onClick={() => restore(b)} disabled={busy === b.batch_id}
                        className="flex items-center gap-1.5 text-xs font-medium px-3 py-1.5 rounded-lg bg-primary text-white hover:bg-primary-light disabled:opacity-50">
                        <RotateCcw size={13} /> {busy === b.batch_id ? 'Restoring…' : 'Restore'}
                      </button>
                    )}
                    <button onClick={() => purge(b)} disabled={busy === b.batch_id}
                      className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg border border-gray-200 text-gray-500 hover:border-red-200 hover:text-red-600 disabled:opacity-50">
                      <X size={13} /> Purge
                    </button>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}

      <div className="flex items-start gap-2 text-xs text-gray-400 mt-6">
        <AlertTriangle size={13} className="mt-0.5 flex-shrink-0" />
        <span>Restoring re-inserts the original rows exactly as they were. After restoring reconciliation data, re-run the affected recon so results reflect the recovered rows.</span>
      </div>
    </div>
  )
}
