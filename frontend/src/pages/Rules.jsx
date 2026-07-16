import React, { useState, useEffect } from 'react'
import { Plus, Trash2, ToggleLeft, ToggleRight, BookOpen, Edit2 } from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'
import { isCoreLedgerPartner, MATCH_FIELDS } from '../productRegistry'

const FIELDS = MATCH_FIELDS

export default function Rules() {
  const [rules, setRules] = useState([])
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({ name: '', partner: '', priority: 1, match_fields: ['eko_tid'], description: '' })
  const [editingId, setEditingId] = useState(null)
  const [partnerList, setPartnerList] = useState([])
  const user = JSON.parse(localStorage.getItem('user') || '{}')

  const load = async () => {
    try { const { data } = await api.get('/recon/rules'); setRules(data) } catch {}
  }
  useEffect(() => {
    load()
    api.get('/admin/partners-public')
      .then(({ data }) => { const core = (data || []).filter(isCoreLedgerPartner); if (core.length) setPartnerList(core) })
      .catch(() => {})
  }, [])

  const startNew = () => {
    setEditingId(null)
    setForm({ name: '', partner: '', priority: 1, match_fields: ['eko_tid'], description: '' })
    setShowForm(true)
  }
  const startEdit = (r) => {
    setEditingId(r.id)
    setForm({ name: r.name, partner: r.partner, priority: r.priority,
              match_fields: r.match_fields, description: r.description || '' })
    setShowForm(true)
  }
  const closeForm = () => { setShowForm(false); setEditingId(null) }
  const handleSave = async () => {
    try {
      if (editingId) { await api.put(`/recon/rules/${editingId}`, form); toast.success('Rule updated') }
      else { await api.post('/recon/rules', form); toast.success('Rule created') }
      closeForm(); load()
    } catch (err) { toast.error(err.response?.data?.detail || 'Failed') }
  }

  const handleToggle = async (id) => {
    try { await api.put(`/recon/rules/${id}/toggle`); load() } catch { toast.error('Failed') }
  }

  const handleDelete = async (id) => {
    if (!confirm('Delete this rule?')) return
    try { await api.delete(`/recon/rules/${id}`); load(); toast.success('Deleted') } catch { toast.error('Failed') }
  }

  const toggleField = (field) => {
    setForm(f => ({
      ...f,
      match_fields: f.match_fields.includes(field)
        ? f.match_fields.filter(x => x !== field)
        : [...f.match_fields, field]
    }))
  }

  const canEdit = user.role === 'admin' || user.permissions?.logic_builder

  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <h1 className="text-xl font-bold text-gray-800">Logic Builder</h1>
        {canEdit && (
          <button onClick={startNew} className="btn-primary flex items-center gap-2">
            <Plus size={15} /> New Rule
          </button>
        )}
      </div>
      <p className="text-sm text-gray-500 mb-5">Define and manage matching rules. Rules are applied in priority order.</p>

      {/* How rules work (the real per-partner defaults are seeded and listed below —
          this box used to hard-code a stale Airtel/Fino sample). */}
      <div className="bg-blue-50 border border-blue-100 rounded-xl p-4 mb-5 text-sm text-blue-700">
        <p className="font-semibold mb-1 flex items-center gap-2"><BookOpen size={14} /> How matching rules work</p>
        <p className="text-xs text-blue-600 mt-1">
          Every partner is seeded with sensible default rules (shown below). Rules run in priority
          order (1 = tried first) and match on the fields you select — the first rule that matches
          wins. Edit, disable, or add your own here.
        </p>
      </div>

      {/* Custom rules */}
      <div className="card">
        <h2 className="font-semibold text-gray-700 mb-3">Custom Rules</h2>
        {rules.length === 0 ? (
          <p className="text-sm text-gray-400 text-center py-6">No custom rules yet. Default rules will be used.</p>
        ) : (
          <div className="space-y-2">
            {rules.map(rule => (
              <div key={rule.id} className={`flex items-center gap-3 p-3 rounded-lg border ${rule.is_active ? 'border-gray-200 bg-white' : 'border-gray-100 bg-gray-50 opacity-60'}`}>
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-sm text-gray-800">{rule.name}</span>
                    <span className="badge-blue capitalize">{rule.partner}</span>
                    <span className="badge-gray">P{rule.priority}</span>
                    {!rule.is_active && <span className="badge-gray">Inactive</span>}
                  </div>
                  <div className="flex gap-1 mt-1">
                    {rule.match_fields.map(f => (
                      <span key={f} className="badge-blue text-xs">{f.replace(/_/g, ' ')}</span>
                    ))}
                  </div>
                  {rule.description && <p className="text-xs text-gray-400 mt-1">{rule.description}</p>}
                </div>
                {canEdit && (
                  <div className="flex gap-2">
                    <button onClick={() => startEdit(rule)} className="text-gray-400 hover:text-primary" title="Edit">
                      <Edit2 size={15} />
                    </button>
                    <button onClick={() => handleToggle(rule.id)} className="text-gray-400 hover:text-primary">
                      {rule.is_active ? <ToggleRight size={20} className="text-green-500" /> : <ToggleLeft size={20} />}
                    </button>
                    <button onClick={() => handleDelete(rule.id)} className="text-gray-400 hover:text-red-500">
                      <Trash2 size={16} />
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Create modal */}
      {showForm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-md p-6">
            <h3 className="font-semibold text-gray-800 mb-4">{editingId ? 'Edit' : 'Create'} Matching Rule</h3>
            <div className="space-y-3">
              <div>
                <label className="text-xs text-gray-500 block mb-1">Rule Name</label>
                <input className="input" placeholder="e.g. TID + Amount match"
                  value={form.name} onChange={e => setForm({...form, name: e.target.value})} />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Partner</label>
                  <select className="select" value={form.partner} onChange={e => setForm({...form, partner: e.target.value})}>
                    <option value="">Select…</option>
                    <option value="all">All Partners</option>
                    {partnerList.map(p => <option key={p.slug} value={p.slug}>{p.display_name}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs text-gray-500 block mb-1">Priority (1 = highest)</label>
                  <input type="number" className="input" min={1} max={99}
                    value={form.priority} onChange={e => setForm({...form, priority: parseInt(e.target.value)})} />
                </div>
              </div>
              <div>
                <label className="text-xs text-gray-500 block mb-2">Match Fields (select all that must match)</label>
                <div className="flex flex-wrap gap-2">
                  {FIELDS.map(f => (
                    <button key={f} type="button" onClick={() => toggleField(f)}
                      className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors
                        ${form.match_fields.includes(f) ? 'bg-primary text-white border-primary' : 'bg-white text-gray-600 border-gray-200 hover:border-primary'}`}>
                      {f.replace(/_/g, ' ')}
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <label className="text-xs text-gray-500 block mb-1">Description</label>
                <input className="input" placeholder="Optional..."
                  value={form.description} onChange={e => setForm({...form, description: e.target.value})} />
              </div>
            </div>
            <div className="flex gap-3 mt-4">
              <button onClick={closeForm} className="btn-ghost flex-1">Cancel</button>
              <button onClick={handleSave} disabled={!form.name || form.match_fields.length === 0} className="btn-primary flex-1">{editingId ? 'Save Changes' : 'Create Rule'}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
