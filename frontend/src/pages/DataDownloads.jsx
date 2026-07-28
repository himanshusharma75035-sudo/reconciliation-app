import React, { useState, useEffect } from 'react'
import { Download, RefreshCw, FileSpreadsheet, ShieldCheck } from 'lucide-react'
import api from '../utils/api'
import toast from 'react-hot-toast'

// Self-serve raw-data download center. Re-exports the ingested source rows (bank statements +
// internal dumps) as Excel, by product + side + date range. Gated by `data_download`; every
// pull is audited server-side. Faithful re-export of the stored data — not the original file.

const SIDES = [
  { key: 'bank', label: 'Bank Statement' },
  { key: 'internal', label: 'Internal Data' },
]

export default function DataDownloads() {
  const [catalog, setCatalog] = useState(null)
  const [loading, setLoading] = useState(true)
  const [product, setProduct] = useState('')
  const [side, setSide] = useState('bank')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.get('/downloads/catalog')
      .then(({ data }) => {
        setCatalog(data)
        const first = (data.products || [])[0]
        if (first) {
          setProduct(first.key)
          setSide(first.sides.bank?.count ? 'bank' : 'internal')
        }
      })
      .catch(() => toast.error('Failed to load the data catalog'))
      .finally(() => setLoading(false))
  }, [])

  const products = catalog?.products || []
  const core = products.filter(p => p.group === 'core')
  const modules = products.filter(p => p.group === 'module')
  const cur = products.find(p => p.key === product)
  const info = cur?.sides?.[side]

  const onProduct = k => {
    setProduct(k); setFrom(''); setTo('')
    const p = products.find(x => x.key === k)
    if (p) setSide(p.sides.bank?.count ? 'bank' : (p.sides.internal?.count ? 'internal' : 'bank'))
  }

  const download = async () => {
    if (!product || !info?.count) return
    setBusy(true)
    try {
      const r = await api.get('/downloads/export', {
        params: { product, side, date_from: from || undefined, date_to: to || undefined },
        responseType: 'blob',
      })
      const cd = r.headers['content-disposition'] || ''
      const m = cd.match(/filename="?([^"]+)"?/)
      const url = URL.createObjectURL(r.data)
      const a = document.createElement('a')
      a.href = url; a.download = m ? m[1] : `${product}_${side}.xlsx`; a.click()
      URL.revokeObjectURL(url)
      toast.success('Download started')
    } catch (e) {
      const msg = e?.response?.data?.detail || 'Download failed'
      toast.error(typeof msg === 'string' ? msg : 'Download failed')
    }
    setBusy(false)
  }

  return (
    <div className="max-w-3xl">
      <div className="mb-5">
        <h1 className="text-xl font-bold text-gray-800">Data Downloads</h1>
        <p className="text-sm text-gray-400">Download the raw uploaded bank statements &amp; internal data, by date range, as Excel.</p>
      </div>

      <div className="flex items-start gap-2 text-xs text-blue-800 bg-blue-50 border border-blue-100 rounded-lg px-3 py-2 mb-5">
        <ShieldCheck size={15} className="mt-0.5 shrink-0" />
        <span>A faithful re-export of the stored data (not the exact original file). These rows contain account numbers — access is restricted and <span className="font-semibold">every download is recorded in the audit log</span>.</span>
      </div>

      {loading ? (
        <div className="text-gray-400 text-sm flex items-center gap-2"><RefreshCw size={15} className="animate-spin" /> Loading catalog…</div>
      ) : products.length === 0 ? (
        <div className="text-gray-400 text-sm rounded-xl border border-gray-200 bg-white p-8 text-center">No uploaded data available to download yet.</div>
      ) : (
        <div className="rounded-xl border border-gray-200 bg-white p-5 space-y-4">
          {/* product */}
          <div>
            <label className="text-xs text-gray-400 block mb-1">Product</label>
            <select className="input w-full" value={product} onChange={e => onProduct(e.target.value)}>
              {core.length > 0 && (
                <optgroup label="Core products">
                  {core.map(p => <option key={p.key} value={p.key}>{p.label}</option>)}
                </optgroup>
              )}
              {modules.length > 0 && (
                <optgroup label="Modules">
                  {modules.map(p => <option key={p.key} value={p.key}>{p.label}</option>)}
                </optgroup>
              )}
            </select>
          </div>

          {/* side */}
          <div>
            <label className="text-xs text-gray-400 block mb-1">Which data</label>
            <div className="flex gap-2">
              {SIDES.map(s => {
                const c = cur?.sides?.[s.key]?.count || 0
                const active = side === s.key
                return (
                  <button key={s.key} onClick={() => c && setSide(s.key)} disabled={!c}
                    className={`flex-1 rounded-lg border px-3 py-2.5 text-sm text-left transition-colors ${
                      active ? 'border-primary bg-primary/5 text-primary'
                        : c ? 'border-gray-200 hover:border-gray-300 text-gray-700'
                        : 'border-gray-100 bg-gray-50 text-gray-300 cursor-not-allowed'}`}>
                    <div className="font-medium">{s.label}</div>
                    <div className="text-[11px] tabular-nums opacity-70">{c ? `${c.toLocaleString('en-IN')} rows` : 'none uploaded'}</div>
                  </button>
                )
              })}
            </div>
          </div>

          {/* dates */}
          <div className="flex flex-wrap items-end gap-3">
            <div><label className="text-xs text-gray-400 block mb-1">From Date</label>
              <input type="date" className="input" value={from} onChange={e => setFrom(e.target.value)} /></div>
            <div><label className="text-xs text-gray-400 block mb-1">To Date</label>
              <input type="date" className="input" value={to} onChange={e => setTo(e.target.value)} /></div>
            <div className="text-[11px] text-gray-400 pb-2">Leave blank for everything.
              {info?.count ? <> Available: <span className="font-mono">{info.min_date || '—'}</span> to <span className="font-mono">{info.max_date || '—'}</span>.</> : null}</div>
          </div>

          {/* action */}
          <div className="flex items-center justify-between pt-1 border-t border-gray-100">
            <div className="text-xs text-gray-500 flex items-center gap-1.5">
              <FileSpreadsheet size={14} className="text-green-600" />
              {info?.count ? `${info.count.toLocaleString('en-IN')} rows in this source` : 'Nothing to download for this selection'}
            </div>
            <button onClick={download} disabled={busy || !info?.count} className="btn flex items-center gap-1.5">
              {busy ? <RefreshCw size={15} className="animate-spin" /> : <Download size={15} />} Download Excel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
