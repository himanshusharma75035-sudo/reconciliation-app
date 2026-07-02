import React, { useState, useEffect } from 'react'
import { Clock, Plus, Play, Trash2, Pencil, Power, X, Mail, CheckCircle2, AlertCircle } from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'

const REPORT_TYPES = [
  { value: 'eod',           label: 'EOD Summary'   },
  { value: 'summary',       label: 'Recon Summary' },
  { value: 'open_items',    label: 'Open Items'    },
  { value: 'matched_pairs', label: 'Matched Pairs' },
  { value: 'ageing',        label: 'Ageing'        },
  { value: 'src',           label: 'SRC Report'    },
  { value: 'evalue_summary',    label: 'E-Value Summary'    },
  { value: 'evalue_exceptions', label: 'E-Value Exceptions' },
  { value: 'evalue_unmatched',  label: 'E-Value Unmatched'  },
  { value: 'bbps_summary',      label: 'BBPS Summary'       },
  { value: 'bbps_exceptions',   label: 'BBPS Exceptions'    },
  { value: 'bbps_unmatched',    label: 'BBPS Unmatched'     },
  { value: 'funds_position',    label: 'Funds Position (EOD balances)' },
]

const FREQUENCIES = [
  { value: 'daily',   label: 'Daily'   },
  { value: 'weekly',  label: 'Weekly'  },
  { value: 'monthly', label: 'Monthly' },
]

const DATE_RANGES = [
  { value: 'today',       label: 'Today'        },
  { value: 'yesterday',   label: 'Yesterday'    },
  { value: 'last_7_days', label: 'Last 7 Days'  },
  { value: 'this_week',   label: 'This Week'    },
  { value: 'this_month',  label: 'This Month'   },
  { value: 'last_month',  label: 'Last Month'   },
]

const DOW = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

const labelOf = (arr, v) => arr.find(x => x.value === v)?.label ?? v

const blankForm = () => ({
  id:           null,
  name:         '',
  report_type:  'eod',
  partner:      '',
  side:         '',
  recon_status: '',
  date_range:   'yesterday',
  frequency:    'daily',
  hour:         8,
  minute:       0,
  day_of_week:  0,
  day_of_month: 1,
  email_to:     '',
})

export default function ScheduledReports({ partners = [] }) {
  const [subs, setSubs]       = useState([])
  const [loading, setLoading] = useState(false)
  const [showForm, setShow]   = useState(false)
  const [form, setForm]       = useState(blankForm())
  const [saving, setSaving]   = useState(false)
  const [busyId, setBusyId]   = useState(null)

  const isAdmin = (() => {
    try { return JSON.parse(localStorage.getItem('user') || '{}').role === 'admin' } catch { return false }
  })()

  const load = async () => {
    setLoading(true)
    try {
      const { data } = await api.get('/report-subscriptions')
      setSubs(data || [])
    } catch { toast.error('Failed to load scheduled reports') }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const upd = patch => setForm(f => ({ ...f, ...patch }))

  const openNew  = () => { setForm(blankForm()); setShow(true) }
  const openEdit = s => {
    const f = s.filters || {}
    setForm({
      id: s.id, name: s.name, report_type: s.report_type,
      partner: f.partner || '', side: f.side || '', recon_status: f.recon_status || '',
      date_range: s.date_range, frequency: s.frequency,
      hour: s.hour, minute: s.minute,
      day_of_week: s.day_of_week ?? 0, day_of_month: s.day_of_month ?? 1,
      email_to: s.email_to || '',
    })
    setShow(true)
  }

  const save = async () => {
    if (!form.name.trim()) return toast.error('Give the report a name')
    const filters = {}
    if (form.partner)      filters.partner      = form.partner
    if (form.side)         filters.side         = form.side
    if (form.recon_status) filters.recon_status = form.recon_status

    const body = {
      name: form.name.trim(),
      report_type: form.report_type,
      filters,
      date_range: form.date_range,
      frequency: form.frequency,
      hour: Number(form.hour),
      minute: Number(form.minute),
      day_of_week:  form.frequency === 'weekly'  ? Number(form.day_of_week)  : null,
      day_of_month: form.frequency === 'monthly' ? Number(form.day_of_month) : null,
      email_to: form.email_to.trim() || null,
    }
    setSaving(true)
    try {
      if (form.id) await api.put(`/report-subscriptions/${form.id}`, body)
      else         await api.post('/report-subscriptions', body)
      toast.success(form.id ? 'Schedule updated' : 'Schedule created')
      setShow(false); load()
    } catch (e) { toast.error(e.response?.data?.detail || 'Save failed') }
    finally { setSaving(false) }
  }

  const toggle = async s => {
    setBusyId(s.id)
    try { await api.patch(`/report-subscriptions/${s.id}/toggle`); load() }
    catch { toast.error('Toggle failed') }
    finally { setBusyId(null) }
  }

  const runNow = async s => {
    setBusyId(s.id)
    try {
      const { data } = await api.post(`/report-subscriptions/${s.id}/run-now`)
      const st = data?.detail?.status
      if (st === 'success')                 toast.success('Report generated and emailed')
      else if (st === 'skipped_no_smtp')    toast('Generated — email skipped (SMTP not configured)', { icon: 'ℹ️' })
      else                                  toast(`Ran: ${st || 'done'}`)
      load()
    } catch (e) { toast.error(e.response?.data?.detail || 'Run failed') }
    finally { setBusyId(null) }
  }

  const remove = async s => {
    if (!confirm(`Delete scheduled report "${s.name}"?`)) return
    setBusyId(s.id)
    try { await api.delete(`/report-subscriptions/${s.id}`); toast.success('Deleted'); load() }
    catch { toast.error('Delete failed') }
    finally { setBusyId(null) }
  }

  const scheduleText = s => {
    const t = `${String(s.hour).padStart(2, '0')}:${String(s.minute).padStart(2, '0')}`
    if (s.frequency === 'daily')   return `Daily at ${t} IST`
    if (s.frequency === 'weekly')  return `Every ${DOW[s.day_of_week ?? 0]} at ${t} IST`
    if (s.frequency === 'monthly') return `Day ${s.day_of_month ?? 1} of month at ${t} IST`
    return t
  }

  return (
    <div className="max-w-4xl">
      <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
        <div>
          <h2 className="font-semibold text-gray-700 flex items-center gap-2">
            <Clock size={17} /> My Scheduled Reports
          </h2>
          <p className="text-xs text-gray-400 mt-0.5">
            Set up reports to be generated and emailed to you automatically — daily, weekly or monthly.
            {isAdmin && ' As admin you can see everyone’s schedules.'}
          </p>
        </div>
        <button onClick={openNew} className="btn-primary flex items-center gap-1.5">
          <Plus size={15} /> New Schedule
        </button>
      </div>

      {loading ? (
        <p className="text-sm text-gray-400 text-center py-10">Loading…</p>
      ) : subs.length === 0 ? (
        <div className="card text-center py-10">
          <Clock size={28} className="mx-auto text-gray-300 mb-2" />
          <p className="text-sm text-gray-500">No scheduled reports yet.</p>
          <p className="text-xs text-gray-400 mt-1">Click <strong>New Schedule</strong> to set up your first automated report.</p>
        </div>
      ) : (
        <div className="space-y-2.5">
          {subs.map(s => (
            <div key={s.id} className={`card flex items-start justify-between gap-4 ${!s.is_active ? 'opacity-60' : ''}`}>
              <div className="min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-semibold text-gray-800 text-sm">{s.name}</span>
                  <span className="text-[11px] px-2 py-0.5 rounded-full bg-blue-50 text-blue-600 font-medium">
                    {labelOf(REPORT_TYPES, s.report_type)}
                  </span>
                  {!s.is_active && (
                    <span className="text-[11px] px-2 py-0.5 rounded-full bg-gray-100 text-gray-500 font-medium">Paused</span>
                  )}
                  {isAdmin && s.username && (
                    <span className="text-[11px] text-gray-400">· {s.username}</span>
                  )}
                </div>
                <div className="text-xs text-gray-500 mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
                  <span className="flex items-center gap-1"><Clock size={12} /> {scheduleText(s)}</span>
                  <span>Window: <strong className="text-gray-600">{labelOf(DATE_RANGES, s.date_range)}</strong></span>
                  {s.filters?.partner && <span>Partner: <strong className="text-gray-600 capitalize">{s.filters.partner}</strong></span>}
                  {s.email_to && <span className="flex items-center gap-1"><Mail size={12} /> {s.email_to}</span>}
                </div>
                {s.last_run_at && (
                  <div className="text-[11px] mt-1.5 flex items-center gap-1">
                    {s.last_run_status === 'success'
                      ? <CheckCircle2 size={12} className="text-green-500" />
                      : <AlertCircle size={12} className="text-amber-500" />}
                    <span className="text-gray-400">
                      Last run {new Date(s.last_run_at + 'Z').toLocaleString('en-IN')} — {s.last_run_message || s.last_run_status}
                    </span>
                  </div>
                )}
              </div>

              <div className="flex items-center gap-1 shrink-0">
                <button title="Run now" disabled={busyId === s.id} onClick={() => runNow(s)}
                  className="p-2 rounded-lg hover:bg-green-50 text-green-600 disabled:opacity-40"><Play size={15} /></button>
                <button title={s.is_active ? 'Pause' : 'Resume'} disabled={busyId === s.id} onClick={() => toggle(s)}
                  className={`p-2 rounded-lg hover:bg-gray-100 disabled:opacity-40 ${s.is_active ? 'text-gray-500' : 'text-amber-600'}`}><Power size={15} /></button>
                <button title="Edit" onClick={() => openEdit(s)}
                  className="p-2 rounded-lg hover:bg-blue-50 text-blue-600"><Pencil size={15} /></button>
                <button title="Delete" disabled={busyId === s.id} onClick={() => remove(s)}
                  className="p-2 rounded-lg hover:bg-red-50 text-red-500 disabled:opacity-40"><Trash2 size={15} /></button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* ── Create / Edit modal ── */}
      {showForm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={() => setShow(false)}>
          <div className="bg-white rounded-xl shadow-xl w-full max-w-lg max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-5 py-3.5 border-b border-gray-100 sticky top-0 bg-white">
              <h3 className="font-semibold text-gray-800">{form.id ? 'Edit' : 'New'} Scheduled Report</h3>
              <button onClick={() => setShow(false)} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
            </div>

            <div className="p-5 space-y-4">
              <div>
                <label className="text-xs text-gray-500 block mb-1">Name</label>
                <input className="input" placeholder="e.g. Fino EOD — Daily 8 AM"
                  value={form.name} onChange={e => upd({ name: e.target.value })} />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Report Type</label>
                  <select className="select" value={form.report_type} onChange={e => upd({ report_type: e.target.value })}>
                    {REPORT_TYPES.map(r => <option key={r.value} value={r.value}>{r.label}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Date Window</label>
                  <select className="select" value={form.date_range} onChange={e => upd({ date_range: e.target.value })}>
                    {DATE_RANGES.map(d => <option key={d.value} value={d.value}>{d.label}</option>)}
                  </select>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Partner (optional)</label>
                  <select className="select" value={form.partner} onChange={e => upd({ partner: e.target.value })}>
                    <option value="">All partners</option>
                    {partners.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Side (optional)</label>
                  <select className="select" value={form.side} onChange={e => upd({ side: e.target.value })}>
                    <option value="">Both</option>
                    <option value="bank">Bank</option>
                    <option value="internal">Internal</option>
                  </select>
                </div>
              </div>

              <div className="border-t border-gray-100 pt-4">
                <label className="text-xs font-semibold text-gray-600 block mb-2">Schedule</label>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="text-xs text-gray-500 block mb-1">Frequency</label>
                    <select className="select" value={form.frequency} onChange={e => upd({ frequency: e.target.value })}>
                      {FREQUENCIES.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}
                    </select>
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    <div>
                      <label className="text-xs text-gray-500 block mb-1">Hour</label>
                      <input type="number" min="0" max="23" className="input"
                        value={form.hour} onChange={e => upd({ hour: e.target.value })} />
                    </div>
                    <div>
                      <label className="text-xs text-gray-500 block mb-1">Minute</label>
                      <input type="number" min="0" max="59" className="input"
                        value={form.minute} onChange={e => upd({ minute: e.target.value })} />
                    </div>
                  </div>
                </div>

                {form.frequency === 'weekly' && (
                  <div className="mt-3">
                    <label className="text-xs text-gray-500 block mb-1">Day of Week</label>
                    <select className="select" value={form.day_of_week} onChange={e => upd({ day_of_week: e.target.value })}>
                      {DOW.map((d, i) => <option key={i} value={i}>{d}</option>)}
                    </select>
                  </div>
                )}
                {form.frequency === 'monthly' && (
                  <div className="mt-3">
                    <label className="text-xs text-gray-500 block mb-1">Day of Month (1–31)</label>
                    <input type="number" min="1" max="31" className="input"
                      value={form.day_of_month} onChange={e => upd({ day_of_month: e.target.value })} />
                    <p className="text-[11px] text-gray-400 mt-1">Months without this day are skipped (e.g. day 31 in Feb).</p>
                  </div>
                )}
                <p className="text-[11px] text-gray-400 mt-2">All times are Asia/Kolkata (IST).</p>
              </div>

              <div className="border-t border-gray-100 pt-4">
                <label className="text-xs text-gray-500 block mb-1">Email To</label>
                <input className="input" placeholder="Leave blank to use your account email — comma-separate for multiple"
                  value={form.email_to} onChange={e => upd({ email_to: e.target.value })} />
              </div>
            </div>

            <div className="flex justify-end gap-2 px-5 py-3.5 border-t border-gray-100 sticky bottom-0 bg-white">
              <button onClick={() => setShow(false)} className="btn-ghost">Cancel</button>
              <button onClick={save} disabled={saving} className="btn-primary">
                {saving ? 'Saving…' : form.id ? 'Update' : 'Create Schedule'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
