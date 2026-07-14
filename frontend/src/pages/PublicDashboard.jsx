// Public /exec dashboard: passwordless, @eko.co.in-only access via an emailed
// one-time code. If a (viewer/operator) session already exists, the dashboard
// renders directly; otherwise the email-code gate is shown. The code exchange
// mints a short-lived VIEWER session (server locks it to the dashboard).
import React, { useState } from 'react'
import api from '../utils/api'
import toast from 'react-hot-toast'
import { Scale, Loader2, Mail, KeyRound, ArrowLeft } from 'lucide-react'
import Analytics from './Analytics'

const EKO_RE = /^[^@\s]+@eko\.co\.in$/i

// Same chrome-free shell the app uses for the standalone dashboard.
function StandaloneShell() {
  return (
    <main className="min-h-screen overflow-y-auto bg-surface">
      <div className="p-6 max-w-7xl mx-auto animate-fade-in">
        <Analytics standalone />
      </div>
    </main>
  )
}

function EmailCodeGate({ onAuthed }) {
  const [step, setStep] = useState('email')   // 'email' | 'code'
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [loading, setLoading] = useState(false)

  const sendCode = async (e) => {
    e?.preventDefault()
    const em = email.trim().toLowerCase()
    if (!EKO_RE.test(em)) return toast.error('Enter your @eko.co.in email address')
    setLoading(true)
    try {
      await api.post('/public/dashboard/request-code', { email: em })
      toast.success('Code sent — check your inbox')
      setStep('code')
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Could not send the code')
    } finally { setLoading(false) }
  }

  const verify = async (e) => {
    e?.preventDefault()
    const c = code.trim()
    if (!/^\d{6}$/.test(c)) return toast.error('Enter the 6-digit code')
    setLoading(true)
    try {
      const { data } = await api.post('/public/dashboard/verify-code',
        { email: email.trim().toLowerCase(), code: c })
      localStorage.setItem('token', data.access_token)
      localStorage.setItem('user', JSON.stringify(data.user))
      onAuthed()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Invalid or expired code')
    } finally { setLoading(false) }
  }

  return (
    <div className="relative min-h-screen bg-gradient-to-br from-primary via-primary to-primary-dark flex items-center justify-center p-4 overflow-hidden">
      <div className="pointer-events-none absolute -top-32 -left-32 w-96 h-96 rounded-full bg-accent/10 blur-3xl" />
      <div className="pointer-events-none absolute -bottom-40 -right-24 w-[28rem] h-[28rem] rounded-full bg-primary-400/20 blur-3xl" />

      <div className="relative w-full max-w-md animate-fade-in">
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-16 h-16 bg-accent/15 ring-1 ring-accent/30 rounded-2xl mb-4 shadow-lg">
            <Scale size={30} className="text-accent" />
          </div>
          <h1 className="text-2xl font-bold text-white tracking-tight">Eko Bharat Ventures</h1>
          <p className="text-white/60 text-sm mt-1">Executive Dashboard</p>
        </div>

        <div className="bg-white rounded-2xl shadow-2xl ring-1 ring-black/5 p-8">
          {step === 'email' ? (
            <form onSubmit={sendCode} className="space-y-4">
              <div>
                <h2 className="text-lg font-semibold text-gray-800 mb-1">View the dashboard</h2>
                <p className="text-xs text-gray-500 mb-4">Enter your <strong>@eko.co.in</strong> email and we'll send you a one-time code. No password needed.</p>
                <label className="text-sm font-medium text-gray-600 block mb-1">Work email</label>
                <div className="relative">
                  <Mail size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
                  <input className="input pl-9" type="email" placeholder="you@eko.co.in" autoFocus
                    value={email} onChange={e => setEmail(e.target.value)} required />
                </div>
              </div>
              <button type="submit" disabled={loading}
                className="btn-primary w-full py-2.5 mt-2 inline-flex items-center justify-center gap-2">
                {loading && <Loader2 size={15} className="animate-spin" />}
                {loading ? 'Sending…' : 'Send code'}
              </button>
            </form>
          ) : (
            <form onSubmit={verify} className="space-y-4">
              <div>
                <h2 className="text-lg font-semibold text-gray-800 mb-1">Enter your code</h2>
                <p className="text-xs text-gray-500 mb-4">We sent a 6-digit code to <strong>{email.trim().toLowerCase()}</strong>. It expires in 10 minutes.</p>
                <label className="text-sm font-medium text-gray-600 block mb-1">6-digit code</label>
                <div className="relative">
                  <KeyRound size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
                  <input className="input pl-9 tracking-[0.4em] font-semibold text-center" inputMode="numeric"
                    maxLength={6} placeholder="••••••" autoFocus
                    value={code} onChange={e => setCode(e.target.value.replace(/\D/g, ''))} required />
                </div>
              </div>
              <button type="submit" disabled={loading}
                className="btn-primary w-full py-2.5 inline-flex items-center justify-center gap-2">
                {loading && <Loader2 size={15} className="animate-spin" />}
                {loading ? 'Verifying…' : 'View dashboard'}
              </button>
              <button type="button" onClick={() => { setStep('email'); setCode('') }}
                className="w-full text-xs text-gray-500 hover:text-gray-700 inline-flex items-center justify-center gap-1">
                <ArrowLeft size={12} /> Use a different email
              </button>
            </form>
          )}
        </div>
        <p className="text-center text-white/40 text-xs mt-6">Access restricted to @eko.co.in accounts</p>
      </div>
    </div>
  )
}

export default function PublicDashboard() {
  const [token, setToken] = useState(() => localStorage.getItem('token'))
  if (!token) return <EmailCodeGate onAuthed={() => setToken(localStorage.getItem('token'))} />
  return <StandaloneShell />
}
