import React, { useState, useEffect } from 'react'
import {
  Settings, Plus, Edit2, Trash2, ToggleLeft, ToggleRight,
  Save, X, ChevronUp, ChevronDown, RefreshCw, CheckCircle, AlertTriangle
} from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'
import { CORE_PRODUCTS, isCoreLedgerPartner, MATCH_FIELDS } from '../productRegistry'

const TABS = [
  { key: 'partners',        label: 'Partners & Banks' },
  { key: 'presets',         label: 'Format Presets' },
  { key: 'rules',           label: 'Matching Rules' },
  { key: 'settlement-banks', label: '🏦 Settlement Banks' },
  { key: 'bank-accounts',   label: '🏛️ Bank Accounts' },
  { key: 'evalue-accounts', label: '👛 E-Value Accounts' },
  { key: 'api-keys',        label: '🔑 API Keys' },
  { key: 'ai',              label: '🤖 AI' },
]

// Derived from the product registry (single source of truth) so a new core product
// auto-appears here instead of being hard-coded — was ['dmt','aeps','pg'], which
// silently dropped qr / digikhata / indonepal.
const PRODUCTS = CORE_PRODUCTS.map(p => p.id)
const SIDES    = ['bank', 'internal']
const MATCH_FIELD_OPTIONS = MATCH_FIELDS   // single source (productRegistry) — was a divergent copy

// ── Helpers ──────────────────────────────────────────────────────────────────
function Badge({ label, color = 'gray' }) {
  const colors = {
    gray:   'bg-gray-100 text-gray-600',
    blue:   'bg-blue-100 text-blue-700',
    green:  'bg-green-100 text-green-700',
    purple: 'bg-purple-100 text-purple-700',
    orange: 'bg-orange-100 text-orange-700',
  }
  return <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${colors[color] || colors.gray}`}>{label}</span>
}

function productColor(p) {
  return p === 'dmt' ? 'blue' : p === 'aeps' ? 'green' : 'purple'
}

// ────────────────────────────────────────────────────────────────────────────
// Partners Tab
// ────────────────────────────────────────────────────────────────────────────
function PartnersTab() {
  const [partners, setPartners] = useState([])
  const [loading, setLoading]   = useState(true)
  const [editing, setEditing]   = useState(null)  // null | 'new' | partner object
  const [form, setForm]         = useState({})

  const load = async () => {
    try {
      const { data } = await api.get('/admin/partners')
      setPartners(data)
    } catch { toast.error('Failed to load partners') }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const blankForm = {
    slug: '', display_name: '', match_prefix: '', product: 'dmt',
    source_value: '', has_bank_statement: true, has_internal_dump: true,
    is_active: true, sort_order: 0, notes: '', settlement_carry_days: 1
  }

  const startNew   = () => { setForm(blankForm); setEditing('new') }
  const startEdit  = (p) => { setForm({ ...p, source_value: p.source_value || '' }); setEditing(p) }
  const cancelEdit = () => setEditing(null)

  const save = async () => {
    if (!form.slug || !form.display_name || !form.match_prefix) {
      return toast.error('Slug, display name and match prefix are required')
    }
    if (form.match_prefix.length > 5) return toast.error('Match prefix must be ≤ 5 characters')
    try {
      if (editing === 'new') {
        await api.post('/admin/partners', form)
        toast.success('Partner created')
      } else {
        await api.put(`/admin/partners/${editing.id}`, form)
        toast.success('Partner updated')
      }
      setEditing(null)
      load()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Save failed')
    }
  }

  const deactivate = async (p) => {
    if (!confirm(`Deactivate "${p.display_name}"? It will be hidden from all dropdowns.`)) return
    try {
      await api.delete(`/admin/partners/${p.id}`)
      toast.success('Partner deactivated')
      load()
    } catch { toast.error('Failed') }
  }

  const reactivate = async (p) => {
    try {
      await api.put(`/admin/partners/${p.id}`, { ...p, is_active: true })
      toast.success('Partner reactivated')
      load()
    } catch { toast.error('Failed') }
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div>
          <p className="text-sm text-gray-500">One row per banking partner or product. Drives Upload dropdowns, RunRecon selectors, and auto-upload cards.</p>
        </div>
        <button onClick={startNew} className="btn-primary flex items-center gap-2">
          <Plus size={15} /> Add Partner
        </button>
      </div>

      {/* ── Add / Edit form ── */}
      {editing && (
        <div className="card mb-5 border-primary border-2">
          <h3 className="font-semibold text-gray-700 mb-4">{editing === 'new' ? 'New Partner' : `Edit: ${editing.display_name}`}</h3>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Slug <span className="text-red-400">*</span></label>
              <input className="input" placeholder="fino" value={form.slug}
                onChange={e => setForm(f => ({...f, slug: e.target.value.toLowerCase().replace(/[^a-z0-9_]/g,'')}))}
                disabled={editing !== 'new'} />
              <p className="text-xs text-gray-400 mt-0.5">Lowercase, no spaces</p>
            </div>
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Display Name <span className="text-red-400">*</span></label>
              <input className="input" placeholder="Fino Payments Bank" value={form.display_name}
                onChange={e => setForm(f => ({...f, display_name: e.target.value}))} />
            </div>
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Match Prefix <span className="text-red-400">*</span></label>
              <input className="input" placeholder="FNO" maxLength={5} value={form.match_prefix}
                onChange={e => setForm(f => ({...f, match_prefix: e.target.value.toUpperCase()}))} />
              <p className="text-xs text-gray-400 mt-0.5">3–5 chars, used in Match IDs</p>
            </div>
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Product</label>
              <select className="select" value={form.product} onChange={e => setForm(f => ({...f, product: e.target.value}))}>
                {PRODUCTS.map(p => <option key={p} value={p}>{p.toUpperCase()}</option>)}
              </select>
            </div>
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Source Value in Simplibank Dump</label>
              <input className="input" placeholder="FINO (blank if not in dump)" value={form.source_value}
                onChange={e => setForm(f => ({...f, source_value: e.target.value}))} />
              <p className="text-xs text-gray-400 mt-0.5">Value in the 'Source' column of Simplibank dump</p>
            </div>
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Sort Order</label>
              <input type="number" className="input" value={form.sort_order}
                onChange={e => setForm(f => ({...f, sort_order: parseInt(e.target.value)||0}))} />
            </div>
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Settlement Carry-Forward (days)</label>
              <input type="number" min="0" max="7" className="input" value={form.settlement_carry_days ?? 1}
                onChange={e => setForm(f => ({...f, settlement_carry_days: parseInt(e.target.value)||0}))} />
              <p className="text-xs text-gray-400 mt-0.5">1 = NEFT D+1 · 2 = T+2 · 0 = same-day only</p>
            </div>
            <div className="flex items-center gap-4 pt-5">
              <label className="flex items-center gap-2 text-sm text-gray-600 cursor-pointer">
                <input type="checkbox" checked={form.has_bank_statement}
                  onChange={e => setForm(f => ({...f, has_bank_statement: e.target.checked}))} />
                Has Bank Statement
              </label>
              <label className="flex items-center gap-2 text-sm text-gray-600 cursor-pointer">
                <input type="checkbox" checked={form.has_internal_dump}
                  onChange={e => setForm(f => ({...f, has_internal_dump: e.target.checked}))} />
                Has Internal Dump
              </label>
            </div>
            <div className="col-span-2">
              <label className="text-xs font-medium text-gray-500 block mb-1">Notes</label>
              <input className="input" placeholder="Optional notes" value={form.notes || ''}
                onChange={e => setForm(f => ({...f, notes: e.target.value}))} />
            </div>
          </div>
          <div className="flex gap-2 mt-4">
            <button onClick={save} className="btn-primary flex items-center gap-2"><Save size={14}/> Save</button>
            <button onClick={cancelEdit} className="btn-ghost">Cancel</button>
          </div>
        </div>
      )}

      {/* ── Partners table ── */}
      {loading ? <p className="text-sm text-gray-400 py-8 text-center">Loading…</p> : (
        <div className="card overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100">
                <th className="table-th">Partner</th>
                <th className="table-th">Slug</th>
                <th className="table-th">Prefix</th>
                <th className="table-th">Product</th>
                <th className="table-th">Source Col Value</th>
                <th className="table-th text-center">Bank Stmt</th>
                <th className="table-th text-center">Dump</th>
                <th className="table-th text-center">Status</th>
                <th className="table-th"></th>
              </tr>
            </thead>
            <tbody>
              {partners.map(p => (
                <tr key={p.id} className={`border-b border-gray-50 hover:bg-gray-50 ${!p.is_active ? 'opacity-50' : ''}`}>
                  <td className="table-td font-medium">{p.display_name}</td>
                  <td className="table-td font-mono text-xs">{p.slug}</td>
                  <td className="table-td font-mono text-xs font-bold">{p.match_prefix}</td>
                  <td className="table-td"><Badge label={p.product.toUpperCase()} color={productColor(p.product)} /></td>
                  <td className="table-td text-gray-500 text-xs">{p.source_value || '—'}</td>
                  <td className="table-td text-center">{p.has_bank_statement ? <CheckCircle size={14} className="text-green-500 mx-auto"/> : <span className="text-gray-300">—</span>}</td>
                  <td className="table-td text-center">{p.has_internal_dump  ? <CheckCircle size={14} className="text-green-500 mx-auto"/> : <span className="text-gray-300">—</span>}</td>
                  <td className="table-td text-center">
                    {p.is_active
                      ? <Badge label="Active" color="green" />
                      : <Badge label="Inactive" color="gray" />}
                  </td>
                  <td className="table-td">
                    <div className="flex items-center gap-2 justify-end">
                      <button onClick={() => startEdit(p)} className="text-gray-400 hover:text-primary"><Edit2 size={14}/></button>
                      {p.is_active
                        ? <button onClick={() => deactivate(p)} className="text-gray-400 hover:text-red-500"><Trash2 size={14}/></button>
                        : <button onClick={() => reactivate(p)} className="text-gray-400 hover:text-green-600 text-xs">Restore</button>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {partners.length === 0 && <p className="text-sm text-gray-400 text-center py-6">No partners yet</p>}
        </div>
      )}
    </div>
  )
}


// ────────────────────────────────────────────────────────────────────────────
// Format Presets Tab
// ────────────────────────────────────────────────────────────────────────────
function PresetsTab() {
  const [presets,  setPresets]  = useState([])
  const [partners, setPartners] = useState([])
  const [filter,   setFilter]   = useState('')
  const [editing,  setEditing]  = useState(null)
  const [form,     setForm]     = useState({})
  const [loading,  setLoading]  = useState(true)

  const load = async () => {
    try {
      const [{ data: ps }, { data: pts }] = await Promise.all([
        api.get('/admin/format-presets'),
        api.get('/admin/partners'),
      ])
      setPresets(ps)
      setPartners(pts)
    } catch { toast.error('Failed to load presets') }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const blankForm = {
    partner_slug: '', side: 'bank', label: '',
    signature_cols: '[]', column_mapping: '{}', special_config: '{}'
  }

  const startNew  = () => { setForm(blankForm); setEditing('new') }
  const startEdit = (p) => {
    setForm({
      partner_slug:   p.partner_slug,
      side:           p.side,
      label:          p.label,
      signature_cols: JSON.stringify(p.signature_cols, null, 2),
      column_mapping: JSON.stringify(p.column_mapping, null, 2),
      special_config: JSON.stringify(p.special_config, null, 2),
    })
    setEditing(p)
  }
  const cancelEdit = () => setEditing(null)

  const save = async () => {
    // Validate JSON fields
    for (const [key, val] of [['signature_cols', form.signature_cols], ['column_mapping', form.column_mapping], ['special_config', form.special_config]]) {
      try { JSON.parse(val) } catch { return toast.error(`Invalid JSON in ${key}`) }
    }
    try {
      if (editing === 'new') {
        await api.post('/admin/format-presets', form)
        toast.success('Preset created')
      } else {
        await api.put(`/admin/format-presets/${editing.id}`, form)
        toast.success('Preset updated')
      }
      setEditing(null)
      load()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Save failed')
    }
  }

  const deactivate = async (p) => {
    if (!confirm(`Deactivate "${p.label}"?`)) return
    try { await api.delete(`/admin/format-presets/${p.id}`); toast.success('Deactivated'); load() }
    catch { toast.error('Failed') }
  }

  const visible = filter ? presets.filter(p => p.partner_slug === filter) : presets

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <select className="select w-48" value={filter} onChange={e => setFilter(e.target.value)}>
            <option value="">All partners</option>
            {partners.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}
          </select>
          <p className="text-sm text-gray-400">Column mapping tells the system how to read each file format.</p>
        </div>
        <button onClick={startNew} className="btn-primary flex items-center gap-2"><Plus size={15}/> Add Preset</button>
      </div>

      {/* Edit / Add form */}
      {editing && (
        <div className="card mb-5 border-primary border-2">
          <h3 className="font-semibold text-gray-700 mb-4">
            {editing === 'new' ? 'New Format Preset' : `Edit: ${editing.label}`}
          </h3>
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-3 mb-3">
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Partner</label>
              <select className="select" value={form.partner_slug} onChange={e => setForm(f => ({...f, partner_slug: e.target.value}))}>
                <option value="">Select partner</option>
                {partners.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}
              </select>
            </div>
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Side</label>
              <select className="select" value={form.side} onChange={e => setForm(f => ({...f, side: e.target.value}))}>
                {SIDES.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Label</label>
              <input className="input" placeholder="Fino PTA Bank Statement" value={form.label}
                onChange={e => setForm(f => ({...f, label: e.target.value}))} />
            </div>
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Signature Columns <span className="text-gray-400">(JSON array — lowercased col names that identify this file)</span></label>
              <textarea rows={6} className="input font-mono text-xs" value={form.signature_cols}
                onChange={e => setForm(f => ({...f, signature_cols: e.target.value}))}
                placeholder='["description", "debits", "credits"]' />
            </div>
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Column Mapping <span className="text-gray-400">(JSON — standard field → file column name)</span></label>
              <textarea rows={6} className="input font-mono text-xs" value={form.column_mapping}
                onChange={e => setForm(f => ({...f, column_mapping: e.target.value}))}
                placeholder='{"eko_tid": "Partner_txn_id", "tracking_number": "RRN"}' />
              <p className="text-xs text-gray-400 mt-1">Standard fields: eko_tid, tracking_number, utr_number, amount, transaction_date, description, status</p>
            </div>
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Special Config <span className="text-gray-400">(JSON — extra options)</span></label>
              <textarea rows={6} className="input font-mono text-xs" value={form.special_config}
                onChange={e => setForm(f => ({...f, special_config: e.target.value}))}
                placeholder='{}' />
              <p className="text-xs text-gray-400 mt-1">Keys: debit_col, credit_col, dr_cr_col, amount_col, net_amount_col, typename_filter, source_filter, debit_credit_col</p>
            </div>
          </div>
          <div className="flex gap-2 mt-4">
            <button onClick={save} className="btn-primary flex items-center gap-2"><Save size={14}/> Save</button>
            <button onClick={cancelEdit} className="btn-ghost">Cancel</button>
          </div>
        </div>
      )}

      {loading ? <p className="text-sm text-gray-400 py-8 text-center">Loading…</p> : (
        <div className="card overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100">
                <th className="table-th">Partner</th>
                <th className="table-th">Side</th>
                <th className="table-th">Label</th>
                <th className="table-th">Signature Cols</th>
                <th className="table-th">Mapped Fields</th>
                <th className="table-th text-center">Active</th>
                <th className="table-th"></th>
              </tr>
            </thead>
            <tbody>
              {visible.map(p => (
                <tr key={p.id} className={`border-b border-gray-50 hover:bg-gray-50 ${!p.is_active ? 'opacity-40' : ''}`}>
                  <td className="table-td font-mono text-xs">{p.partner_slug}</td>
                  <td className="table-td"><Badge label={p.side} color={p.side === 'bank' ? 'blue' : 'green'} /></td>
                  <td className="table-td text-gray-700">{p.label}</td>
                  <td className="table-td text-xs text-gray-500">{p.signature_cols.length} cols</td>
                  <td className="table-td text-xs text-gray-500">{Object.keys(p.column_mapping).join(', ')}</td>
                  <td className="table-td text-center">{p.is_active ? <CheckCircle size={14} className="text-green-500 mx-auto"/> : '—'}</td>
                  <td className="table-td">
                    <div className="flex gap-2 justify-end">
                      <button onClick={() => startEdit(p)} className="text-gray-400 hover:text-primary"><Edit2 size={14}/></button>
                      {p.is_active && <button onClick={() => deactivate(p)} className="text-gray-400 hover:text-red-500"><Trash2 size={14}/></button>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {visible.length === 0 && <p className="text-sm text-gray-400 text-center py-6">No presets</p>}
        </div>
      )}
    </div>
  )
}


// ────────────────────────────────────────────────────────────────────────────
// Matching Rules Tab
// ────────────────────────────────────────────────────────────────────────────
function RulesTab() {
  const [rules,    setRules]    = useState([])
  const [partners, setPartners] = useState([])
  const [filter,   setFilter]   = useState('')
  const [editing,  setEditing]  = useState(null)
  const [form,     setForm]     = useState({})
  const [loading,  setLoading]  = useState(true)

  const load = async () => {
    try {
      const [{ data: rs }, { data: pts }] = await Promise.all([
        api.get('/admin/match-rules'),
        api.get('/admin/partners'),
      ])
      setRules(rs)
      setPartners(pts)
    } catch { toast.error('Failed to load rules') }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const blankForm = { name: '', partner: filter || '', priority: 1, match_fields: '[]', description: '', scope: 'bank_internal' }

  const startNew  = () => { setForm({...blankForm, partner: filter || ''}); setEditing('new') }
  const startEdit = (r) => {
    setForm({
      name: r.name, partner: r.partner, priority: r.priority,
      match_fields: JSON.stringify(r.match_fields), description: r.description || '',
      scope: r.scope || 'bank_internal'
    })
    setEditing(r)
  }
  const cancelEdit = () => setEditing(null)

  const toggleField = (field) => {
    let current = []
    try { current = JSON.parse(form.match_fields) } catch {}
    if (current.includes(field)) {
      setForm(f => ({...f, match_fields: JSON.stringify(current.filter(x => x !== field))}))
    } else {
      setForm(f => ({...f, match_fields: JSON.stringify([...current, field])}))
    }
  }

  const save = async () => {
    if (!form.name || !form.partner) return toast.error('Name and partner are required')
    let fields = []
    try { fields = JSON.parse(form.match_fields) } catch {}
    if (fields.length === 0) return toast.error('Select at least one match field')
    try {
      if (editing === 'new') {
        await api.post('/admin/match-rules', form)
        toast.success('Rule created')
      } else {
        await api.put(`/admin/match-rules/${editing.id}`, form)
        toast.success('Rule updated')
      }
      setEditing(null)
      load()
    } catch (err) { toast.error(err.response?.data?.detail || 'Save failed') }
  }

  const toggle = async (r) => {
    try { await api.patch(`/admin/match-rules/${r.id}/toggle`); load() }
    catch { toast.error('Failed') }
  }

  const del = async (r) => {
    if (!confirm(`Delete rule "${r.name}" for ${r.partner}?`)) return
    try { await api.delete(`/admin/match-rules/${r.id}`); toast.success('Deleted'); load() }
    catch { toast.error('Failed') }
  }

  const visible = filter ? rules.filter(r => r.partner === filter) : rules

  // Group by partner for display
  const grouped = visible.reduce((acc, r) => {
    if (!acc[r.partner]) acc[r.partner] = []
    acc[r.partner].push(r)
    return acc
  }, {})

  const fieldChip = (f) => {
    const colors = { eko_tid: 'blue', tracking_number: 'green', utr_number: 'purple', amount: 'orange' }
    return <Badge key={f} label={f.replace('_', ' ')} color={colors[f] || 'gray'} />
  }

  // Side-pairing scope (Rajendra 2026-07-27). Same-side = net-zero opposite DR/CR.
  const SCOPES = [
    { value: 'bank_internal',     label: 'Bank ↔ Simplibank' },
    { value: 'bank_bank',         label: 'Bank ↔ Bank' },
    { value: 'internal_internal', label: 'Simplibank ↔ Simplibank' },
  ]
  const SCOPE_LABEL = Object.fromEntries(SCOPES.map(s => [s.value, s.label]))

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <select className="select w-48" value={filter} onChange={e => setFilter(e.target.value)}>
            <option value="">All partners</option>
            {partners.filter(isCoreLedgerPartner).map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}
          </select>
          <p className="text-sm text-gray-400">Rules applied in priority order — lower number = tried first.</p>
        </div>
        <button onClick={startNew} className="btn-primary flex items-center gap-2"><Plus size={15}/> Add Rule</button>
      </div>

      {/* Edit / Add form */}
      {editing && (
        <div className="card mb-5 border-primary border-2">
          <h3 className="font-semibold text-gray-700 mb-4">{editing === 'new' ? 'New Rule' : `Edit: ${editing.name}`}</h3>
          <div className="grid grid-cols-1 lg:grid-cols-4 gap-3">
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Rule Name</label>
              <input className="input" placeholder="TID + Tracking" value={form.name}
                onChange={e => setForm(f => ({...f, name: e.target.value}))} />
            </div>
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Partner</label>
              <select className="select" value={form.partner} onChange={e => setForm(f => ({...f, partner: e.target.value}))}>
                <option value="">Select partner</option>
                {partners.filter(isCoreLedgerPartner).map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}
              </select>
            </div>
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Priority (1 = highest)</label>
              <input type="number" className="input" min={1} value={form.priority}
                onChange={e => setForm(f => ({...f, priority: parseInt(e.target.value)||1}))} />
            </div>
            <div>
              <label className="text-xs font-medium text-gray-500 block mb-1">Description (optional)</label>
              <input className="input" placeholder="When IMPS RRN is available" value={form.description}
                onChange={e => setForm(f => ({...f, description: e.target.value}))} />
            </div>
          </div>
          <div className="mt-3">
            <label className="text-xs font-medium text-gray-500 block mb-1">Match Direction</label>
            <select className="select lg:w-64" value={form.scope || 'bank_internal'}
              onChange={e => setForm(f => ({...f, scope: e.target.value}))}>
              {SCOPES.map(s => <option key={s.value} value={s.value}>{s.label}</option>)}
            </select>
            {form.scope && form.scope !== 'bank_internal' && (
              <p className="text-[11px] text-amber-600 mt-1">
                Same-side rule: pairs an opposite debit + credit (reversal / net-zero); does not affect bank-vs-Simplibank totals.
              </p>
            )}
          </div>
          <div className="mt-3">
            <label className="text-xs font-medium text-gray-500 block mb-2">Match Fields — ALL must match for a hit</label>
            <div className="flex flex-wrap gap-2">
              {MATCH_FIELD_OPTIONS.map(field => {
                let current = []; try { current = JSON.parse(form.match_fields) } catch {}
                const active = current.includes(field)
                return (
                  <button key={field} onClick={() => toggleField(field)}
                    className={`px-3 py-1.5 rounded-lg text-sm border font-medium transition-all
                      ${active ? 'bg-primary text-white border-primary' : 'bg-white text-gray-500 border-gray-200 hover:border-primary'}`}>
                    {field.replace(/_/g, ' ')}
                  </button>
                )
              })}
            </div>
            <p className="text-xs text-gray-400 mt-1">
              Selected: {(() => { try { return JSON.parse(form.match_fields).join(' + ') || 'none' } catch { return 'invalid' } })()}
            </p>
          </div>
          <div className="flex gap-2 mt-4">
            <button onClick={save} className="btn-primary flex items-center gap-2"><Save size={14}/> Save</button>
            <button onClick={cancelEdit} className="btn-ghost">Cancel</button>
          </div>
        </div>
      )}

      {loading ? <p className="text-sm text-gray-400 py-8 text-center">Loading…</p> : (
        Object.entries(grouped).length === 0
          ? <div className="card text-center py-8 text-gray-400 text-sm">No rules found</div>
          : Object.entries(grouped).sort(([a],[b]) => a.localeCompare(b)).map(([partner, partnerRules]) => (
          <div key={partner} className="card mb-4">
            <div className="flex items-center gap-2 mb-3">
              <h3 className="font-semibold text-gray-700 capitalize">{partner}</h3>
              <Badge label={partnerRules[0] ? partners.find(p=>p.slug===partner)?.product?.toUpperCase() || '' : ''} color={productColor(partners.find(p=>p.slug===partner)?.product||'')} />
              <span className="text-xs text-gray-400">{partnerRules.length} rule{partnerRules.length !== 1 ? 's' : ''}</span>
            </div>
            <div className="space-y-2">
              {[...partnerRules].sort((a,b)=>a.priority-b.priority).map((r,i) => (
                <div key={r.id} className={`flex items-center gap-3 p-2.5 rounded-lg border ${r.is_active ? 'border-gray-200 bg-gray-50' : 'border-gray-100 bg-gray-50 opacity-50'}`}>
                  <span className="text-xs font-bold text-gray-400 w-5 text-center">{r.priority}</span>
                  <span className="text-sm font-medium text-gray-700 flex-1">{r.name}
                    {r.scope && r.scope !== 'bank_internal' && (
                      <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded-full font-semibold bg-amber-50 text-amber-700 border border-amber-200">
                        {SCOPE_LABEL[r.scope] || r.scope}
                      </span>
                    )}
                  </span>
                  <div className="flex gap-1">{r.match_fields.map(fieldChip)}</div>
                  {r.description && <span className="text-xs text-gray-400 italic hidden lg:block">{r.description}</span>}
                  <div className="flex items-center gap-2">
                    <button onClick={() => toggle(r)} title={r.is_active ? 'Disable' : 'Enable'}
                      className={`text-sm ${r.is_active ? 'text-green-500 hover:text-gray-400' : 'text-gray-300 hover:text-green-500'}`}>
                      {r.is_active ? <ToggleRight size={18}/> : <ToggleLeft size={18}/>}
                    </button>
                    <button onClick={() => startEdit(r)} className="text-gray-400 hover:text-primary"><Edit2 size={13}/></button>
                    <button onClick={() => del(r)} className="text-gray-400 hover:text-red-500"><Trash2 size={13}/></button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))
      )}

      {visible.length === 0 && !loading && (
        <div className="card text-center py-8">
          <AlertTriangle size={24} className="text-amber-400 mx-auto mb-2"/>
          <p className="text-sm text-gray-500">No rules found for this partner.</p>
          <p className="text-xs text-gray-400 mt-1">The engine will use built-in fallback rules until you add rules here.</p>
        </div>
      )}
    </div>
  )
}


// ────────────────────────────────────────────────────────────────────────────
// Settlement Banks Tab
// ────────────────────────────────────────────────────────────────────────────
const EMPTY_SB = {
  partner: '', partner_label: '', bank_name: '', account_number: '',
  narration_keyword: '', settlement_period: 'T+2', is_active: true, notes: '',
}

const PI_PARTNERS = [
  { value: 'pg',   label: 'PayU (Accept Payment)' },
  { value: 'aeps', label: 'AePS Cashout (Fingpay)' },
  { value: 'qr',   label: 'QR Collection (Paypoint)' },
]

function SettlementBanksTab() {
  const [configs, setConfigs] = useState([])
  const [sevStatus, setSevStatus] = useState([])
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState(null)
  const [form, setForm] = useState(EMPTY_SB)
  const [saving, setSaving] = useState(false)
  const upd = patch => setForm(f => ({ ...f, ...patch }))

  const load = async () => {
    try {
      const [cfgRes, sevRes] = await Promise.all([
        api.get('/settlement-bank/configs'),
        api.get('/settlement-bank/sev-status'),
      ])
      setConfigs(cfgRes.data.configs || [])
      setSevStatus(sevRes.data.rows || [])
    } catch { toast.error('Failed to load settlement bank config') }
  }

  useEffect(() => { load() }, [])

  const openCreate = () => { setForm(EMPTY_SB); setEditing(null); setShowForm(true) }
  const openEdit   = cfg => {
    setForm({ ...cfg })
    setEditing(cfg)
    setShowForm(true)
  }

  const handleSave = async () => {
    if (!form.partner || !form.bank_name || !form.narration_keyword)
      return toast.error('Partner, Bank Name, and Narration Keyword are required')
    // Auto-fill label if empty
    const payload = { ...form }
    if (!payload.partner_label)
      payload.partner_label = PI_PARTNERS.find(p => p.value === form.partner)?.label || form.partner
    setSaving(true)
    try {
      if (editing?.id) {
        await api.put(`/settlement-bank/configs/${editing.id}`, payload)
        toast.success('Settlement bank updated')
      } else {
        await api.post('/settlement-bank/configs', payload)
        toast.success('Settlement bank configured')
      }
      setShowForm(false)
      load()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Save failed')
    } finally { setSaving(false) }
  }

  const handleDelete = async id => {
    if (!window.confirm('Remove this settlement bank config?')) return
    try { await api.delete(`/settlement-bank/configs/${id}`); toast.success('Removed'); load() }
    catch { toast.error('Delete failed') }
  }

  return (
    <div className="space-y-6">
      {/* Explainer */}
      <div className="card bg-indigo-50 border border-indigo-100 p-4">
        <p className="text-sm font-semibold text-indigo-800 mb-1">Two-Stage Settlement Verification (SEV)</p>
        <p className="text-xs text-indigo-700">
          Settlement Verification (SEV) is a second-stage control. After PI transactions (PayU/AePS/QR) are matched against
          internal Simplibank records, the net settlement amount is verified against the actual bank credit
          in the settlement account. Configure the bank account and the keyword that appears in the bank
          narration to identify each partner's credits.
        </p>
      </div>

      {/* SEV status per partner */}
      <div>
        <h3 className="text-sm font-semibold text-gray-700 mb-3">SEV Configuration Status</h3>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {sevStatus.map(r => (
            <div key={r.partner} className={`card p-4 border-l-4 ${r.configured ? 'border-green-400' : 'border-amber-400'}`}>
              <div className="flex items-center gap-2 mb-1">
                {r.configured
                  ? <CheckCircle size={14} className="text-green-600" />
                  : <AlertTriangle size={14} className="text-amber-500" />
                }
                <span className="text-sm font-semibold text-gray-800">{r.label}</span>
              </div>
              {r.configured ? (
                <div className="text-xs text-gray-500 space-y-0.5">
                  <div>{r.bank_name}</div>
                  <div className="font-mono">...{r.account_number?.slice(-6)}</div>
                  <div className="text-indigo-600 font-medium">Keyword: "{r.narration_keyword}"</div>
                  <div>Settlement: {r.settlement_period}</div>
                </div>
              ) : (
                <p className="text-xs text-amber-600 mt-1">Not configured — SEV will be skipped</p>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Config list */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-gray-700">Settlement Bank Accounts</h3>
          <button onClick={openCreate} className="btn-primary flex items-center gap-1.5 text-sm">
            <Plus size={14} /> Add Settlement Bank
          </button>
        </div>

        {showForm && (
          <div className="card mb-4 border border-indigo-100">
            <div className="flex items-center justify-between mb-4">
              <h4 className="font-semibold text-gray-800 text-sm">
                {editing ? 'Edit Settlement Bank' : 'Add Settlement Bank'}
              </h4>
              <button onClick={() => setShowForm(false)} className="text-gray-400 hover:text-gray-600"><X size={15}/></button>
            </div>
            <div className="grid grid-cols-2 gap-3 mb-4">
              <div>
                <label className="text-xs text-gray-500 block mb-1">PI Partner *</label>
                <select className="select" value={form.partner} onChange={e => upd({ partner: e.target.value, partner_label: PI_PARTNERS.find(p=>p.value===e.target.value)?.label || '' })}>
                  <option value="">Select…</option>
                  {PI_PARTNERS.map(p => <option key={p.value} value={p.value}>{p.label}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs text-gray-500 block mb-1">Settlement Period</label>
                <select className="select" value={form.settlement_period} onChange={e => upd({ settlement_period: e.target.value })}>
                  {['T+1','T+2','T+3'].map(v => <option key={v} value={v}>{v}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs text-gray-500 block mb-1">Bank Name *</label>
                <input className="input" value={form.bank_name} onChange={e => upd({ bank_name: e.target.value })} placeholder="e.g. Axis Bank" />
              </div>
              <div>
                <label className="text-xs text-gray-500 block mb-1">Account Number</label>
                <input className="input font-mono" value={form.account_number} onChange={e => upd({ account_number: e.target.value })} placeholder="e.g. 000011112222333" />
              </div>
              <div className="col-span-2">
                <label className="text-xs text-gray-500 block mb-1">Narration Keyword * <span className="text-gray-400">(appears in bank statement credit narration)</span></label>
                <input className="input" value={form.narration_keyword} onChange={e => upd({ narration_keyword: e.target.value })} placeholder="e.g. GATEWAY SETTLEMENT" />
                <p className="text-xs text-gray-400 mt-1">
                  This string must appear in the bank statement's description/narration for credits from this partner.
                  Used to auto-identify the settlement credit during SEV matching.
                </p>
              </div>
              <div className="col-span-2">
                <label className="text-xs text-gray-500 block mb-1">Notes</label>
                <input className="input" value={form.notes} onChange={e => upd({ notes: e.target.value })} placeholder="Optional: settlement details, contact, etc." />
              </div>
            </div>
            <div className="flex gap-3">
              <button onClick={() => setShowForm(false)} className="btn-ghost">Cancel</button>
              <button onClick={handleSave} disabled={saving} className="btn-primary">
                {saving ? 'Saving…' : editing ? 'Update' : 'Save Configuration'}
              </button>
            </div>
          </div>
        )}

        {configs.length === 0 ? (
          <div className="text-center py-8 text-gray-400 text-sm">
            No settlement banks configured yet. Add one to enable SEV matching.
          </div>
        ) : (
          <div className="card overflow-hidden p-0">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 bg-gray-50 text-xs text-gray-400 uppercase tracking-wide">
                  <th className="text-left px-4 py-2.5">Partner</th>
                  <th className="text-left px-4 py-2.5">Bank</th>
                  <th className="text-left px-4 py-2.5">Account</th>
                  <th className="text-left px-4 py-2.5">Narration Keyword</th>
                  <th className="text-center px-4 py-2.5">Period</th>
                  <th className="px-4 py-2.5">Actions</th>
                </tr>
              </thead>
              <tbody>
                {configs.map(cfg => (
                  <tr key={cfg.id} className="border-b border-gray-50 hover:bg-gray-50">
                    <td className="px-4 py-3">
                      <div className="font-medium text-gray-800">{cfg.partner_label || cfg.partner}</div>
                    </td>
                    <td className="px-4 py-3 text-gray-600">{cfg.bank_name}</td>
                    <td className="px-4 py-3 font-mono text-xs text-gray-500">...{cfg.account_number?.slice(-8)}</td>
                    <td className="px-4 py-3">
                      <span className="text-xs bg-indigo-50 text-indigo-700 px-2 py-0.5 rounded font-medium font-mono">
                        {cfg.narration_keyword}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-center text-gray-500">{cfg.settlement_period}</td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <button onClick={() => openEdit(cfg)} className="text-gray-400 hover:text-primary"><Edit2 size={13}/></button>
                        <button onClick={() => handleDelete(cfg.id)} className="text-gray-400 hover:text-red-500"><Trash2 size={13}/></button>
                      </div>
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

// ────────────────────────────────────────────────────────────────────────────
// API Keys Tab (H1 — programmatic access for agents like OpenClaw)
// ────────────────────────────────────────────────────────────────────────────
function APIKeysTab() {
  const [keys, setKeys]       = useState([])
  const [showForm, setShowForm] = useState(false)
  const [newKey, setNewKey]   = useState(null)   // plaintext shown once after creation
  const [form, setForm]       = useState({ name: '', description: '', expires_days: '', allowed_ips: '' })
  const [saving, setSaving]   = useState(false)
  const upd = patch => setForm(f => ({ ...f, ...patch }))

  const load = async () => {
    try { const { data } = await api.get('/auth/api-keys'); setKeys(data) }
    catch { toast.error('Failed to load API keys') }
  }
  useEffect(() => { load() }, [])

  const handleCreate = async () => {
    if (!form.name.trim()) return toast.error('Key name is required')
    setSaving(true)
    try {
      const payload = { name: form.name, description: form.description }
      if (form.expires_days) payload.expires_days = parseInt(form.expires_days)
      if (form.allowed_ips && form.allowed_ips.trim()) payload.allowed_ips = form.allowed_ips.trim()
      const { data } = await api.post('/auth/api-keys', payload)
      setNewKey(data)
      setShowForm(false)
      setForm({ name: '', description: '', expires_days: '', allowed_ips: '' })
      load()
      toast.success('API key created — copy it now, it won\'t be shown again')
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Create failed')
    } finally { setSaving(false) }
  }

  const handleRevoke = async (id, name) => {
    if (!window.confirm(`Revoke API key "${name}"? Any agent using it will lose access immediately.`)) return
    try {
      await api.delete(`/auth/api-keys/${id}`)
      toast.success('Key revoked')
      load()
    } catch { toast.error('Revoke failed') }
  }

  const handleRestore = async (id) => {
    try { await api.patch(`/auth/api-keys/${id}/restore`); toast.success('Key restored'); load() }
    catch { toast.error('Restore failed') }
  }

  return (
    <div className="space-y-5">
      {/* Explainer */}
      <div className="card bg-indigo-50 border border-indigo-100 p-4 text-xs text-indigo-800">
        <p className="font-semibold mb-1">🔑 API Keys — Programmatic Access</p>
        <p>
          API keys let external agents (e.g. OpenClaw) call the Recon API without a browser session.
          Pass the key in the <code className="bg-indigo-100 px-1 rounded font-mono">X-API-Key</code> header.
          Keys are scoped — create separate keys per integration and revoke if compromised.
          The plaintext key is shown <strong>once</strong> at creation — store it in your agent's secrets vault immediately.
        </p>
        <p className="mt-1.5 font-mono text-indigo-600">
          Example: <code>curl -H "X-API-Key: eko_xxxx..." https://your-server/api/recon/open-items</code>
        </p>
      </div>

      {/* One-time key reveal */}
      {newKey && (
        <div className="card border-2 border-green-400 bg-green-50 p-5">
          <div className="flex items-start justify-between mb-3">
            <div className="flex items-center gap-2">
              <CheckCircle size={18} className="text-green-600" />
              <span className="font-bold text-green-800">API Key Created — Copy Now</span>
            </div>
            <button onClick={() => setNewKey(null)} className="text-gray-400 hover:text-gray-600"><X size={16}/></button>
          </div>
          <div className="bg-white rounded-lg p-3 font-mono text-sm text-gray-800 break-all border border-green-200 mb-3 select-all">
            {newKey.api_key}
          </div>
          <div className="flex gap-3 text-xs">
            <button
              onClick={() => { navigator.clipboard.writeText(newKey.api_key); toast.success('Copied to clipboard') }}
              className="btn-primary text-sm">
              Copy to Clipboard
            </button>
            <span className="text-red-600 font-semibold self-center">⚠ This key will NOT be shown again after you close this banner.</span>
          </div>
          <div className="mt-2 text-xs text-gray-500">
            Key prefix: <code className="font-mono">{newKey.key_prefix}…</code> · Expires: {newKey.expires_at}
          </div>
        </div>
      )}

      {/* Create form */}
      {showForm && (
        <div className="card border border-indigo-100">
          <div className="flex items-center justify-between mb-4">
            <h3 className="font-semibold text-gray-800 text-sm">Create New API Key</h3>
            <button onClick={() => setShowForm(false)} className="text-gray-400 hover:text-gray-600"><X size={15}/></button>
          </div>
          <div className="grid grid-cols-2 gap-3 mb-4">
            <div className="col-span-2">
              <label className="text-xs text-gray-500 block mb-1">Key Name * <span className="text-gray-400">(e.g. "OpenClaw Agent", "Finance Dashboard")</span></label>
              <input className="input" value={form.name} onChange={e => upd({ name: e.target.value })} placeholder="OpenClaw Recon Agent" autoFocus />
            </div>
            <div className="col-span-2">
              <label className="text-xs text-gray-500 block mb-1">Description</label>
              <input className="input" value={form.description} onChange={e => upd({ description: e.target.value })} placeholder="Used by OpenClaw agent for daily recon monitoring" />
            </div>
            <div>
              <label className="text-xs text-gray-500 block mb-1">Expires in (days) <span className="text-gray-400">— leave blank for never</span></label>
              <input type="number" className="input" value={form.expires_days} onChange={e => upd({ expires_days: e.target.value })} placeholder="365" min="1" />
            </div>
            <div className="md:col-span-2">
              <label className="text-xs text-gray-500 block mb-1">Allowed IPs / CIDRs <span className="text-gray-400">— comma-separated; blank = any IP</span></label>
              <input className="input" value={form.allowed_ips} onChange={e => upd({ allowed_ips: e.target.value })} placeholder="e.g. 203.0.113.10, 10.0.0.0/24 (lock the OpenClaw key to its server)" />
            </div>
          </div>
          <div className="flex gap-3">
            <button onClick={() => setShowForm(false)} className="btn-ghost">Cancel</button>
            <button onClick={handleCreate} disabled={saving} className="btn-primary">
              {saving ? 'Creating…' : 'Create Key'}
            </button>
          </div>
        </div>
      )}

      {/* Keys list */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-gray-700">Active & Revoked Keys ({keys.length})</h3>
          {!showForm && (
            <button onClick={() => setShowForm(true)} className="btn-primary flex items-center gap-1.5 text-sm">
              <Plus size={14} /> Create API Key
            </button>
          )}
        </div>

        {keys.length === 0 ? (
          <div className="text-center py-10 text-gray-400 text-sm">
            No API keys yet. Create one to allow programmatic access for agents and integrations.
          </div>
        ) : (
          <div className="card overflow-hidden p-0">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 bg-gray-50 text-xs text-gray-400 uppercase tracking-wide">
                  <th className="text-left px-4 py-2.5">Name</th>
                  <th className="text-left px-4 py-2.5">Prefix</th>
                  <th className="text-left px-4 py-2.5">Description</th>
                  <th className="text-center px-4 py-2.5">Status</th>
                  <th className="text-left px-4 py-2.5">Last Used</th>
                  <th className="text-left px-4 py-2.5">Expires</th>
                  <th className="px-4 py-2.5">Actions</th>
                </tr>
              </thead>
              <tbody>
                {keys.map(k => (
                  <tr key={k.id} className={`border-b border-gray-50 hover:bg-gray-50 ${!k.is_active ? 'opacity-50' : ''}`}>
                    <td className="px-4 py-2.5 font-medium text-gray-800">{k.name}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-gray-500">{k.key_prefix}…</td>
                    <td className="px-4 py-2.5 text-xs text-gray-500 max-w-[200px] truncate">{k.description || '—'}</td>
                    <td className="px-4 py-2.5 text-center">
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${k.is_active ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-600'}`}>
                        {k.is_active ? 'Active' : 'Revoked'}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-xs text-gray-500">
                      {k.last_used_at ? new Date(k.last_used_at).toLocaleString() : 'Never'}
                    </td>
                    <td className="px-4 py-2.5 text-xs text-gray-500">{k.expires_at}</td>
                    <td className="px-4 py-2.5">
                      {k.is_active
                        ? <button onClick={() => handleRevoke(k.id, k.name)}
                            className="text-xs text-red-500 hover:text-red-700 font-medium">Revoke</button>
                        : <button onClick={() => handleRestore(k.id)}
                            className="text-xs text-green-600 hover:text-green-800 font-medium">Restore</button>
                      }
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

// ────────────────────────────────────────────────────────────────────────────
// Main Admin Page
// ────────────────────────────────────────────────────────────────────────────
const AI_PRESETS = [
  ['OpenAI',        'https://api.openai.com/v1',                          'gpt-4o-mini'],
  ['OpenRouter',    'https://openrouter.ai/api/v1',                       'meta-llama/llama-3.3-70b-instruct'],
  ['Groq',          'https://api.groq.com/openai/v1',                     'llama-3.3-70b-versatile'],
  ['Together',      'https://api.together.xyz/v1',                        'meta-llama/Llama-3.3-70B-Instruct-Turbo'],
  ['Ollama (local)','http://localhost:11434/v1',                         'llama3.1'],
  ['vLLM (local)',  'http://localhost:8000/v1',                          ''],
  ['Gemini',        'https://generativelanguage.googleapis.com/v1beta/openai', 'gemini-1.5-flash'],
]

function AIConfigTab() {
  const [cfg, setCfg] = useState(null)
  const [provider, setProvider] = useState('anthropic')
  const [aiKey, setAiKey] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [anthKey, setAnthKey] = useState('')
  const [saving, setSaving] = useState(false)

  const load = () => api.get('/admin/ai-config').then(({ data }) => {
    setCfg(data)
    setProvider(data.provider || 'anthropic')
    setBaseUrl(data.base_url || '')
    setModel(data.model || '')
  }).catch(() => toast.error('Failed to load AI config'))
  useEffect(() => { load() }, [])

  const saveRecon = async () => {
    setSaving(true)
    try {
      const body = { provider, base_url: baseUrl.trim(), model: model.trim() }
      if (aiKey.trim()) body.ai_api_key = aiKey.trim()      // blank keeps the stored key
      await api.post('/admin/ai-config', body)
      toast.success('Reconciliation AI settings saved'); setAiKey(''); load()
    } catch (e) { toast.error(e?.response?.data?.detail || 'Save failed') }
    setSaving(false)
  }
  const saveAnth = async (clear = false) => {
    if (!clear && !anthKey.trim()) { toast.error('Paste an API key first'); return }
    setSaving(true)
    try {
      await api.post('/admin/ai-config', { api_key: clear ? '' : anthKey.trim() })
      toast.success(clear ? 'Anthropic key cleared' : 'Anthropic key saved'); setAnthKey(''); load()
    } catch (e) { toast.error(e?.response?.data?.detail || 'Save failed') }
    setSaving(false)
  }

  return (
    <div className="max-w-2xl space-y-8">
      {/* Reconciliation AI — any provider */}
      <div>
        <div className="mb-3">
          <h3 className="font-semibold text-gray-700">Reconciliation AI</h3>
          <p className="text-sm text-gray-500">Powers suggested matches, anomaly detection and insights. Use Anthropic, OpenAI, or any OpenAI-compatible endpoint — including open-source models via Ollama, vLLM, OpenRouter, Groq or Together. Keys are stored in the database, never shown or logged.</p>
        </div>
        <div className="card space-y-4">
          <div className="flex items-center gap-2 text-sm">
            <span className="text-gray-500">Status:</span>
            {cfg?.ai_configured
              ? <span className="text-green-700 font-medium">● Ready <span className="text-gray-400 font-normal">· {cfg.provider} · <span className="font-mono">{cfg.model}</span></span></span>
              : <span className="text-gray-400">Not configured</span>}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-gray-400 block mb-1">Provider</label>
              <select className="select w-full" value={provider} onChange={e => setProvider(e.target.value)}>
                <option value="anthropic">Anthropic (Claude)</option>
                <option value="openai">OpenAI / OpenAI-compatible (incl. open-source)</option>
              </select>
            </div>
            <div>
              <label className="text-xs text-gray-400 block mb-1">Model</label>
              <input className="input w-full font-mono" placeholder={provider === 'anthropic' ? 'claude-opus-4-8' : 'gpt-4o-mini'}
                value={model} onChange={e => setModel(e.target.value)} />
            </div>
          </div>
          {provider === 'openai' && (
            <div>
              <label className="text-xs text-gray-400 block mb-1">API base URL</label>
              <input className="input w-full font-mono" placeholder="https://api.openai.com/v1"
                value={baseUrl} onChange={e => setBaseUrl(e.target.value)} />
              <div className="flex flex-wrap gap-1.5 mt-1.5">
                {AI_PRESETS.map(([name, url, m]) => (
                  <button key={name} type="button" onClick={() => { setBaseUrl(url); if (m && !model.trim()) setModel(m) }}
                    className="text-[11px] px-2 py-0.5 rounded-full bg-gray-100 text-gray-600 hover:bg-gray-200">{name}</button>
                ))}
              </div>
            </div>
          )}
          <div>
            <label className="text-xs text-gray-400 block mb-1">
              API key {provider === 'openai' ? '(leave blank for a keyless local endpoint like Ollama)' : '(blank = use the Anthropic key below)'}
            </label>
            <input type="password" className="input w-full font-mono" autoComplete="off"
              placeholder={provider === 'anthropic' ? 'sk-ant-…' : 'provider key'} value={aiKey} onChange={e => setAiKey(e.target.value)} />
            {cfg?.ai_masked
              ? <p className="text-[11px] text-gray-400 mt-1">Stored key <span className="font-mono">{cfg.ai_masked}</span> — leave blank to keep it.</p>
              : <p className="text-[11px] text-gray-400 mt-1">Write-only — never returned to the browser.</p>}
          </div>
          <button onClick={saveRecon} disabled={saving} className="btn">Save reconciliation AI</button>
        </div>
      </div>

      {/* Developer Portal / Builder agent — Anthropic only */}
      <div>
        <div className="mb-3">
          <h3 className="font-semibold text-gray-700">Developer Portal / Builder agent</h3>
          <p className="text-sm text-gray-500">The autonomous agent runs on Anthropic. This key also acts as the fallback for Reconciliation AI when its provider is set to Anthropic.</p>
        </div>
        <div className="card space-y-4">
          <div className="flex items-center gap-2 text-sm">
            <span className="text-gray-500">Status:</span>
            {cfg?.configured
              ? <span className="text-green-700 font-medium">● Configured <span className="font-mono text-gray-400">{cfg.masked}</span> <span className="text-gray-400">(from {cfg.source})</span></span>
              : <span className="text-gray-400">Not configured</span>}
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">Anthropic API key</label>
            <input type="password" className="input w-full font-mono" placeholder="sk-ant-…" value={anthKey}
              onChange={e => setAnthKey(e.target.value)} autoComplete="off" />
            <p className="text-[11px] text-gray-400 mt-1">Write-only — never returned to the browser.</p>
          </div>
          <div className="flex gap-2">
            <button onClick={() => saveAnth(false)} disabled={saving} className="btn">Save key</button>
            {cfg?.configured && cfg.source === 'config' &&
              <button onClick={() => saveAnth(true)} disabled={saving} className="btn-ghost text-red-600">Clear</button>}
          </div>
        </div>
      </div>
    </div>
  )
}

export default function Admin() {
  const [tab, setTab] = useState('partners')

  return (
    <div>
      <div className="flex items-center gap-3 mb-1">
        <Settings size={20} className="text-gray-600" />
        <h1 className="text-xl font-bold text-gray-800">Configuration</h1>
      </div>
      <p className="text-sm text-gray-500 mb-6">
        Manage banking partners, file format mappings, and matching rules — without touching code.
      </p>

      {/* Tab bar */}
      <div className="flex gap-1 mb-6 border-b border-gray-200">
        {TABS.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors -mb-px
              ${tab === t.key ? 'border-primary text-primary' : 'border-transparent text-gray-500 hover:text-gray-700'}`}>
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'partners'         && <PartnersTab />}
      {tab === 'presets'          && <PresetsTab />}
      {tab === 'rules'            && <RulesTab />}
      {tab === 'settlement-banks' && <SettlementBanksTab />}
      {tab === 'bank-accounts'   && <BankAccountsTab />}
      {tab === 'evalue-accounts' && <EvalueAccountsTab />}
      {tab === 'api-keys'        && <APIKeysTab />}
      {tab === 'ai'              && <AIConfigTab />}
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// E-Value Accounts Tab
// ────────────────────────────────────────────────────────────────────────────
function EvalueAccountsTab() {
  const [rows, setRows] = useState([])
  const [form, setForm] = useState({ bank_name: '', account_number: '', reco_acc_no: '' })
  const [adding, setAdding] = useState(false)
  const load = () => api.get('/evalue/accounts/all').then(({ data }) => setRows(data)).catch(() => toast.error('Failed to load E-Value accounts'))
  useEffect(() => { load() }, [])
  const add = async () => {
    if (!form.bank_name || !form.account_number || !form.reco_acc_no) return toast.error('All fields required')
    try { await api.post('/evalue/accounts', form); toast.success('Account added'); setForm({ bank_name: '', account_number: '', reco_acc_no: '' }); setAdding(false); load() }
    catch (e) { toast.error(e.response?.data?.detail || 'Failed') }
  }
  const toggle = async (r) => { try { await api.patch(`/evalue/accounts/${r.id}/toggle`); load() } catch { toast.error('Failed') } }
  const banks = [...new Set(rows.map(r => r.bank_name))]
  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <p className="text-sm text-gray-500">{rows.length} E-Value collection accounts across {banks.length} banks. The <strong>reco code</strong> (e.g. SBI-4393) must match the internal dump's <code>source</code> column.</p>
        <button onClick={() => setAdding(!adding)} className="btn-primary text-sm">+ Add Account</button>
      </div>
      {adding && (
        <div className="card mb-4 grid grid-cols-1 md:grid-cols-4 gap-3 items-end">
          <div><label className="text-xs text-gray-500 block mb-1">Bank</label><input className="input" value={form.bank_name} onChange={e => setForm(f => ({ ...f, bank_name: e.target.value }))} placeholder="SBI" /></div>
          <div><label className="text-xs text-gray-500 block mb-1">Account Number</label><input className="input" value={form.account_number} onChange={e => setForm(f => ({ ...f, account_number: e.target.value }))} placeholder="00009999888877" /></div>
          <div><label className="text-xs text-gray-500 block mb-1">Reco Code</label><input className="input" value={form.reco_acc_no} onChange={e => setForm(f => ({ ...f, reco_acc_no: e.target.value }))} placeholder="SBI-4393" /></div>
          <button onClick={add} className="btn-primary">Save</button>
        </div>
      )}
      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead><tr className="border-b text-gray-500"><th className="table-th text-left">Bank</th><th className="table-th text-left">Account Number</th><th className="table-th text-left">Reco Code</th><th className="table-th">Status</th><th className="table-th"></th></tr></thead>
          <tbody>{rows.map(r => (
            <tr key={r.id} className={`border-b border-gray-50 hover:bg-gray-50 ${!r.is_active ? 'opacity-50' : ''}`}>
              <td className="table-td">{r.bank_name}</td><td className="table-td font-mono">{r.account_number}</td><td className="table-td font-mono font-semibold">{r.reco_acc_no}</td>
              <td className="table-td text-center">{r.is_active ? <span className="badge-green">Active</span> : <span className="badge-gray">Disabled</span>}</td>
              <td className="table-td text-right"><button onClick={() => toggle(r)} className="text-xs text-gray-500 hover:text-primary">{r.is_active ? 'Disable' : 'Enable'}</button></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  )
}


// ── Bank Accounts (per DMT partner) ──────────────────────────────────────────
// A partner like Axis runs several current accounts; statements are tagged with
// their account number at ingest. Register/disable accounts here; uploading a new
// account's statement also auto-discovers it.
function BankAccountsTab() {
  const [rows, setRows]   = useState([])
  const [form, setForm]   = useState({ partner: 'axis', account_number: '', label: '', ifsc: '', is_primary: false })
  const [adding, setAdding] = useState(false)
  const [partners, setPartners] = useState([])

  const load = () => api.get('/admin/bank-accounts').then(({ data }) => setRows(data)).catch(() => toast.error('Failed to load bank accounts'))
  useEffect(() => {
    load()
    api.get('/admin/partners-public?product=dmt').then(({ data }) => setPartners(data || [])).catch(() => {})
  }, [])

  const add = async () => {
    if (!form.partner || !form.account_number) return toast.error('Partner and account number are required')
    try {
      await api.post('/admin/bank-accounts', form)
      toast.success('Bank account added')
      setForm({ partner: form.partner, account_number: '', label: '', ifsc: '', is_primary: false })
      setAdding(false); load()
    } catch (e) { toast.error(e.response?.data?.detail || 'Failed') }
  }
  const toggle = async (r) => {
    try { await api.put(`/admin/bank-accounts/${r.id}`, { ...r, is_active: !r.is_active }); load() }
    catch { toast.error('Failed') }
  }
  const remove = async (r) => {
    if (!window.confirm(`Remove account ${r.account_number}?`)) return
    try { await api.delete(`/admin/bank-accounts/${r.id}`); toast.success('Removed'); load() }
    catch { toast.error('Failed') }
  }
  const partnerOpts = partners.length ? partners.map(p => ({ slug: p.slug, name: p.display_name }))
                                       : [{ slug: 'axis', name: 'Axis Bank' }, { slug: 'fino', name: 'Fino Payments Bank' }, { slug: 'airtel', name: 'Airtel Payments Bank' }]

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <p className="text-sm text-gray-500">{rows.length} registered bank accounts. A partner (e.g. Axis) can hold several accounts; each bank statement is tagged with its account number so you can filter and report per account. Recon still runs under the single partner.</p>
        <button onClick={() => setAdding(!adding)} className="btn-primary text-sm">+ Add Bank Account</button>
      </div>
      {adding && (
        <div className="card mb-4 grid grid-cols-1 md:grid-cols-5 gap-3 items-end">
          <div><label className="text-xs text-gray-500 block mb-1">Partner</label>
            <select className="select" value={form.partner} onChange={e => setForm(f => ({ ...f, partner: e.target.value }))}>
              {partnerOpts.map(p => <option key={p.slug} value={p.slug}>{p.name}</option>)}
            </select></div>
          <div><label className="text-xs text-gray-500 block mb-1">Account Number</label><input className="input" value={form.account_number} onChange={e => setForm(f => ({ ...f, account_number: e.target.value }))} placeholder="000011112222333" /></div>
          <div><label className="text-xs text-gray-500 block mb-1">Label</label><input className="input" value={form.label} onChange={e => setForm(f => ({ ...f, label: e.target.value }))} placeholder="Axis DMT — A/c …5990" /></div>
          <div><label className="text-xs text-gray-500 block mb-1">IFSC (optional)</label><input className="input" value={form.ifsc} onChange={e => setForm(f => ({ ...f, ifsc: e.target.value }))} placeholder="BANK0001234" /></div>
          <button onClick={add} className="btn-primary">Save</button>
        </div>
      )}
      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead><tr className="border-b text-gray-500"><th className="table-th text-left">Partner</th><th className="table-th text-left">Account Number</th><th className="table-th text-left">Label</th><th className="table-th text-left">IFSC</th><th className="table-th">Source</th><th className="table-th">Status</th><th className="table-th"></th></tr></thead>
          <tbody>{rows.map(r => (
            <tr key={r.id} className={`border-b border-gray-50 hover:bg-gray-50 ${!r.is_active ? 'opacity-50' : ''}`}>
              <td className="table-td capitalize">{r.partner}{r.is_primary && <span className="ml-1 badge-blue text-[10px]">primary</span>}</td>
              <td className="table-td font-mono">{r.account_number}</td>
              <td className="table-td">{r.label || '—'}</td>
              <td className="table-td font-mono">{r.ifsc || '—'}</td>
              <td className="table-td text-center text-xs text-gray-400">{r.auto_added ? 'auto' : 'manual'}</td>
              <td className="table-td text-center">{r.is_active ? <span className="badge-green">Active</span> : <span className="badge-gray">Disabled</span>}</td>
              <td className="table-td text-right whitespace-nowrap">
                <button onClick={() => toggle(r)} className="text-xs text-gray-500 hover:text-primary mr-3">{r.is_active ? 'Disable' : 'Enable'}</button>
                <button onClick={() => remove(r)} className="text-xs text-red-400 hover:text-red-600">Delete</button>
              </td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  )
}
