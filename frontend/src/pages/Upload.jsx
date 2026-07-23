import React, { useState, useRef, useEffect } from 'react'
import { Upload as UploadIcon, X, ChevronRight, CheckCircle, FileText, Filter, Landmark, Building2, History, AlertTriangle, ExternalLink } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import toast from 'react-hot-toast'
import api from '../utils/api'

// ── AePS Settlement Upload Component ─────────────────────────────────────────
function AepsSettlementUpload() {
  const [uploading, setUploading] = useState(null)  // 'tplus' | 'anomaly' | 'cib'
  const fileRefs = useRef({})   // stable refs per card (audit F7: no createRef in render)

  const SETTLEMENT_CARDS = [
    {
      key:      'tplus',
      label:    'T Plus Report',
      desc:     'Tplus1Settl*.xlsx — settlement batch with deduction breakdown',
      icon:     '📊',
      endpoint: '/aeps/upload/tplus',
      hint:     'One row per settlement. Formula: Settlement = Txn − Anomaly − 2FA − CIB',
    },
    {
      key:      'anomaly',
      label:    'Anomaly RRN Report',
      desc:     "Anomaly RRN's Report*.xlsx — individual unsettled transactions",
      icon:     '⚠️',
      endpoint: '/aeps/upload/anomaly',
      hint:     'Each RRN = one unsettled transaction deducted from settlement',
    },
    {
      key:      'cib',
      label:    'CIB Report',
      desc:     'CIBReport*.xlsx — chargebacks and penalty deductions',
      icon:     '🔴',
      endpoint: '/aeps/upload/cib',
      hint:     'Each row = one chargeback or FRAUD/PENALTY recovery',
    },
  ]

  const handleSettlementUpload = async (card, file) => {
    if (!file) return
    setUploading(card.key)
    try {
      const fd = new FormData()
      fd.append('file', file)
      const { data } = await api.post(card.endpoint, fd)
      const dup = data.duplicates || 0
      toast.success(`${card.label}: ${data.inserted} rows uploaded${dup ? ` · ${dup} duplicate${dup > 1 ? 's' : ''} skipped` : ''}`,
        { duration: dup ? 7000 : 4000 })
      if (dup && !data.inserted) toast(`All ${dup} rows already exist — nothing new added.`, { icon: '⚠️', duration: 7000 })
      if (data.errors?.length) toast.error(`${data.errors.length} row errors — check file`)
    } catch (err) {
      toast.error(err.response?.data?.detail || `${card.label} upload failed`)
    } finally {
      setUploading(null)
    }
  }

  return (
    <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
      {SETTLEMENT_CARDS.map(card => {
        const fileRef = fileRefs.current[card.key] || (fileRefs.current[card.key] = React.createRef())
        return (
          <div key={card.key} className="rounded-xl border-2 border-gray-200 bg-white p-4 hover:border-purple-300 transition-colors">
            <div className="flex items-center gap-2 mb-3">
              <span className="text-xl">{card.icon}</span>
              <div>
                <div className="font-semibold text-gray-800 text-sm">{card.label}</div>
                <div className="text-xs text-gray-400">{card.desc}</div>
              </div>
            </div>
            <p className="text-xs text-purple-600 bg-purple-50 rounded-lg px-2.5 py-1.5 mb-3">{card.hint}</p>
            <div className="space-y-2">
              <input
                ref={fileRef}
                type="file"
                accept=".xlsx,.xls"
                className="hidden"
                onChange={e => { if (e.target.files[0]) handleSettlementUpload(card, e.target.files[0]) }}
              />
              <button
                onClick={() => fileRef.current.click()}
                disabled={uploading === card.key}
                className="btn-primary w-full text-sm"
                style={{ background: '#7c3aed' }}
              >
                {uploading === card.key ? 'Uploading…' : `Upload ${card.label} →`}
              </button>
            </div>
          </div>
        )
      })}
      <div className="xl:col-span-3 text-right">
        <Link to="/aeps-settlement" className="text-xs text-purple-600 hover:underline flex items-center gap-1 justify-end">
          View Settlement Recon Dashboard <ExternalLink size={11} />
        </Link>
      </div>
    </div>
  )
}

const STANDARD_FIELDS = [
  { key: 'eko_tid',           label: 'Eko TID',                    required: false, hint: 'auto-extracted from Description if skipped' },
  { key: 'tracking_number',   label: 'Tracking Number',            required: false, hint: 'auto-extracted from Description if skipped' },
  { key: 'utr_number',        label: 'UTR Number (NEFT)',          required: false, hint: 'auto-extracted from Description if skipped' },
  { key: 'amount',            label: 'Amount',                     required: false },
  { key: 'status',            label: 'Status',                     required: false },
  { key: 'transaction_date',  label: 'Transaction Date',           required: false },
  { key: 'description',       label: 'Description (auto-extract)', required: false, hint: 'map this to auto-extract TID + Tracking + UTR' },
]

// Product definitions come from the central registry — single source of truth
// (audit fix F5). Add a product once in productRegistry.js and it appears here.
import { PRODUCTS } from '../productRegistry'
import { filterProductsByAccess } from '../utils/permissions'

const FALLBACK_DMT_BANKS = [
  { slug: 'fino',   display_name: 'Fino Payments Bank',  has_bank_statement: true },
  { slug: 'airtel', display_name: 'Airtel Payments Bank', has_bank_statement: true },
  { slug: 'axis',   display_name: 'Axis Bank',           has_bank_statement: true },
]

// ── QR Settlement Upload component ───────────────────────────────────────────
function QRSettlementUpload() {
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef(null)

  const handleUpload = async (file) => {
    if (!file) return
    setUploading(true)
    const attempt = force => { const fd = new FormData(); fd.append('file', file); return api.post(`/qr/upload/settlement${force ? '?force=true' : ''}`, fd) }
    try {
      let res
      try { res = await attempt(false) }
      catch (err) {
        const detail = err.response?.data?.detail || 'Settlement upload failed'
        // 409 [DUPLICATE] → offer Re-upload (replace): re-applying upserts per Settlement
        // ID, so existing batches are replaced and new ones added — never doubled.
        if (err.response?.status === 409 &&
            window.confirm(`${detail}\n\nRe-upload anyway and re-apply this file? Existing settlement batches are replaced, not duplicated.`)) {
          res = await attempt(true)
        } else { toast.error(detail); return }
      }
      const data = res.data
      toast.success(`Settlement: ${data.inserted} batches uploaded${data.replaced ? ` (${data.replaced} replaced)` : ''}`)
      if (data.errors?.length) toast.error(`${data.errors.length} formula errors — check file`)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Settlement upload failed')
    } finally { setUploading(false) }
  }

  return (
    <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
      <div className="rounded-xl border-2 border-gray-200 bg-white p-4 hover:border-indigo-300 transition-colors">
        <div className="flex items-center gap-2 mb-3">
          <span className="text-xl">💰</span>
          <div>
            <div className="font-semibold text-gray-800 text-sm">Settlement Report</div>
            <div className="text-xs text-gray-400">ExportSettlementsList_*.xlsx — batch settlement breakdown</div>
          </div>
        </div>
        <p className="text-xs text-indigo-600 bg-indigo-50 rounded-lg px-2.5 py-1.5 mb-3">
          Formula: Net = Amount − Fees − Fees Tax − Early Settlement − Early Settlement Tax
        </p>
        <input ref={fileRef} type="file" accept=".xlsx,.xls" className="hidden"
          onChange={e => { if (e.target.files[0]) handleUpload(e.target.files[0]) }} />
        <button onClick={() => fileRef.current.click()} disabled={uploading}
          className="btn-primary w-full text-sm" style={{ background: '#4f46e5' }}>
          {uploading ? 'Uploading…' : 'Upload Settlement Report →'}
        </button>
      </div>
      <div className="rounded-xl border-2 border-gray-200 bg-white p-4 flex flex-col justify-between">
        <div>
          <div className="flex items-center gap-2 mb-3">
            <span className="text-xl">🔴</span>
            <div>
              <div className="font-semibold text-gray-800 text-sm">Chargebacks</div>
              <div className="text-xs text-gray-400">Manual entry from Paypoint chargeback emails</div>
            </div>
          </div>
          <p className="text-xs text-red-600 bg-red-50 rounded-lg px-2.5 py-1.5">
            Enter chargeback details from email: Case Date, RRN, Amount, TAT. RRN auto cross-references bank transactions.
          </p>
        </div>
        <Link to="/qr-settlement" className="btn-primary w-full text-sm mt-3 text-center block"
          style={{ background: '#dc2626' }}>
          Manage Chargebacks →
        </Link>
      </div>
    </div>
  )
}

// ── SBI Kiosk Upload Panel (inline — no column mapping step needed) ───────────
function SBIKioskUploads() {
  const [uploading, setUploading] = useState(null)

  const SBIUploadCard = ({ label, desc, endpoint, hint, color = '#1d4ed8', extraField = null }) => {
    const [file, setFile] = useState(null)
    const [extra, setExtra] = useState('')
    const ref = useRef(null)

    const doUpload = async () => {
      if (!file) return toast.error('Select a file first')
      setUploading(label)
      const attempt = force => {
        const fd = new FormData()
        fd.append('file', file)
        if (extraField && extra) fd.append(extraField.name, extra)
        return api.post(`${endpoint}${force ? '?force=true' : ''}`, fd)
      }
      try {
        let res
        try { res = await attempt(false) }
        catch (err) {
          const detail = err.response?.data?.detail || `${label} failed`
          // 409 [DUPLICATE] → Re-upload (replace): every SBI upload now REPLACES its own
          // scope (its dates/product/BC) or dedups, so re-applying never doubles rows.
          if (err.response?.status === 409 &&
              window.confirm(`${detail}\n\nRe-upload anyway and re-apply this file? Rows it already covers are replaced, not duplicated.`)) {
            res = await attempt(true)
          } else { toast.error(detail); return }
        }
        const data = res.data
        toast.success(`${label}: ${data.inserted} rows uploaded${data.replaced ? ` (${data.replaced} replaced)` : ''}`)
        setFile(null)
      } catch (err) {
        toast.error(err.response?.data?.detail || `${label} failed`)
      } finally { setUploading(null) }
    }

    return (
      <div className={`rounded-xl border-2 p-4 transition-all ${file ? 'border-rose-400 bg-rose-50/30' : 'border-gray-200 bg-white hover:border-rose-300'}`}>
        <div className="font-semibold text-gray-800 text-sm mb-1">{label}</div>
        <div className="text-xs text-gray-400 mb-2">{desc}</div>
        {hint && <p className="text-xs text-rose-600 bg-rose-50 rounded px-2 py-1.5 mb-2">{hint}</p>}
        {extraField && (
          <div className="mb-2">
            <label className="text-xs text-gray-500 block mb-1">{extraField.label}</label>
            <input type="date" className="input text-xs" onChange={e => setExtra(e.target.value)} />
          </div>
        )}
        <input ref={ref} type="file" accept=".xls,.xlsx,.csv" className="hidden"
          onChange={e => { if (e.target.files[0]) setFile(e.target.files[0]) }} />
        <div onClick={() => ref.current.click()}
          className="border-2 border-dashed border-gray-200 rounded-lg p-3 text-center cursor-pointer hover:border-rose-300 mb-2">
          {file
            ? <div className="flex items-center justify-center gap-2 text-xs text-gray-600">
                <FileText size={12}/><span className="truncate max-w-[180px]">{file.name}</span>
                <button onClick={e => { e.stopPropagation(); setFile(null) }} className="text-gray-400 hover:text-red-500"><X size={11}/></button>
              </div>
            : <div className="text-gray-400 text-xs"><FileText size={14} className="mx-auto mb-1"/>Click to select</div>
          }
        </div>
        <button onClick={doUpload} disabled={!file || uploading === label}
          className="btn-primary w-full text-sm" style={{ background: file ? color : undefined }}>
          {uploading === label ? 'Uploading…' : `Upload →`}
        </button>
      </div>
    )
  }

  const sections = [
    {
      label: 'P01 — Settlement Recon', color: '#1d4ed8',
      cards: [
        { label: 'Bank Statement', desc: 'SBI settlement account .xls (tab-separated, all daily transactions)', endpoint: '/sbi/upload/bank-statement', hint: 'Settlement rows auto-detected by EKOSETTLEMENT keyword' },
        { label: 'KO Limits Config Report', desc: 'KO_Limits_Configuration_Report_*.xls — KO Deposit/Withdrawal per CSP', endpoint: '/sbi/upload/ko-limits' },
      ]
    },
    {
      label: 'P02 — Bank + Transaction Report Recon', color: '#15803d',
      cards: [
        { label: 'Transaction Report File', desc: 'Upload each of the 7 BC Transaction Report files separately (AEPS, Withdrawal, Deposit, Money Transfer, Other, etc.)', endpoint: '/sbi/upload/txn-report', hint: 'All 7 files share the same column structure — upload them one by one' },
      ]
    },
    {
      label: 'P03 — CSP-Transaction-Bank Recon', color: '#7e22ce',
      cards: [
        { label: 'CSP Master Sheet', desc: 'CSP Code | Ref Number | Mode (Cash/CDM/Electronic) — reference numbers for matching', endpoint: '/sbi/upload/csp-master' },
      ]
    },
    {
      label: 'P04 — Wallet Balance Recon', color: '#d97706',
      cards: [
        { label: 'KO Cash Holding Report', desc: 'KO_Cash_Holding_Report_*.xls — opening/closing balances per KO', endpoint: '/sbi/upload/ko-cash-holding', extraField: { name: 'report_date', label: 'Report Date' } },
        { label: 'Limit Update Failure Report', desc: 'Limit_Fail_*.xls — KOs whose wallet limit update failed', endpoint: '/sbi/upload/limit-failures' },
      ]
    },
  ]

  return (
    <div className="space-y-6 max-w-5xl">
      <div className="card bg-rose-50 border border-rose-100 p-4 text-xs text-rose-700">
        <span className="font-semibold">SBI Kiosk Banking — 4 Reconciliation Processes.</span> Upload all required files below, then run each process from the{' '}
        <Link to="/sbi-kiosk" className="underline font-semibold">SBI Kiosk Recon</Link> page in the sidebar.
      </div>
      {sections.map(section => (
        <div key={section.label}>
          <div className="flex items-center gap-2 mb-3">
            <span className="text-xs font-bold px-2.5 py-1 rounded-full uppercase tracking-wide"
              style={{ background: section.color + '18', color: section.color }}>
              {section.label}
            </span>
          </div>
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            {section.cards.map(card => (
              <SBIUploadCard key={card.label} {...card} color={section.color} />
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

// ── Generic two-card product layout (reused for Digikhata, Indonepal, QR) ─────
function GenericProductCards({ product, accentColor, cards, config, setConfig, dateMode, setDateMode,
                               file, setFile, fileRef, uploading, handleUpload, onFileSelect, hints = {} }) {
  const ac = {
    cyan:   { border: 'border-cyan-500',   bg: 'bg-cyan-50/30',   hover: 'hover:border-cyan-400',
              label: 'bg-cyan-100 text-cyan-700',   dateBorder: 'border-cyan-500 bg-cyan-50 text-cyan-700',
              info: 'bg-cyan-50 border-cyan-100 text-cyan-700',   btn: '#0891b2' },
    orange: { border: 'border-orange-500', bg: 'bg-orange-50/30', hover: 'hover:border-orange-400',
              label: 'bg-orange-100 text-orange-700', dateBorder: 'border-orange-500 bg-orange-50 text-orange-700',
              info: 'bg-orange-50 border-orange-100 text-orange-700', btn: '#f97316' },
    teal:   { border: 'border-teal-500',   bg: 'bg-teal-50/30',   hover: 'hover:border-teal-400',
              label: 'bg-teal-100 text-teal-700',   dateBorder: 'border-teal-500 bg-teal-50 text-teal-700',
              info: 'bg-teal-50 border-teal-100 text-teal-700',   btn: '#0d9488' },
  }[accentColor] || {}

  return (
    <div className="grid grid-cols-1 xl:grid-cols-2 gap-5 max-w-5xl">
      {cards.map(card => (
        <div key={card.side}
          className={`rounded-xl border-2 p-5 transition-all ${config.side === card.side ? `${ac.border} ${ac.bg}` : `border-gray-200 bg-white ${ac.hover}`}`}>
          <div className="flex items-center gap-2.5 mb-4">
            <div className={`p-2 ${ac.label?.replace('text-', 'bg-').replace('-700', '-100')} rounded-lg text-xl`}>{card.icon}</div>
            <div>
              <h2 className="font-bold text-gray-800">{card.label}</h2>
              <p className="text-xs text-gray-400">{card.desc}</p>
            </div>
          </div>
          <div className="space-y-3">
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Recon Date</label>
              <div className="flex gap-2 mb-1.5">
                {['single','auto'].map(m => (
                  <label key={m} className={`flex items-center gap-1.5 text-xs cursor-pointer px-2.5 py-1 rounded-lg border transition-all
                    ${dateMode === m && config.side === card.side ? ac.dateBorder : 'border-gray-200 text-gray-500'}`}>
                    <input type="radio" value={m}
                      checked={dateMode === m && config.side === card.side}
                      onChange={() => { setConfig(c => ({...c, side: card.side, partner: card.partner})); setDateMode(m) }}
                      className="hidden" />
                    {m === 'single' ? 'Single date' : 'Auto-detect'}
                  </label>
                ))}
              </div>
              {config.side === card.side && dateMode === 'single'
                ? <input type="date" className="input" value={config.recon_date}
                    onChange={e => setConfig(c => ({...c, side: card.side, partner: card.partner, recon_date: e.target.value}))} />
                : <p className={`text-xs rounded-lg px-3 py-2 ${ac.info}`}>Date auto-detected from each row</p>
              }
            </div>
            {hints[card.side] && (
              <p className={`text-xs rounded-lg px-3 py-2 border ${ac.info}`}>{hints[card.side]}</p>
            )}
            <div
              onClick={() => { setConfig(c => ({...c, side: card.side, partner: card.partner})); fileRef.current.click() }}
              className={`border-2 border-dashed border-gray-200 rounded-xl p-5 text-center cursor-pointer ${ac.hover} transition-colors`}>
              {file && config.side === card.side
                ? <div className="flex items-center justify-center gap-2 text-gray-600">
                    <FileText size={16}/><span className="text-sm font-medium truncate max-w-xs">{file.name}</span>
                    <button onClick={e => { e.stopPropagation(); setFile(null) }} className="text-gray-400 hover:text-red-500"><X size={13}/></button>
                  </div>
                : <div className="text-gray-400"><FileText size={20} className="mx-auto mb-1.5"/><p className="text-xs">Click to select file</p><p className="text-xs text-gray-300 mt-0.5">Excel · CSV · PDF</p></div>
              }
            </div>
            <button
              onClick={() => { setConfig(c => ({...c, side: card.side, partner: card.partner})); handleUpload() }}
              disabled={!file || config.side !== card.side || uploading || (dateMode === 'single' && !config.recon_date)}
              className="btn-primary w-full"
              style={{ background: config.side === card.side ? ac.btn : undefined }}>
              {uploading && config.side === card.side ? 'Parsing...' : `Upload ${card.label} →`}
            </button>
          </div>
        </div>
      ))}
      <input ref={fileRef} type="file" accept=".csv,.xlsx,.xls,.pdf" className="hidden" onChange={onFileSelect} />
    </div>
  )
}

const PRODUCT_PARTNER = { dmt: 'fino', pg: 'pg', qr: 'qr', digikhata: 'digikhata', indonepal: 'indonepal', aeps: 'aeps', kiosk: 'kiosk' }

export default function Upload({ forcedProduct = null, embedded = false }) {
  const navigate = useNavigate()
  const [mainTab, setMainTab]         = useState('upload')    // 'upload' | 'history'
  const [product, setProduct]         = useState(forcedProduct)  // 'dmt' | 'aeps' | 'pg' | … (locked when forcedProduct set)
  const [dmtBanks, setDmtBanks]       = useState(FALLBACK_DMT_BANKS)
  const [step, setStep]               = useState(1)
  const [config, setConfig]           = useState({ partner: forcedProduct ? (PRODUCT_PARTNER[forcedProduct] || 'fino') : 'fino', side: 'bank', recon_date: today() })
  const [dateMode, setDateMode]       = useState('single')   // 'single' | 'auto'
  const [file, setFile]               = useState(null)
  const [uploading, setUploading]     = useState(false)
  const [sessionData, setSessionData] = useState(null)
  const [mapping, setMapping]         = useState({})
  const [saveTemplate, setSaveTemplate]   = useState(false)
  const [templateName, setTemplateName]   = useState('')
  const [confirming, setConfirming]   = useState(false)
  const [rowFilter, setRowFilter]     = useState({ column: '', value: '' })
  const [sourceColumn, setSourceColumn]   = useState('')   // for mixed dump auto-split
  const [ingestResult, setIngestResult]   = useState(null)
  const [multiUploading, setMultiUploading] = useState(false)
  const fileRef = useRef()
  const bankMultiRef = useRef()

  const isMixed = config.partner === 'mixed'

  // Upload several bank statements at once (e.g. Axis's 3 accounts). Each file is
  // parsed and auto-confirmed using its detected format preset; the source account
  // number is tagged automatically at ingest.
  const handleMultiUpload = async (files) => {
    const list = Array.from(files || [])
    if (!list.length) return
    if (!config.partner) return toast.error('Select a bank first')
    setMultiUploading(true)
    let ok = 0, fail = 0
    for (const f of list) {
      try {
        const fd = new FormData()
        fd.append('file', f)
        fd.append('partner', config.partner)
        fd.append('side', 'bank')
        fd.append('recon_date', dateMode === 'auto' ? 'auto' : config.recon_date)
        const { data } = await api.post('/upload/file', fd)
        const fd2 = new FormData()
        fd2.append('session_id', data.session_id)
        fd2.append('mapping', JSON.stringify(data.suggested_mapping || {}))
        fd2.append('save_template', false); fd2.append('template_name', '')
        fd2.append('filter_column', ''); fd2.append('filter_value', ''); fd2.append('source_column', '')
        await api.post('/upload/confirm-mapping', fd2)
        ok++
      } catch (err) {
        fail++
        toast.error(`${f.name}: ${err.response?.data?.detail || 'failed'}`, { duration: 6000 })
      }
    }
    setMultiUploading(false)
    if (ok) toast.success(`Uploaded ${ok} statement${ok > 1 ? 's' : ''}${fail ? ` · ${fail} failed` : ''}`)
    if (bankMultiRef.current) bankMultiRef.current.value = ''
  }

  function today() {
    return new Date().toISOString().split('T')[0]
  }

  useEffect(() => {
    api.get('/admin/partners-public?product=dmt').then(({ data }) => {
      const banks = data.filter(p => p.has_bank_statement && p.is_active)
      if (banks.length > 0) setDmtBanks(banks)
    }).catch(() => {}) // keep fallback on error
  }, [])

  const handleFileSelect = (e) => {
    const f = e.target.files[0]
    if (f) setFile(f)
  }

  const handleUpload = async () => {
    if (!file) return toast.error('Select a file first')
    setUploading(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('partner', config.partner)
      fd.append('side', config.side)
      fd.append('recon_date', dateMode === 'auto' ? 'auto' : config.recon_date)
      const { data } = await api.post('/upload/file', fd)
      setSessionData(data)
      setMapping(data.suggested_mapping || {})

      const cols = data.columns || []
      if (config.side === 'internal') {
        const srcCol = cols.find(c => c.toLowerCase() === 'source')
        if (isMixed) {
          // Mixed mode: set source column for auto-split, clear row filter
          if (srcCol) setSourceColumn(srcCol)
          setRowFilter({ column: '', value: '' })
        } else if (srcCol) {
          // Single-partner mode: only auto-filter if at least one preview row actually
          // contains the partner slug in the Source column.
          // AePS/PG dumps have Source = 'FINGPAY'/'PAYU' (not 'AEPS'/'PG'), so skip the
          // auto-filter for those products — their TYPENAME filter handles row selection instead.
          const partnerVal = config.partner.toUpperCase()
          const previewMatchesPartner = (data.preview || []).some(row => {
            const v = String(row[srcCol] || '').trim().toUpperCase()
            return v === partnerVal
          })
          if (previewMatchesPartner) {
            setRowFilter({ column: srcCol, value: partnerVal })
          } else {
            setRowFilter({ column: '', value: '' })
          }
          setSourceColumn('')
        }
      } else {
        setRowFilter({ column: '', value: '' })
        setSourceColumn('')
      }

      setStep(2)
      toast.success(`File parsed: ${data.row_count.toLocaleString()} rows`)
    } catch (err) {
      const detail = err.response?.data?.detail || 'Upload failed'
      // WLR and FREC are hard blocks — show as persistent error, not just toast
      if (detail.startsWith('[WLR]') || detail.startsWith('[FREC]')) {
        toast.error(detail, { duration: 8000, style: { maxWidth: 520, fontSize: '13px' } })
      } else {
        toast.error(detail)
      }
    } finally {
      setUploading(false)
    }
  }

  const handleConfirmMapping = async () => {
    setConfirming(true)
    try {
      const fd = new FormData()
      fd.append('session_id', sessionData.session_id)
      fd.append('mapping', JSON.stringify(mapping))
      fd.append('save_template', saveTemplate)
      fd.append('template_name', templateName)
      fd.append('filter_column', rowFilter.column || '')
      fd.append('filter_value', rowFilter.value || '')
      fd.append('source_column', sourceColumn || '')
      const { data } = await api.post('/upload/confirm-mapping', fd)
      setIngestResult(data)
      const partnerInfo = data.partner_counts
        ? Object.entries(data.partner_counts).map(([p, c]) => `${p}: ${c}`).join(', ')
        : ''
      toast.success(`Ingested ${data.row_count} txns${partnerInfo ? ` (${partnerInfo})` : ''}${data.skipped ? ` — ${data.skipped} skipped` : ''}`)
      setStep(3)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to confirm mapping')
    } finally {
      setConfirming(false)
    }
  }

  const reset = () => {
    setProduct(forcedProduct); setStep(1); setFile(null); setSessionData(null)
    setMapping({}); setRowFilter({ column: '', value: '' }); setIngestResult(null)
  }

  const canSubmit = () => {
    if (isMixed && !sourceColumn) return false  // mixed mode needs source column
    const m = mapping
    return m.eko_tid || m.tracking_number || m.description
  }

  return (
    <div>
      {!embedded && (<>
      <div className="flex items-center justify-between mb-1">
        <h1 className="text-xl font-bold text-gray-800">Upload Files</h1>
        <div className="flex gap-1 bg-gray-100 rounded-lg p-1">
          <button onClick={() => setMainTab('upload')}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-colors
              ${mainTab === 'upload' ? 'bg-white shadow-sm text-primary' : 'text-gray-500 hover:text-gray-700'}`}>
            <UploadIcon size={14} /> Upload
          </button>
          <button onClick={() => setMainTab('history')}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-colors
              ${mainTab === 'history' ? 'bg-white shadow-sm text-primary' : 'text-gray-500 hover:text-gray-700'}`}>
            <History size={14} /> History
          </button>
        </div>
      </div>
      <p className="text-sm text-gray-500 mb-6">Upload bank statements or internal dumps (CSV, Excel, PDF)</p>
      </>)}

      {mainTab === 'history' && <UploadHistoryPanel />}
      {mainTab === 'history' ? null : (<>

      {/* ── Product selector (shown before step 1 if no product chosen) ── */}
      {!product && step === 1 && (
        <div className="max-w-3xl">
          <h2 className="font-semibold text-gray-700 mb-1">Select Product</h2>
          <p className="text-sm text-gray-400 mb-5">Choose the product you want to upload data for.</p>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-4">
            {filterProductsByAccess(PRODUCTS).map(p => (
              <button
                key={p.id}
                onClick={() => {
                  if (p.route) { navigate(p.route); return }   // dedicated product → its own page
                  setProduct(p.id)
                  if (p.id === 'aeps')       setConfig(c => ({ ...c, partner: 'aeps',       side: 'bank', recon_date: today() }))
                  if (p.id === 'pg')         setConfig(c => ({ ...c, partner: 'pg',         side: 'bank', recon_date: today() }))
                  if (p.id === 'dmt')        setConfig(c => ({ ...c, partner: 'fino',       side: 'bank', recon_date: today() }))
                  if (p.id === 'qr')         setConfig(c => ({ ...c, partner: 'qr',         side: 'bank', recon_date: today() }))
                  if (p.id === 'digikhata')  setConfig(c => ({ ...c, partner: 'digikhata', side: 'bank', recon_date: today() }))
                  if (p.id === 'indonepal')  setConfig(c => ({ ...c, partner: 'indonepal', side: 'bank', recon_date: today() }))
                  if (p.id === 'kiosk')      setConfig(c => ({ ...c, partner: 'kiosk',     side: 'bank', recon_date: today() }))
                }}
                className={`rounded-xl border-2 p-5 text-left hover:shadow-md transition-all
                  ${p.color === 'blue'   ? 'border-blue-200   hover:border-blue-400   hover:bg-blue-50'   : ''}
                  ${p.color === 'green'  ? 'border-green-200  hover:border-green-400  hover:bg-green-50'  : ''}
                  ${p.color === 'purple' ? 'border-purple-200 hover:border-purple-400 hover:bg-purple-50' : ''}
                  ${p.color === 'cyan'   ? 'border-cyan-200   hover:border-cyan-400   hover:bg-cyan-50'   : ''}
                  ${p.color === 'rose'   ? 'border-rose-200   hover:border-rose-400   hover:bg-rose-50'   : ''}
                  ${p.color === 'orange' ? 'border-orange-200 hover:border-orange-400 hover:bg-orange-50' : ''}
                  ${p.color === 'teal'   ? 'border-teal-200   hover:border-teal-400   hover:bg-teal-50'   : ''}
                `}
              >
                <div className="text-3xl mb-3">{p.icon}</div>
                <div className="font-bold text-gray-800 text-base mb-1">{p.label}</div>
                <div className="text-xs text-gray-400">{p.desc}</div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Steps — shown once product is chosen */}
      {product && (
      <div className="flex items-center gap-2 mb-7">
        <button onClick={reset} className="text-xs text-gray-400 hover:text-primary mr-2 flex items-center gap-1">
          {embedded ? '↻ Start over' : `← ${PRODUCTS.find(p => p.id === product)?.label}`}
        </button>
        {['Configure', 'Map Columns', 'Done'].map((s, i) => (
          <React.Fragment key={i}>
            <div className={`flex items-center gap-1.5 text-sm font-medium px-3 py-1 rounded-full
              ${step === i+1 ? 'bg-primary text-white' : step > i+1 ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-400'}`}>
              {step > i+1 ? <CheckCircle size={14} /> : <span>{i+1}</span>}
              {s}
            </div>
            {i < 2 && <ChevronRight size={14} className="text-gray-300" />}
          </React.Fragment>
        ))}
      </div>
      )}

      {/* ── Step 1: Two-card layout (DMT) ────────────────────────── */}
      {product === 'dmt' && step === 1 && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-5 max-w-5xl">

          {/* ── Card 1: Bank Statement ─────────────────────────────── */}
          <div className={`rounded-xl border-2 p-5 transition-all ${config.side === 'bank' ? 'border-primary bg-primary/5' : 'border-gray-200 bg-white hover:border-gray-300'}`}>
            <div className="flex items-center gap-2.5 mb-4">
              <div className="p-2 bg-primary/10 rounded-lg"><Landmark size={18} className="text-primary" /></div>
              <div>
                <h2 className="font-bold text-gray-800">Bank Statement</h2>
                <p className="text-xs text-gray-400">Upload statement received from bank</p>
              </div>
            </div>

            <div className="space-y-3">
              <div>
                <label className="text-xs font-medium text-gray-500 block mb-1">Bank / Partner</label>
                <select className="select"
                  value={config.side === 'bank' ? config.partner : ''}
                  onChange={e => setConfig({ ...config, side: 'bank', partner: e.target.value })}>
                  <option value="" disabled>Select bank</option>
                  {dmtBanks.map(b => <option key={b.slug} value={b.slug}>{b.display_name}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 block mb-1">Recon Date</label>
                <div className="flex gap-2 mb-1.5">
                  {['single','auto'].map(m => (
                    <label key={m} className={`flex items-center gap-1.5 text-xs cursor-pointer px-2.5 py-1 rounded-lg border transition-all
                      ${dateMode === m && config.side === 'bank' ? 'border-primary bg-primary/5 text-primary font-medium' : 'border-gray-200 text-gray-500'}`}>
                      <input type="radio" name="bank_date_mode" value={m}
                        checked={dateMode === m && config.side === 'bank'}
                        onChange={() => { setConfig(c => ({...c, side: 'bank'})); setDateMode(m) }}
                        className="hidden" />
                      {m === 'single' ? 'Single date' : 'Auto-detect (multi-date)'}
                    </label>
                  ))}
                </div>
                {config.side === 'bank' && dateMode === 'single'
                  ? <input type="date" className="input"
                      value={config.recon_date}
                      onChange={e => setConfig({ ...config, side: 'bank', recon_date: e.target.value })} />
                  : <p className="text-xs text-primary bg-primary/5 rounded-lg px-3 py-2">
                      Date will be read from each row's Transaction Date column
                    </p>
                }
              </div>
              <div>
                <div
                  onClick={() => { setConfig(c => ({...c, side: 'bank'})); fileRef.current.click() }}
                  className="border-2 border-dashed border-gray-200 rounded-xl p-5 text-center cursor-pointer hover:border-primary/40 hover:bg-primary/5 transition-colors"
                >
                  {file && config.side === 'bank' ? (
                    <div className="flex items-center justify-center gap-2 text-primary">
                      <FileText size={16} /><span className="text-sm font-medium truncate max-w-xs">{file.name}</span>
                      <button onClick={e => { e.stopPropagation(); setFile(null) }} className="text-gray-400 hover:text-red-500"><X size={13} /></button>
                    </div>
                  ) : (
                    <div className="text-gray-400">
                      <UploadIcon size={20} className="mx-auto mb-1.5" />
                      <p className="text-xs">Click to select file</p>
                      <p className="text-xs text-gray-300 mt-0.5">CSV · Excel · PDF</p>
                    </div>
                  )}
                </div>
              </div>
              <div className="bg-amber-50 border border-amber-100 rounded-lg p-2.5 text-xs text-amber-700">
                FEE CHG rows and settlement credits stored separately. TID + Tracking auto-extracted from Description.
              </div>
              <button
                onClick={() => { setConfig(c => ({...c, side: 'bank'})); handleUpload() }}
                disabled={!file || config.side !== 'bank' || uploading || !config.partner || (dateMode === 'single' && !config.recon_date)}
                className="btn-primary w-full"
              >
                {uploading && config.side === 'bank' ? 'Parsing...' : 'Upload Bank Statement →'}
              </button>

              {/* Multi-account: upload several statements at once (e.g. Axis's 3 accounts) */}
              <input ref={bankMultiRef} type="file" accept=".csv,.xlsx,.xls,.pdf" multiple className="hidden"
                onChange={e => handleMultiUpload(e.target.files)} />
              <button
                onClick={() => { setConfig(c => ({...c, side: 'bank'})); bankMultiRef.current.click() }}
                disabled={!config.partner || multiUploading || (dateMode === 'single' && !config.recon_date)}
                className="w-full text-xs text-primary hover:underline disabled:opacity-40 disabled:no-underline mt-1">
                {multiUploading ? 'Uploading statements…' : '+ Upload multiple statements (this bank)'}
              </button>
            </div>
          </div>

          {/* ── Card 2: Simplibank Dump ────────────────────────────── */}
          <div className={`rounded-xl border-2 p-5 transition-all ${config.side === 'internal' ? 'border-green-500 bg-green-50/30' : 'border-gray-200 bg-white hover:border-gray-300'}`}>
            <div className="flex items-center gap-2.5 mb-4">
              <div className="p-2 bg-green-100 rounded-lg"><Building2 size={18} className="text-green-600" /></div>
              <div>
                <h2 className="font-bold text-gray-800">Simplibank Dump</h2>
                <p className="text-xs text-gray-400">Combined DMT dump — auto-splits by partner</p>
              </div>
            </div>

            <div className="space-y-3">
              {/* Always auto-split: no partner selector needed */}
              <div className="flex items-center gap-2 p-2.5 bg-green-50 border border-green-100 rounded-lg">
                <CheckCircle size={14} className="text-green-600 flex-shrink-0" />
                <p className="text-xs text-green-700">
                  Auto-splits by <code className="bg-green-100 px-1 rounded">Source</code> column → Fino, Airtel, Axis, Levin each go to their own recon. CR rows skipped. Recon runs automatically.
                </p>
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 block mb-1">Recon Date</label>
                <div className="flex gap-2 mb-1.5">
                  {['single','auto'].map(m => (
                    <label key={m} className={`flex items-center gap-1.5 text-xs cursor-pointer px-2.5 py-1 rounded-lg border transition-all
                      ${dateMode === m && config.side === 'internal' ? 'border-green-500 bg-green-50 text-green-700 font-medium' : 'border-gray-200 text-gray-500'}`}>
                      <input type="radio" name="internal_date_mode" value={m}
                        checked={dateMode === m && config.side === 'internal'}
                        onChange={() => { setConfig(c => ({...c, side: 'internal', partner: 'mixed'})); setDateMode(m) }}
                        className="hidden" />
                      {m === 'single' ? 'Single date' : 'Auto-detect (multi-date)'}
                    </label>
                  ))}
                </div>
                {config.side === 'internal' && dateMode === 'single'
                  ? <input type="date" className="input"
                      value={config.recon_date}
                      onChange={e => setConfig({ ...config, side: 'internal', partner: 'mixed', recon_date: e.target.value })} />
                  : <p className="text-xs text-green-700 bg-green-50 rounded-lg px-3 py-2">
                      Date will be read from each row's Transaction Date column
                    </p>
                }
              </div>
              <div>
                <div
                  onClick={() => { setConfig(c => ({...c, side: 'internal', partner: 'mixed'})); fileRef.current.click() }}
                  className="border-2 border-dashed border-gray-200 rounded-xl p-5 text-center cursor-pointer hover:border-green-400/50 hover:bg-green-50/30 transition-colors"
                >
                  {file && config.side === 'internal' ? (
                    <div className="flex items-center justify-center gap-2 text-green-600">
                      <FileText size={16} /><span className="text-sm font-medium truncate max-w-xs">{file.name}</span>
                      <button onClick={e => { e.stopPropagation(); setFile(null) }} className="text-gray-400 hover:text-red-500"><X size={13} /></button>
                    </div>
                  ) : (
                    <div className="text-gray-400">
                      <UploadIcon size={20} className="mx-auto mb-1.5" />
                      <p className="text-xs">Click to select file</p>
                      <p className="text-xs text-gray-300 mt-0.5">CSV · Excel · PDF</p>
                    </div>
                  )}
                </div>
              </div>
              <button
                onClick={() => { setConfig(c => ({...c, side: 'internal', partner: 'mixed'})); handleUpload() }}
                disabled={!file || config.side !== 'internal' || uploading || (dateMode === 'single' && !config.recon_date)}
                className="w-full py-2 px-4 rounded-lg font-medium text-sm bg-green-600 text-white hover:bg-green-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {uploading && config.side === 'internal' ? 'Parsing...' : 'Upload Simplibank Dump →'}
              </button>
            </div>
          </div>

          {/* Hidden file input shared between both cards */}
          <input ref={fileRef} type="file" accept=".csv,.xlsx,.xls,.pdf" className="hidden" onChange={handleFileSelect} />
        </div>
      )}

      {/* ── Step 1: AePS Cashout cards ───────────────────────────── */}
      {product === 'aeps' && step === 1 && (
        <div className="space-y-6 max-w-5xl">
          {/* Section 1: Transaction Recon */}
          <div>
            <div className="flex items-center gap-2 mb-3">
              <span className="text-xs font-bold text-green-700 bg-green-100 px-2.5 py-1 rounded-full uppercase tracking-wide">Transaction Recon</span>
              <span className="text-xs text-gray-400">Match AePS cashout transactions between Fingpay and Simplibank</span>
            </div>
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          {[
            { side: 'bank',     label: 'Fingpay AePS Report',    desc: 'cw-success-failure export from Fingpay', icon: '🏧', partner: 'aeps' },
            { side: 'internal', label: 'Simplibank AePS Dump',   desc: 'Combined dump — AePS Cashout rows auto-filtered', icon: '🗂️', partner: 'aeps' },
          ].map(card => (
            <div key={card.side}
              className={`rounded-xl border-2 p-5 transition-all ${config.side === card.side ? 'border-green-500 bg-green-50/30' : 'border-gray-200 bg-white hover:border-gray-300'}`}>
              <div className="flex items-center gap-2.5 mb-4">
                <div className="p-2 bg-green-100 rounded-lg text-xl">{card.icon}</div>
                <div>
                  <h2 className="font-bold text-gray-800">{card.label}</h2>
                  <p className="text-xs text-gray-400">{card.desc}</p>
                </div>
              </div>
              <div className="space-y-3">
                <div>
                  <label className="text-xs font-medium text-gray-500 block mb-1">Recon Date</label>
                  <div className="flex gap-2 mb-1.5">
                    {['single','auto'].map(m => (
                      <label key={m} className={`flex items-center gap-1.5 text-xs cursor-pointer px-2.5 py-1 rounded-lg border transition-all
                        ${dateMode === m && config.side === card.side ? 'border-green-500 bg-green-50 text-green-700 font-medium' : 'border-gray-200 text-gray-500'}`}>
                        <input type="radio" value={m}
                          checked={dateMode === m && config.side === card.side}
                          onChange={() => { setConfig(c => ({...c, side: card.side, partner: card.partner})); setDateMode(m) }}
                          className="hidden" />
                        {m === 'single' ? 'Single date' : 'Auto-detect'}
                      </label>
                    ))}
                  </div>
                  {config.side === card.side && dateMode === 'single'
                    ? <input type="date" className="input" value={config.recon_date}
                        onChange={e => setConfig(c => ({...c, side: card.side, partner: card.partner, recon_date: e.target.value}))} />
                    : <p className="text-xs text-green-700 bg-green-50 rounded-lg px-3 py-2">Date auto-detected from each row</p>
                  }
                </div>
                <div
                  onClick={() => { setConfig(c => ({...c, side: card.side, partner: card.partner})); fileRef.current.click() }}
                  className="border-2 border-dashed border-gray-200 rounded-xl p-5 text-center cursor-pointer hover:border-green-400 hover:bg-green-50/30 transition-colors">
                  {file && config.side === card.side
                    ? <div className="flex items-center justify-center gap-2 text-green-600">
                        <FileText size={16}/><span className="text-sm font-medium truncate max-w-xs">{file.name}</span>
                        <button onClick={e => { e.stopPropagation(); setFile(null) }} className="text-gray-400 hover:text-red-500"><X size={13}/></button>
                      </div>
                    : <div className="text-gray-400"><FileText size={20} className="mx-auto mb-1.5"/><p className="text-xs">Click to select file</p><p className="text-xs text-gray-300 mt-0.5">Excel · CSV · PDF</p></div>
                  }
                </div>
                <button
                  onClick={() => { setConfig(c => ({...c, side: card.side, partner: card.partner})); handleUpload() }}
                  disabled={!file || config.side !== card.side || uploading || (dateMode === 'single' && !config.recon_date)}
                  className="btn-primary w-full">
                  {uploading && config.side === card.side ? 'Parsing...' : `Upload ${card.label} →`}
                </button>
              </div>
            </div>
          ))}
          <input ref={fileRef} type="file" accept=".csv,.xlsx,.xls,.pdf" className="hidden" onChange={handleFileSelect} />
            </div>
          </div>

          {/* Section 2: Settlement Recon */}
          <div>
            <div className="flex items-center gap-2 mb-3">
              <span className="text-xs font-bold text-purple-700 bg-purple-100 px-2.5 py-1 rounded-full uppercase tracking-wide">Settlement Recon</span>
              <span className="text-xs text-gray-400">Upload Fingpay settlement reports to verify net payout</span>
            </div>
            <AepsSettlementUpload />
          </div>
        </div>
      )}

      {/* ── Step 1: Accept Payment (PG) cards ───────────────────── */}
      {product === 'pg' && step === 1 && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-5 max-w-5xl">
          {[
            { side: 'bank',     label: 'PayU Settlement Report', desc: 'settlementReport_*.xlsx from PayU dashboard', icon: '💳', partner: 'pg' },
            { side: 'internal', label: 'Simplibank PG Dump',     desc: 'Combined dump — Accept Payment rows auto-filtered', icon: '🗂️', partner: 'pg' },
          ].map(card => (
            <div key={card.side}
              className={`rounded-xl border-2 p-5 transition-all ${config.side === card.side ? 'border-purple-500 bg-purple-50/20' : 'border-gray-200 bg-white hover:border-gray-300'}`}>
              <div className="flex items-center gap-2.5 mb-4">
                <div className="p-2 bg-purple-100 rounded-lg text-xl">{card.icon}</div>
                <div>
                  <h2 className="font-bold text-gray-800">{card.label}</h2>
                  <p className="text-xs text-gray-400">{card.desc}</p>
                </div>
              </div>
              <div className="space-y-3">
                <div>
                  <label className="text-xs font-medium text-gray-500 block mb-1">Recon Date</label>
                  <div className="flex gap-2 mb-1.5">
                    {['single','auto'].map(m => (
                      <label key={m} className={`flex items-center gap-1.5 text-xs cursor-pointer px-2.5 py-1 rounded-lg border transition-all
                        ${dateMode === m && config.side === card.side ? 'border-purple-500 bg-purple-50 text-purple-700 font-medium' : 'border-gray-200 text-gray-500'}`}>
                        <input type="radio" value={m}
                          checked={dateMode === m && config.side === card.side}
                          onChange={() => { setConfig(c => ({...c, side: card.side, partner: card.partner})); setDateMode(m) }}
                          className="hidden" />
                        {m === 'single' ? 'Single date' : 'Auto-detect'}
                      </label>
                    ))}
                  </div>
                  {config.side === card.side && dateMode === 'single'
                    ? <input type="date" className="input" value={config.recon_date}
                        onChange={e => setConfig(c => ({...c, side: card.side, partner: card.partner, recon_date: e.target.value}))} />
                    : <p className="text-xs text-purple-700 bg-purple-50 rounded-lg px-3 py-2">Date auto-detected from each row</p>
                  }
                </div>
                <div
                  onClick={() => { setConfig(c => ({...c, side: card.side, partner: card.partner})); fileRef.current.click() }}
                  className="border-2 border-dashed border-gray-200 rounded-xl p-5 text-center cursor-pointer hover:border-purple-400 hover:bg-purple-50/20 transition-colors">
                  {file && config.side === card.side
                    ? <div className="flex items-center justify-center gap-2 text-purple-600">
                        <FileText size={16}/><span className="text-sm font-medium truncate max-w-xs">{file.name}</span>
                        <button onClick={e => { e.stopPropagation(); setFile(null) }} className="text-gray-400 hover:text-red-500"><X size={13}/></button>
                      </div>
                    : <div className="text-gray-400"><FileText size={20} className="mx-auto mb-1.5"/><p className="text-xs">Click to select file</p><p className="text-xs text-gray-300 mt-0.5">Excel · CSV · PDF</p></div>
                  }
                </div>
                {card.side === 'bank' && (
                  <div className="bg-purple-50 border border-purple-100 rounded-lg p-2.5 text-xs text-purple-700">
                    Net Amount (post-fee) stored automatically for PG Settlement Summary.
                  </div>
                )}
                <button
                  onClick={() => { setConfig(c => ({...c, side: card.side, partner: card.partner})); handleUpload() }}
                  disabled={!file || config.side !== card.side || uploading || (dateMode === 'single' && !config.recon_date)}
                  className="btn-primary w-full" style={{background: config.side === card.side ? '#7c3aed' : undefined}}>
                  {uploading && config.side === card.side ? 'Parsing...' : `Upload ${card.label} →`}
                </button>
              </div>
            </div>
          ))}
          <input ref={fileRef} type="file" accept=".csv,.xlsx,.xls,.pdf" className="hidden" onChange={handleFileSelect} />
        </div>
      )}

      {/* ── Step 1: QR Collection cards ──────────────────────────── */}
      {product === 'qr' && step === 1 && (
        <div className="space-y-6 max-w-5xl">
          {/* Section 1: Transaction Recon */}
          <div>
            <div className="flex items-center gap-2 mb-3">
              <span className="text-xs font-bold text-cyan-700 bg-cyan-100 px-2.5 py-1 rounded-full uppercase tracking-wide">Transaction Recon</span>
              <span className="text-xs text-gray-400">Match QR transactions between Paypoint report and Simplibank dump</span>
            </div>
            <GenericProductCards
              product="qr" accentColor="cyan"
              cards={[
                { side: 'bank',     partner: 'qr',    label: 'QR Transaction Report', desc: 'ExportTransactionList_*.xlsx from Paypoint — OrderId + RRN for matching', icon: '📱' },
                { side: 'internal', partner: 'mixed',  label: 'Simplibank Combined Dump', desc: 'Combined dump — QR Collection rows auto-filtered by TYPENAME', icon: '🗂️' },
              ]}
              config={config} setConfig={setConfig} dateMode={dateMode} setDateMode={setDateMode}
              file={file} setFile={setFile} fileRef={fileRef} uploading={uploading}
              handleUpload={handleUpload} onFileSelect={handleFileSelect}
              hints={{
                bank:     'Failed rows auto-set aside. Credit card rows stored with net amount (fees deducted).',
                internal: 'All QR Collection rows ingested — TYPENAME filter applied automatically.',
              }}
            />
          </div>

          {/* Section 2: Settlement Recon */}
          <div>
            <div className="flex items-center gap-2 mb-3">
              <span className="text-xs font-bold text-indigo-700 bg-indigo-100 px-2.5 py-1 rounded-full uppercase tracking-wide">Settlement Recon</span>
              <span className="text-xs text-gray-400">Upload Paypoint settlement report to verify net payout</span>
            </div>
            <QRSettlementUpload />
          </div>
        </div>
      )}

      {/* ── Step 1: Digikhata (PPI Wallet Load) cards ───────────── */}
      {product === 'digikhata' && step === 1 && (
        <GenericProductCards
          product="digikhata"
          accentColor="orange"
          cards={[
            { side: 'bank',     partner: 'digikhata', label: 'Digikhata Bank Report',   desc: 'Transaction + eKYC Charge rows — Eko TID in "Txn Chain Ref Id"', icon: '👛' },
            { side: 'internal', partner: 'mixed',      label: 'Simplibank Combined Dump', desc: 'Combined dump — Digi Khata Load Wallet rows auto-filtered',     icon: '🗂️' },
          ]}
          config={config} setConfig={setConfig} dateMode={dateMode} setDateMode={setDateMode}
          file={file} setFile={setFile} fileRef={fileRef} uploading={uploading}
          handleUpload={handleUpload} onFileSelect={handleFileSelect}
          hints={{
            bank:     'All rows ingested — normal transactions + eKYC Charges (₹10 each) classified separately',
            internal: 'All sub-entries per TID ingested: main load, CCF, GST, Commission, TDS',
          }}
        />
      )}

      {/* ── Step 1: Indonepal cards ──────────────────────────────── */}
      {product === 'indonepal' && step === 1 && (
        <GenericProductCards
          product="indonepal"
          accentColor="teal"
          cards={[
            { side: 'bank',     partner: 'indonepal', label: 'Indo-Nepal Bank Report',   desc: 'Transaction Report — Eko TID in "PartnerPinNo" column', icon: '🌏' },
            { side: 'internal', partner: 'mixed',     label: 'Simplibank Combined Dump', desc: 'Combined dump — Indo-Nepal Money Transfer rows auto-filtered', icon: '🗂️' },
          ]}
          config={config} setConfig={setConfig} dateMode={dateMode} setDateMode={setDateMode}
          file={file} setFile={setFile} fileRef={fileRef} uploading={uploading}
          handleUpload={handleUpload} onFileSelect={handleFileSelect}
          hints={{
            bank:     'Success rows matched on Eko TID. Total/summary row automatically skipped.',
            internal: 'Success = DR only. Failed = DR + CR reversal pair — both ingested for visibility.',
          }}
        />
      )}

      {/* ── Step 1: SBI Kiosk cards ──────────────────────────────── */}
      {product === 'kiosk' && step === 1 && (
        <SBIKioskUploads />
      )}

      {/* ── Step 2: Map Columns ───────────────────────────────────── */}
      {step === 2 && sessionData && (
        <div className="card max-w-2xl">
          <div className="mb-4">
            <h2 className="font-semibold text-gray-700">Map Columns</h2>
            <p className="text-xs text-gray-400 mt-0.5">
              {sessionData.row_count.toLocaleString()} rows in <span className="font-medium">{sessionData.filename}</span>
            </p>
          </div>

          {/* Re-upload warning banner */}
          {sessionData.existing_data?.count > 0 && (
            <div className="flex items-start gap-2.5 bg-orange-50 border border-orange-200 rounded-lg px-4 py-3 mb-4">
              <AlertTriangle size={16} className="text-orange-500 flex-shrink-0 mt-0.5" />
              <div className="flex-1">
                <p className="text-sm font-semibold text-orange-800">Duplicate upload detected</p>
                <p className="text-xs text-orange-700 mt-0.5">
                  <strong>{sessionData.existing_data.count.toLocaleString()} transactions</strong> already exist for{' '}
                  <strong className="capitalize">{sessionData.existing_data.partner}</strong> /{' '}
                  <strong>{sessionData.existing_data.side}</strong> /{' '}
                  <strong>{sessionData.existing_data.date}</strong>.
                  Proceeding will create duplicates. Clear existing data first if you want a clean re-import.
                </p>
              </div>
            </div>
          )}

          {/* PDF info banner */}
          {sessionData.filename?.toLowerCase().endsWith('.pdf') && !sessionData.format_detected && (
            <div className="flex items-start gap-2.5 bg-blue-50 border border-blue-200 rounded-lg px-4 py-3 mb-5">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-blue-500 flex-shrink-0 mt-0.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
              <div>
                <p className="text-sm font-semibold text-blue-800">PDF parsed — please verify column mapping below</p>
                <p className="text-xs text-blue-700 mt-0.5">
                  Text-based PDFs extract correctly. If columns look empty or garbled, the PDF may be scanned — export as Excel or CSV instead.
                </p>
              </div>
            </div>
          )}

          {/* Format auto-detected banner */}
          {sessionData.format_detected && (
            <div className="flex items-center gap-2.5 bg-green-50 border border-green-200 rounded-lg px-4 py-3 mb-5">
              <CheckCircle size={16} className="text-green-500 flex-shrink-0" />
              <div>
                <span className="text-sm font-semibold text-green-800">Format auto-detected: </span>
                <span className="text-sm text-green-700">{sessionData.format_detected}</span>
                <p className="text-xs text-green-600 mt-0.5">
                  Columns pre-filled. Amount &amp; DR/CR will be auto-detected from Debit/Credit columns — no manual mapping needed.
                </p>
              </div>
            </div>
          )}

          {/* Mixed Dump: Source Column selector */}
          {isMixed && (
            <div className="bg-green-50 border border-green-100 rounded-lg p-3 mb-5">
              <div className="flex items-center gap-2 mb-2">
                <Filter size={13} className="text-green-600" />
                <span className="text-xs font-semibold text-green-700">Auto-Split by Source Column</span>
              </div>
              <div className="flex gap-2 items-center">
                <label className="text-xs text-green-700 flex-shrink-0 w-28">Source Column:</label>
                <select
                  className="select text-xs py-1.5 flex-1"
                  value={sourceColumn}
                  onChange={e => setSourceColumn(e.target.value)}
                >
                  <option value="">— Select column —</option>
                  {sessionData.columns.map(col => (
                    <option key={col} value={col}>{col}</option>
                  ))}
                </select>
                {sourceColumn && (
                  <span className="text-xs bg-green-100 text-green-700 px-2 py-1 rounded font-medium flex-shrink-0">Active</span>
                )}
              </div>
              <p className="text-xs text-green-600 mt-1.5">
                FINO rows → Fino recon &nbsp;·&nbsp; AIRTEL rows → Airtel recon &nbsp;·&nbsp; Others skipped &nbsp;·&nbsp; CR rows auto-skipped
              </p>
            </div>
          )}

          {/* Row Filter (single-partner mode) */}
          {!isMixed && (
          <div className="bg-gray-50 rounded-lg p-3 mb-5 border border-gray-100">
            <div className="flex items-center gap-2 mb-2">
              <Filter size={13} className="text-gray-400" />
              <span className="text-xs font-semibold text-gray-600">Row Filter</span>
              <span className="text-xs text-gray-400">(optional — filter to specific rows only)</span>
            </div>
            <div className="flex gap-2 items-center">
              <select
                className="select text-xs py-1.5 flex-1"
                value={rowFilter.column}
                onChange={e => setRowFilter({ ...rowFilter, column: e.target.value })}
              >
                <option value="">— No filter —</option>
                {sessionData.columns.map(col => (
                  <option key={col} value={col}>{col}</option>
                ))}
              </select>
              <span className="text-xs text-gray-400 flex-shrink-0">=</span>
              <input
                className="input text-xs py-1.5 flex-1"
                placeholder="value (e.g. FINO)"
                value={rowFilter.value}
                onChange={e => setRowFilter({ ...rowFilter, value: e.target.value })}
                disabled={!rowFilter.column}
              />
              {rowFilter.column && rowFilter.value && (
                <span className="text-xs bg-blue-100 text-blue-700 px-2 py-1 rounded font-medium flex-shrink-0">Active</span>
              )}
            </div>
            {config.side === 'internal' && rowFilter.column && (
              <p className="text-xs text-gray-400 mt-1.5">
                + CR rows (reversals/refunds) will be auto-skipped
              </p>
            )}
            {config.side === 'bank' && (
              <p className="text-xs text-gray-400 mt-1.5">
                + FEE CHG rows and RTGS credits will be stored separately
              </p>
            )}
          </div>
          )}

          {/* Column Mapping */}
          <div className="space-y-3 mb-5">
            {STANDARD_FIELDS.map(field => (
              <div key={field.key} className="flex items-start gap-3">
                <div className="w-52 flex-shrink-0 pt-1.5">
                  <div className="text-sm text-gray-600">{field.label}</div>
                  {field.hint && !mapping[field.key] && (
                    <div className="text-xs text-gray-400 mt-0.5">{field.hint}</div>
                  )}
                </div>
                <select
                  className="select flex-1"
                  value={mapping[field.key] || ''}
                  onChange={e => {
                    const val = e.target.value
                    const updated = { ...mapping }
                    if (val) updated[field.key] = val
                    else delete updated[field.key]
                    setMapping(updated)
                  }}
                >
                  <option value="">— Skip —</option>
                  {sessionData.columns.map(col => (
                    <option key={col} value={col}>{col}</option>
                  ))}
                </select>
                {mapping[field.key] && <CheckCircle size={16} className="text-green-500 flex-shrink-0 mt-2" />}
              </div>
            ))}
          </div>

          {/* Preview */}
          <div className="mb-5">
            <p className="text-xs font-medium text-gray-500 mb-2">File Preview (first 5 rows)</p>
            <div className="overflow-x-auto rounded-lg border border-gray-100">
              <table className="text-xs w-full">
                <thead className="bg-gray-50">
                  <tr>{sessionData.columns.map(c => (
                    <th key={c} className={`px-2 py-1 text-left font-medium whitespace-nowrap
                      ${Object.values(mapping).includes(c) ? 'text-primary bg-primary/5' : 'text-gray-500'}`}>{c}</th>
                  ))}</tr>
                </thead>
                <tbody>
                  {sessionData.preview.map((row, i) => (
                    <tr key={i} className="border-t border-gray-50">
                      {sessionData.columns.map(c => (
                        <td key={c} className={`px-2 py-1 whitespace-nowrap max-w-36 truncate
                          ${Object.values(mapping).includes(c) ? 'text-gray-800 font-medium' : 'text-gray-400'}`}>
                          {row[c]}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Save template */}
          <div className="border-t border-gray-100 pt-4 mb-4">
            <label className="flex items-center gap-2 text-sm text-gray-600 cursor-pointer">
              <input type="checkbox" checked={saveTemplate} onChange={e => setSaveTemplate(e.target.checked)} />
              Save this mapping as template for {config.partner} {config.side} files
            </label>
            {saveTemplate && (
              <input className="input mt-2" placeholder={`e.g. ${config.partner} ${config.side} default`}
                value={templateName} onChange={e => setTemplateName(e.target.value)} />
            )}
          </div>

          <div className="flex gap-3">
            <button onClick={() => setStep(1)} className="btn-ghost">Back</button>
            <button
              onClick={handleConfirmMapping}
              disabled={confirming || !canSubmit()}
              className="btn-primary flex-1"
            >
              {confirming ? 'Ingesting...' : 'Confirm & Ingest'}
            </button>
          </div>
          {!canSubmit() && (
            <p className="text-xs text-amber-600 mt-2 text-center">
              Map at least one of: Eko TID, Tracking Number, or Description
            </p>
          )}
        </div>
      )}

      {/* ── Step 3: Done ──────────────────────────────────────────── */}
      {step === 3 && (
        <div className="card max-w-md text-center py-10">
          <CheckCircle size={48} className="text-green-500 mx-auto mb-4" />
          <h2 className="text-lg font-semibold text-gray-800 mb-2">Upload Successful</h2>
          {ingestResult && (
            <div className="flex justify-center gap-4 mb-4 flex-wrap">
              <div className="text-center">
                <div className="text-2xl font-bold text-gray-800">{ingestResult.row_count}</div>
                <div className="text-xs text-gray-500">
                  {(PRODUCTS.find(p => p.id === product)?.label ?? 'DMT') + ' Transactions'}
                </div>
              </div>
              {ingestResult.fee_charge_count > 0 && (
                <div className="text-center">
                  <div className="text-2xl font-bold text-blue-500">{ingestResult.fee_charge_count}</div>
                  <div className="text-xs text-gray-500">Fee / Charges</div>
                </div>
              )}
              {ingestResult.settlement_credit_count > 0 && (
                <div className="text-center">
                  <div className="text-2xl font-bold text-purple-500">{ingestResult.settlement_credit_count}</div>
                  <div className="text-xs text-gray-500">Settlement Credits</div>
                </div>
              )}
              {ingestResult.reversal_count > 0 && (
                <div className="text-center">
                  <div className="text-2xl font-bold text-orange-500">{ingestResult.reversal_count}</div>
                  <div className="text-xs text-gray-500">Reversals</div>
                </div>
              )}
              {ingestResult.skipped > 0 && (
                <div className="text-center">
                  <div className="text-2xl font-bold text-amber-500">{ingestResult.skipped}</div>
                  <div className="text-xs text-gray-500">Filtered out</div>
                </div>
              )}
            </div>
          )}
          {ingestResult?.reversal_auto && Object.keys(ingestResult.reversal_auto).length > 0 && (
            <div className="bg-orange-50 border border-orange-200 rounded-lg px-4 py-3 mb-4 text-xs text-orange-800">
              <strong>Reversals paired:</strong>{' '}
              {Object.entries(ingestResult.reversal_auto).map(([key, r]) => (
                <span key={key} className="mr-3">
                  <span className="font-semibold capitalize">{key}</span>
                  {' — '}<span className="font-semibold">{r.reversal_matched}</span> reversal(s) closed
                </span>
              ))}
            </div>
          )}
          {ingestResult?.partner_counts && Object.keys(ingestResult.partner_counts).length > 0 && (
            <div className="bg-green-50 border border-green-100 rounded-lg px-4 py-3 mb-4 text-xs text-green-700">
              <strong>Split:</strong>{' '}
              {Object.entries(ingestResult.partner_counts).map(([p, c]) => (
                <span key={p} className="font-semibold capitalize mr-2">{p}: {c} txns</span>
              ))}
            </div>
          )}
          {ingestResult?.auto_recon && Object.keys(ingestResult.auto_recon).length > 0 && (
            <div className="mb-4 space-y-2">
              {Object.entries(ingestResult.auto_recon).map(([key, r]) => {
                const rate = r.match_rate ?? 0
                const isAlert = r.alert || rate < 85
                return (
                  <div key={key} className={`rounded-lg px-4 py-3 text-xs border flex items-start gap-3
                    ${isAlert ? 'bg-red-50 border-red-200 text-red-800' : 'bg-green-50 border-green-200 text-green-800'}`}>
                    <span className="text-base mt-0.5">{isAlert ? '⚠️' : '✅'}</span>
                    <div className="flex-1">
                      <div className="font-semibold mb-0.5 capitalize">
                        {key} — Auto Recon Complete
                        {isAlert && <span className="ml-2 font-bold text-red-600">LOW MATCH RATE — review open items</span>}
                      </div>
                      <div className="flex gap-4 mt-1">
                        <span>✓ Matched: <strong>{r.matched}</strong></span>
                        {r.unmatched_bank > 0 && <span>⊘ Bank open: <strong className="text-red-600">{r.unmatched_bank}</strong></span>}
                        {r.unmatched_internal > 0 && <span>⊘ Internal open: <strong className="text-orange-600">{r.unmatched_internal}</strong></span>}
                        <span className={`font-bold ${isAlert ? 'text-red-600' : 'text-green-700'}`}>
                          Match rate: {rate}%
                        </span>
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
          {ingestResult?.fee_charge_count > 0 && (
            <p className="text-xs text-blue-600 bg-blue-50 rounded-lg px-3 py-2 mb-4">
              Fee/charge rows are stored separately — export them from Reports.
            </p>
          )}

          {/* AS=SD Integrity Warnings — non-blocking, shown after ingestion */}
          {ingestResult?.integrity_warnings?.length > 0 && (
            <div className="mb-4 space-y-2">
              {ingestResult.integrity_warnings.map((w, i) => (
                <div key={i} className={`rounded-lg px-4 py-3 text-xs border flex items-start gap-2
                  ${w.status === 'pending_tplus' || w.status === 'pending_cw'
                    ? 'bg-amber-50 border-amber-200 text-amber-800'
                    : 'bg-red-50 border-red-200 text-red-800'
                  }`}>
                  <span className="text-base mt-0.5">
                    {(w.status === 'pending_tplus' || w.status === 'pending_cw') ? '⏳' : '⚠️'}
                  </span>
                  <div>
                    <div className="font-semibold mb-0.5">
                      {w.check} {w.partner ? `(${w.partner.toUpperCase()})` : ''}
                      {w.status === 'pending_tplus' ? ' — Pending T Plus' :
                       w.status === 'pending_cw'    ? ' — Pending Fingpay cw file' : ' — Check Failed'}
                    </div>
                    <div>{w.message}</div>
                    {w.difference != null && !w.passed && (
                      <div className="mt-1 font-mono">
                        AS: ₹{Number(w.sum_net_amount || w.cw_amount || 0).toLocaleString('en-IN', {minimumFractionDigits:2})}
                        {' '} SD: ₹{Number(w.sum_computed_sd || w.tplus_amount || 0).toLocaleString('en-IN', {minimumFractionDigits:2})}
                        {' '} Diff: ₹{Number(w.difference || 0).toFixed(2)}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Data-quality profile (roadmap 1.6) — non-blocking, display-only */}
          {ingestResult?.data_quality?.has_warnings && (
            <div className="mb-4 bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 text-xs text-amber-800 flex items-start gap-2">
              <span className="text-base mt-0.5">⚠️</span>
              <div>
                <div className="font-semibold mb-0.5">Data-quality notice — informational only, nothing was blocked</div>
                <ul className="list-disc ml-4 space-y-0.5">
                  {ingestResult.data_quality.warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
                <div className="text-amber-600 mt-1">Full profile is on the Ingestion Monitor.</div>
              </div>
            </div>
          )}

          {/* SEV notice when settlement bank not configured */}
          {ingestResult?.sev_notice && (
            <div className="mb-4 bg-indigo-50 border border-indigo-100 rounded-lg px-4 py-3 text-xs text-indigo-700 flex items-start gap-2">
              <span className="text-base">🏦</span>
              <div>
                <div className="font-semibold mb-0.5">SEV Skipped</div>
                <div>{ingestResult.sev_notice}</div>
                <a href={`${import.meta.env.BASE_URL}admin`} className="underline mt-1 inline-block">Configure in Settings →</a>
              </div>
            </div>
          )}

          <p className="text-sm text-gray-500 mb-6">Transactions are ready for reconciliation.</p>
          <div className="flex gap-3 justify-center">
            <button onClick={reset} className="btn-ghost">Upload Another</button>
            <a href={`${import.meta.env.BASE_URL}run-recon`} className="btn-primary">Run Recon →</a>
          </div>
        </div>
      )}
      </>)}  {/* end mainTab !== 'history' conditional */}
    </div>
  )
}


// ── Upload History sub-panel ──────────────────────────────────────────────────
function UploadHistoryPanel() {
  const [items, setItems]         = useState([])
  const [total, setTotal]         = useState(0)
  const [loading, setLoading]     = useState(false)
  const [partner, setPartner]     = useState('')
  const [side, setSide]           = useState('')
  const [partnerList, setPartnerList] = useState([])

  useEffect(() => {
    api.get('/admin/partners-public')
      .then(({ data }) => { if (data?.length) setPartnerList(data) })
      .catch(() => {})
  }, [])

  const load = async () => {
    setLoading(true)
    try {
      const params = {}
      if (partner) params.partner = partner
      if (side)    params.side    = side
      const { data } = await api.get('/upload/history', { params })
      setItems(data.items); setTotal(data.total)
    } catch { toast.error('Failed to load history') }
    finally { setLoading(false) }
  }

  useEffect(() => { load() }, [partner, side])

  return (
    <div>
      <div className="card mb-4">
        <div className="flex gap-3 flex-wrap">
          <div>
            <label className="text-xs text-gray-400 block mb-1">Partner</label>
            <select className="select" value={partner} onChange={e => setPartner(e.target.value)}>
              <option value="">All</option>
              {partnerList.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}
              {/* "mixed" is not a real partner slug — transactions are stored under their actual partner.
                  Only show it if the backend actually has records under 'mixed' (shouldn't happen). */}
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">Side</label>
            <select className="select" value={side} onChange={e => setSide(e.target.value)}>
              <option value="">All</option>
              <option value="bank">Bank</option>
              <option value="internal">Internal</option>
            </select>
          </div>
        </div>
      </div>
      <div className="card p-0 overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-100 text-sm text-gray-500">{total} uploads recorded</div>
        <table className="w-full text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="table-th">File</th>
              <th className="table-th">Partner</th>
              <th className="table-th">Side</th>
              <th className="table-th">Recon Date</th>
              <th className="table-th text-right">Rows</th>
              <th className="table-th">Uploaded By</th>
              <th className="table-th">Uploaded At</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={7} className="text-center py-8 text-gray-400">Loading…</td></tr>
            ) : items.length === 0 ? (
              <tr><td colSpan={7} className="text-center py-8 text-gray-400">No uploads yet</td></tr>
            ) : items.map(h => (
              <tr key={h.id} className="border-b border-gray-50 hover:bg-gray-50">
                <td className="table-td font-mono text-xs text-gray-600 max-w-xs truncate">{h.filename}</td>
                <td className="table-td capitalize">{h.partner}</td>
                <td className="table-td">
                  <span className={h.side === 'bank' ? 'badge-blue' : 'badge-gray'}>{h.side}</span>
                </td>
                <td className="table-td font-mono text-xs">{h.recon_date}</td>
                <td className="table-td text-right tabular-nums">{h.rows_inserted?.toLocaleString()}</td>
                <td className="table-td text-gray-500">{h.username}</td>
                <td className="table-td font-mono text-xs text-gray-400">{h.uploaded_at?.slice(0, 16).replace('T', ' ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
