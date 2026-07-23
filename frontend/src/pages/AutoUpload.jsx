import React, { useEffect, useState, useCallback } from 'react'
import { DMT_PARTNERS } from '../productRegistry'
import axios from '../utils/api'
import {
  FolderOpen, Play, Clock, CheckCircle, XCircle, AlertCircle,
  Save, RefreshCw, ToggleLeft, ToggleRight, ChevronDown, ChevronUp, Info
} from 'lucide-react'
import toast from 'react-hot-toast'

const DATE_FORMAT_OPTIONS = [
  { value: 'YYYYMMDD',   label: 'YYYYMMDD   (e.g. 20260506)' },
  { value: 'YYYY-MM-DD', label: 'YYYY-MM-DD (e.g. 2026-05-06)' },
  { value: 'DDMMYYYY',   label: 'DDMMYYYY   (e.g. 06052026)' },
  { value: 'DD-MM-YYYY', label: 'DD-MM-YYYY (e.g. 06-05-2026)' },
  { value: 'DD/MM/YYYY', label: 'DD/MM/YYYY (e.g. 06/05/2026)' },
]

const PARTNER_COLORS = {
  fino:       { bg: 'bg-blue-50',    border: 'border-blue-200',   badge: 'badge-blue',   dot: 'bg-blue-500'   },
  airtel:     { bg: 'bg-red-50',     border: 'border-red-200',    badge: 'badge-red',    dot: 'bg-red-500'    },
  axis:       { bg: 'bg-indigo-50',  border: 'border-indigo-200', badge: 'badge-blue',   dot: 'bg-indigo-500' },
  levin:      { bg: 'bg-teal-50',    border: 'border-teal-200',   badge: 'badge-gray',   dot: 'bg-teal-500'   },
  aeps:       { bg: 'bg-green-50',   border: 'border-green-200',  badge: 'badge-green',  dot: 'bg-green-500'  },
  pg:         { bg: 'bg-purple-50',  border: 'border-purple-200', badge: 'badge-purple', dot: 'bg-purple-500' },
  qr:         { bg: 'bg-cyan-50',    border: 'border-cyan-200',   badge: 'badge-blue',   dot: 'bg-cyan-500'   },
  digikhata:  { bg: 'bg-orange-50',  border: 'border-orange-200', badge: 'badge-orange', dot: 'bg-orange-500' },
  indonepal:  { bg: 'bg-yellow-50',  border: 'border-yellow-200', badge: 'badge-yellow', dot: 'bg-yellow-500' },
  sbi:        { bg: 'bg-rose-50',    border: 'border-rose-200',   badge: 'badge-red',    dot: 'bg-rose-500'   },
}

const SIDE_LABELS = { bank: 'Bank Statement', internal: 'Internal Dump' }

function statusBadge(status) {
  if (!status) return <span className="badge-gray">Never run</span>
  if (status === 'success')   return <span className="badge-green flex items-center gap-1"><CheckCircle size={11}/>Success</span>
  if (status === 'not_found') return <span className="badge-yellow flex items-center gap-1"><AlertCircle size={11}/>File not found</span>
  return <span className="badge-red flex items-center gap-1"><XCircle size={11}/>Error</span>
}

function todayFilename(prefix, suffix, dateFormat) {
  const now = new Date()
  const y = now.getFullYear()
  const m = String(now.getMonth() + 1).padStart(2, '0')
  const d = String(now.getDate()).padStart(2, '0')
  const map = {
    'YYYYMMDD':   `${y}${m}${d}`,
    'YYYY-MM-DD': `${y}-${m}-${d}`,
    'DDMMYYYY':   `${d}${m}${y}`,
    'DD-MM-YYYY': `${d}-${m}-${y}`,
    'DD/MM/YYYY': `${d}/${m}/${y}`,
  }
  const dateStr = map[dateFormat] || `${y}${m}${d}`
  return `${prefix || ''}${dateStr}${suffix || '.xlsx'}`
}

function ConfigCard({ config: initialConfig, onRefresh }) {
  const [config, setConfig]         = useState(initialConfig)
  const [form, setForm]             = useState({
    folder_path:     initialConfig.folder_path || '',
    file_prefix:     initialConfig.file_prefix || '',
    file_suffix:     initialConfig.file_suffix || '.xlsx',
    date_format:     initialConfig.date_format || 'YYYYMMDD',
    recon_date_mode: initialConfig.recon_date_mode || 'auto',
    auto_recon:      initialConfig.auto_recon !== false,
  })
  const [schedForm, setSchedForm]   = useState({
    hour:   initialConfig.schedule?.hour ?? 8,
    minute: initialConfig.schedule?.minute ?? 0,
  })
  const [saving, setSaving]         = useState(false)
  const [triggering, setTriggering] = useState(false)
  const [testing, setTesting]       = useState(false)
  const [testResult, setTestResult] = useState(null)
  const [triggerResult, setTriggerResult] = useState(null)
  const [schedOpen, setSchedOpen]   = useState(false)

  const colors = PARTNER_COLORS[config.partner] || PARTNER_COLORS.fino

  const handleSave = async () => {
    setSaving(true)
    try {
      const { data } = await axios.put(`/auto-upload/configs/${config.id}`, form)
      setConfig(data)
      toast.success('Settings saved')
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async () => {
    // Save first, then test
    setSaving(true)
    try {
      await axios.put(`/auto-upload/configs/${config.id}`, form)
    } catch {}
    setSaving(false)
    setTesting(true)
    setTestResult(null)
    try {
      const { data } = await axios.get(`/auto-upload/configs/${config.id}/test`)
      setTestResult(data)
    } catch (e) {
      toast.error('Test failed')
    } finally {
      setTesting(false)
    }
  }

  const handleTrigger = async () => {
    setTriggering(true)
    setTriggerResult(null)
    const attempt = force => axios.post(`/auto-upload/trigger/${config.id}${force ? '?force=true' : ''}`)
    try {
      let res
      try { res = await attempt(false) }
      catch (e) {
        const msg = e.response?.data?.detail || 'Upload failed'
        // 409 [DUPLICATE] → Re-upload (replace): the module handler re-applies the file
        // with replace/upsert semantics — rows it already covers are superseded.
        if (e.response?.status === 409 &&
            window.confirm(`${msg}\n\nRe-trigger anyway and re-apply the file? Rows it already covers are replaced, not duplicated.`)) {
          res = await attempt(true)
        } else {
          setTriggerResult({ ok: false, msg })
          toast.error(msg)
          return
        }
      }
      const data = res.data
      setTriggerResult({ ok: true, data })
      toast.success(`Ingested ${data.row_count} rows from ${data.filename}`)
      onRefresh()
    } catch (e) {
      const msg = e.response?.data?.detail || 'Upload failed'
      setTriggerResult({ ok: false, msg })
      toast.error(msg)
    } finally {
      setTriggering(false)
    }
  }

  const handleSaveSchedule = async () => {
    setSaving(true)
    try {
      await axios.put(`/auto-upload/schedules/${config.id}`, {
        hour:       Number(schedForm.hour),
        minute:     Number(schedForm.minute),
        is_enabled: config.schedule?.is_enabled ?? false,
      })
      toast.success('Schedule saved')
      onRefresh()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const handleToggleSchedule = async () => {
    try {
      const { data } = await axios.post(`/auto-upload/schedules/${config.id}/toggle`)
      setConfig(prev => ({ ...prev, schedule: { ...prev.schedule, is_enabled: data.is_enabled } }))
      toast.success(data.is_enabled ? 'Schedule enabled' : 'Schedule disabled')
      onRefresh()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Toggle failed')
    }
  }

  const schedEnabled = config.schedule?.is_enabled

  return (
    <div className={`card border ${colors.border} ${colors.bg}`}>
      {/* Header */}
      <div className="flex items-start justify-between mb-4">
        <div className="flex items-center gap-2">
          <span className={`w-2.5 h-2.5 rounded-full ${colors.dot} flex-shrink-0 mt-0.5`}/>
          <div>
            <div className="font-semibold text-gray-800 text-sm">{config.label}</div>
            <div className="text-xs text-gray-500 mt-0.5">
              {config.partner.toUpperCase()} · {SIDE_LABELS[config.side]}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {statusBadge(config.last_trigger_status)}
          {config.last_triggered_at && (
            <span className="text-xs text-gray-400">{new Date(config.last_triggered_at).toLocaleString('en-IN')}</span>
          )}
        </div>
      </div>

      {/* Folder path */}
      <div className="mb-3">
        <label className="block text-xs font-semibold text-gray-600 mb-1 flex items-center gap-1">
          <FolderOpen size={12}/> Folder Path <span className="text-gray-400 font-normal">(on server machine)</span>
        </label>
        <input
          className="input text-xs font-mono"
          placeholder={`/home/user/recon/uploads/${config.partner}_${config.side}`}
          value={form.folder_path}
          onChange={e => setForm(p => ({ ...p, folder_path: e.target.value }))}
        />
      </div>

      {/* Filename pattern */}
      <div className="mb-3">
        <label className="block text-xs font-semibold text-gray-600 mb-1">File Naming Pattern</label>
        <div className="flex gap-2 items-center">
          <input
            className="input text-xs"
            placeholder="Prefix e.g. FINO_BANK_"
            value={form.file_prefix}
            onChange={e => setForm(p => ({ ...p, file_prefix: e.target.value }))}
            style={{ flex: 2 }}
          />
          <select
            className="select text-xs"
            value={form.date_format}
            onChange={e => setForm(p => ({ ...p, date_format: e.target.value }))}
            style={{ flex: 3 }}
          >
            {DATE_FORMAT_OPTIONS.map(o => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
          <select
            className="select text-xs"
            value={form.file_suffix}
            onChange={e => setForm(p => ({ ...p, file_suffix: e.target.value }))}
            style={{ flex: 1 }}
          >
            <option value=".xlsx">.xlsx</option>
            <option value=".xls">.xls</option>
            <option value=".csv">.csv</option>
            <option value=".pdf">.pdf</option>
          </select>
        </div>
        <div className="mt-1.5 flex items-center gap-1 text-xs text-gray-500">
          <Info size={11}/>
          Today's file:&nbsp;
          <span className="font-mono font-medium text-gray-700">
            {todayFilename(form.file_prefix, form.file_suffix, form.date_format)}
          </span>
        </div>
        <p className="mt-1 text-xs text-gray-400 pl-4">
          If the configured format is not found, .xlsx / .csv / .pdf are also checked automatically.
        </p>
      </div>

      {/* Options row */}
      <div className="flex gap-4 mb-4 text-xs">
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={form.auto_recon}
            onChange={e => setForm(p => ({ ...p, auto_recon: e.target.checked }))}
            className="rounded"
          />
          <span className="text-gray-600">Auto-run recon after upload</span>
        </label>
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={form.recon_date_mode === 'auto'}
            onChange={e => setForm(p => ({ ...p, recon_date_mode: e.target.checked ? 'auto' : 'fixed' }))}
            className="rounded"
          />
          <span className="text-gray-600">Auto-detect date from rows</span>
        </label>
      </div>

      {/* Test result */}
      {testResult && (
        <div className={`text-xs rounded-lg px-3 py-2 mb-3 ${testResult.file_exists ? 'bg-green-50 border border-green-200 text-green-800' : testResult.folder_exists ? 'bg-yellow-50 border border-yellow-200 text-yellow-800' : 'bg-red-50 border border-red-200 text-red-800'}`}>
          {testResult.folder_exists
            ? testResult.file_exists
              ? <><CheckCircle size={11} className="inline mr-1"/>Folder found · File <strong>{testResult.expected_filename}</strong> is present and ready</>
              : <><AlertCircle size={11} className="inline mr-1"/>Folder found · File <strong>{testResult.expected_filename}</strong> not yet in folder</>
            : <><XCircle size={11} className="inline mr-1"/>Folder not found: <code>{testResult.folder_path}</code></>
          }
        </div>
      )}

      {/* Trigger result */}
      {triggerResult && (
        <div className={`text-xs rounded-lg px-3 py-2 mb-3 ${triggerResult.ok ? 'bg-green-50 border border-green-200 text-green-800' : 'bg-red-50 border border-red-200 text-red-800'}`}>
          {triggerResult.ok
            ? <><CheckCircle size={11} className="inline mr-1"/>Uploaded <strong>{triggerResult.data.row_count}</strong> rows from <strong>{triggerResult.data.filename}</strong>{triggerResult.data.auto_recon && Object.keys(triggerResult.data.auto_recon).length > 0 ? ` · Recon ran: ${Object.values(triggerResult.data.auto_recon).reduce((s, r) => s + r.matched, 0)} matched` : ''}</>
            : <><XCircle size={11} className="inline mr-1"/>{triggerResult.msg}</>
          }
        </div>
      )}

      {/* Action buttons */}
      <div className="flex gap-2 mb-3">
        <button className="btn-ghost text-xs flex items-center gap-1" onClick={handleSave} disabled={saving}>
          <Save size={13}/>{saving ? 'Saving…' : 'Save Settings'}
        </button>
        <button className="btn-ghost text-xs flex items-center gap-1" onClick={handleTest} disabled={testing || saving}>
          <RefreshCw size={13} className={testing ? 'animate-spin' : ''}/>{testing ? 'Testing…' : 'Test Path'}
        </button>
        <button className="btn-primary text-xs flex items-center gap-1 ml-auto" onClick={handleTrigger} disabled={triggering}>
          <Play size={13}/>{triggering ? 'Uploading…' : 'Upload Now'}
        </button>
      </div>

      {/* Schedule section */}
      <div className="border-t border-gray-200 pt-3">
        <button
          className="flex items-center justify-between w-full text-xs font-semibold text-gray-600 mb-2"
          onClick={() => setSchedOpen(p => !p)}
        >
          <span className="flex items-center gap-1.5">
            <Clock size={12}/>
            Cron Schedule
            {schedEnabled
              ? <span className="badge-green ml-1">Active · {String(config.schedule?.hour ?? 8).padStart(2,'0')}:{String(config.schedule?.minute ?? 0).padStart(2,'0')} IST</span>
              : <span className="badge-gray ml-1">Off</span>
            }
          </span>
          {schedOpen ? <ChevronUp size={13}/> : <ChevronDown size={13}/>}
        </button>

        {schedOpen && (
          <div className="space-y-2">
            <div className="flex gap-2 items-center">
              <div>
                <label className="block text-xs text-gray-500 mb-0.5">Hour (0–23)</label>
                <input
                  type="number" min={0} max={23}
                  className="input text-xs w-20"
                  value={schedForm.hour}
                  onChange={e => setSchedForm(p => ({ ...p, hour: e.target.value }))}
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-0.5">Minute (0–59)</label>
                <input
                  type="number" min={0} max={59}
                  className="input text-xs w-20"
                  value={schedForm.minute}
                  onChange={e => setSchedForm(p => ({ ...p, minute: e.target.value }))}
                />
              </div>
              <div className="mt-4">
                <button className="btn-ghost text-xs flex items-center gap-1" onClick={handleSaveSchedule}>
                  <Save size={12}/>Save Time
                </button>
              </div>
              <div className="mt-4 ml-auto">
                <button
                  onClick={handleToggleSchedule}
                  className={`flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg font-medium transition-colors ${schedEnabled ? 'bg-green-100 text-green-700 hover:bg-green-200' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'}`}
                >
                  {schedEnabled ? <ToggleRight size={15}/> : <ToggleLeft size={15}/>}
                  {schedEnabled ? 'Enabled' : 'Disabled'}
                </button>
              </div>
            </div>
            {config.schedule?.last_run_at && (
              <div className="text-xs text-gray-500">
                Last cron run: {new Date(config.schedule.last_run_at).toLocaleString('en-IN')} —{' '}
                {statusBadge(config.schedule.last_run_status)}
                {config.schedule.last_run_message && (
                  <span className="ml-1 text-gray-400">{config.schedule.last_run_message}</span>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Product grouping definition ───────────────────────────────────────────────
// Maps each partner slug to a product group for section rendering.
// Add new partners here when onboarded — they'll auto-appear in their section.
const PRODUCT_GROUPS = [
  {
    key:      'dmt',
    label:    'DMT',
    desc:     'Domestic Money Transfer — Fino, Airtel, Axis, Levin',
    icon:     '💸',
    color:    { badge: 'bg-blue-100 text-blue-700',   border: 'border-blue-200' },
    partners: DMT_PARTNERS,
  },
  {
    key:      'aeps',
    label:    'AePS Cashout',
    desc:     'Fingpay transaction report + Simplibank dump',
    icon:     '🏧',
    color:    { badge: 'bg-green-100 text-green-700',  border: 'border-green-200' },
    partners: ['aeps'],
  },
  {
    key:      'pg',
    label:    'Accept Payment (PG)',
    desc:     'PayU settlement report + Simplibank dump',
    icon:     '💳',
    color:    { badge: 'bg-purple-100 text-purple-700', border: 'border-purple-200' },
    partners: ['pg'],
  },
  {
    key:      'qr',
    label:    'QR Collection',
    desc:     'Paypoint transaction report + Simplibank dump',
    icon:     '📱',
    color:    { badge: 'bg-cyan-100 text-cyan-700',    border: 'border-cyan-200' },
    partners: ['qr'],
  },
  {
    key:      'digikhata',
    label:    'Digikhata (PPI)',
    desc:     'Digikhata bank report + Simplibank dump',
    icon:     '👛',
    color:    { badge: 'bg-orange-100 text-orange-700', border: 'border-orange-200' },
    partners: ['digikhata'],
  },
  {
    key:      'indonepal',
    label:    'Indo-Nepal',
    desc:     'Indo-Nepal bank report + Simplibank dump',
    icon:     '🌏',
    color:    { badge: 'bg-teal-100 text-teal-700',    border: 'border-teal-200' },
    partners: ['indonepal'],
  },
  {
    key:      'sbi',
    label:    'SBI Kiosk',
    desc:     'P01 Bank Statement · P02 Transaction Reports · P03 CSP Master · P04 Cash Holding & Limit Failures',
    icon:     '🏧',
    color:    { badge: 'bg-rose-100 text-rose-700',    border: 'border-rose-200' },
    partners: ['sbi'],
  },
  {
    key:      'evalue',
    label:    'E-Value',
    desc:     'Simplibank E-Value load-in dump (bank statements are uploaded per-account in the E-Value window)',
    icon:     '👛',
    color:    { badge: 'bg-cyan-100 text-cyan-700',    border: 'border-cyan-200' },
    partners: ['evalue'],
  },
  {
    key:      'bbps',
    label:    'BBPS',
    desc:     'Simplibank BBPS dump + Moneyart / Levin operator statement (provider auto-detected)',
    icon:     '🧾',
    color:    { badge: 'bg-purple-100 text-purple-700', border: 'border-purple-200' },
    partners: ['bbps'],
  },
]

export default function AutoUpload() {
  const [configs, setConfigs] = useState([])
  const [loading, setLoading] = useState(true)
  const [activeProduct, setActiveProduct] = useState(null) // null = show all / overview

  const loadConfigs = useCallback(async () => {
    try {
      const { data } = await axios.get('/auto-upload/configs')
      setConfigs(data)
    } catch (e) {
      toast.error('Failed to load auto-upload configs')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { loadConfigs() }, [loadConfigs])

  // Group configs by product
  const grouped = PRODUCT_GROUPS.map(grp => ({
    ...grp,
    configs: configs.filter(c => grp.partners.includes(c.partner)),
  })).filter(grp => grp.configs.length > 0)

  // Any configs not mapped to a product group (future partners)
  const knownPartners = PRODUCT_GROUPS.flatMap(g => g.partners)
  const ungrouped = configs.filter(c => !knownPartners.includes(c.partner))

  // Status summary per group
  const groupStatus = (grp) => {
    const cfgs = grp.configs
    const active = cfgs.filter(c => c.last_trigger_status === 'success').length
    const errors = cfgs.filter(c => c.last_trigger_status && c.last_trigger_status !== 'success' && c.last_trigger_status !== 'not_found').length
    return { active, errors, total: cfgs.length }
  }

  const selectedGroup = activeProduct ? grouped.find(g => g.key === activeProduct) : null

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-xl font-bold text-gray-800">Auto Upload</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            Configure watched folders and schedules per product. Drop today's file in the folder and click Upload Now, or let the cron job run automatically.
          </p>
        </div>
        <button className="btn-ghost text-xs flex items-center gap-1" onClick={loadConfigs}>
          <RefreshCw size={13}/>Refresh
        </button>
      </div>

      {/* Info banner */}
      <div className="bg-blue-50 border border-blue-200 rounded-xl px-4 py-3 mb-5 text-xs text-blue-800 flex items-start gap-2">
        <Info size={14} className="flex-shrink-0 mt-0.5"/>
        <div>
          <strong>How it works:</strong> Set the folder path where you'll drop files, define the naming pattern (prefix + date format + suffix), and save.
          The app looks for <code className="bg-blue-100 px-1 rounded">prefix + today's date + suffix</code> in that folder.
          Use <strong>Upload Now</strong> for a one-click pickup, or enable a <strong>Cron Schedule</strong> for fully automatic daily ingestion.
        </div>
      </div>

      {loading ? (
        <div className="text-sm text-gray-400 py-10 text-center">Loading…</div>
      ) : (
        <>
          {/* Product selector tabs — same pattern as Upload Files */}
          <div className="flex gap-0 mb-6 border-b border-gray-200 overflow-x-auto">
            <button
              onClick={() => setActiveProduct(null)}
              className={`px-5 py-3 text-sm font-medium border-b-2 transition-colors whitespace-nowrap flex items-center gap-2
                ${!activeProduct ? 'border-primary text-primary' : 'border-transparent text-gray-500 hover:text-gray-700'}`}>
              🗂️ All Products
              <span className="text-xs bg-gray-100 text-gray-500 px-1.5 py-0.5 rounded-full">{configs.length}</span>
            </button>
            {grouped.map(grp => {
              const st = groupStatus(grp)
              return (
                <button key={grp.key}
                  onClick={() => setActiveProduct(grp.key)}
                  className={`px-5 py-3 text-sm font-medium border-b-2 transition-colors whitespace-nowrap flex items-center gap-2
                    ${activeProduct === grp.key ? 'border-primary text-primary' : 'border-transparent text-gray-500 hover:text-gray-700'}`}>
                  {grp.icon} {grp.label}
                  {st.errors > 0 && (
                    <span className="text-xs bg-red-100 text-red-600 px-1.5 py-0.5 rounded-full">{st.errors} err</span>
                  )}
                </button>
              )
            })}
          </div>

          {/* All Products overview — product cards with summary */}
          {!activeProduct && (
            <div className="space-y-6">
              {grouped.map(grp => {
                const st = groupStatus(grp)
                return (
                  <div key={grp.key}>
                    {/* Product section header */}
                    <div className="flex items-center justify-between mb-3">
                      <div className="flex items-center gap-3">
                        <span className="text-xl">{grp.icon}</span>
                        <div>
                          <span className={`text-xs font-bold px-2.5 py-1 rounded-full ${grp.color.badge}`}>
                            {grp.label}
                          </span>
                          <span className="text-xs text-gray-400 ml-2">{grp.desc}</span>
                        </div>
                      </div>
                      <button
                        onClick={() => setActiveProduct(grp.key)}
                        className="text-xs text-primary hover:underline font-medium">
                        Configure all →
                      </button>
                    </div>

                    {/* Cards for this product */}
                    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                      {grp.configs.map(cfg => (
                        <ConfigCard key={cfg.id} config={cfg} onRefresh={loadConfigs} />
                      ))}
                    </div>
                  </div>
                )
              })}

              {/* Ungrouped (future partners not yet in PRODUCT_GROUPS) */}
              {ungrouped.length > 0 && (
                <div>
                  <div className="flex items-center gap-2 mb-3">
                    <span className="text-xs font-bold bg-gray-100 text-gray-600 px-2.5 py-1 rounded-full">Other</span>
                  </div>
                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                    {ungrouped.map(cfg => (
                      <ConfigCard key={cfg.id} config={cfg} onRefresh={loadConfigs} />
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Single product view */}
          {activeProduct && selectedGroup && (
            <div>
              <div className="flex items-center gap-3 mb-5">
                <button onClick={() => setActiveProduct(null)} className="text-xs text-gray-400 hover:text-primary">
                  ← All Products
                </button>
                <span className="text-gray-300">/</span>
                <span className={`text-xs font-bold px-2.5 py-1 rounded-full ${selectedGroup.color.badge}`}>
                  {selectedGroup.icon} {selectedGroup.label}
                </span>
                <span className="text-xs text-gray-400">{selectedGroup.desc}</span>
              </div>
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
                {selectedGroup.configs.map(cfg => (
                  <ConfigCard key={cfg.id} config={cfg} onRefresh={loadConfigs} />
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
