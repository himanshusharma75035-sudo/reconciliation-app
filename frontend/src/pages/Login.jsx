import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Eye, EyeOff, Loader2, KeyRound, ArrowLeft } from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'
import EkoLogo from '../components/EkoLogo'

const DEVICE_TRUST_KEY = 'device_trust'

export default function Login() {
  const [form, setForm] = useState({ username: '', password: '' })
  const [showPass, setShowPass] = useState(false)
  const [loading, setLoading] = useState(false)
  // 2FA
  const [step, setStep] = useState('creds')      // 'creds' | 'otp'
  const [code, setCode] = useState('')
  const [remember, setRemember] = useState(true)
  const [emailHint, setEmailHint] = useState('')
  const [pending, setPending] = useState('')     // login-pending token from /login
  const navigate = useNavigate()

  const finish = (data) => {
    localStorage.setItem('token', data.access_token)
    localStorage.setItem('user', JSON.stringify(data.user))
    if (data.device_trust) localStorage.setItem(DEVICE_TRUST_KEY, data.device_trust)
    toast.success(`Welcome, ${data.user.full_name || data.user.username}`)
    navigate(data.user.role === 'viewer' ? '/exec' : '/dashboard')
  }

  const submitCreds = async (e) => {
    e.preventDefault()
    setLoading(true)
    try {
      const params = new URLSearchParams()
      params.append('username', form.username)
      params.append('password', form.password)
      const trust = localStorage.getItem(DEVICE_TRUST_KEY) || ''
      const { data } = await api.post('/auth/login', params, {
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          ...(trust ? { 'X-Device-Trust': trust } : {}),
        },
      })
      if (data.otp_required) {
        setPending(data.pending || '')
        setEmailHint(data.email_hint || '')
        setStep('otp')
        setCode('')
        toast.success('Verification code sent to your email')
      } else {
        finish(data)
      }
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  const submitOtp = async (e) => {
    e.preventDefault()
    if (!/^\d{6}$/.test(code.trim())) return toast.error('Enter the 6-digit code')
    setLoading(true)
    try {
      const { data } = await api.post('/auth/verify-otp', {
        pending, code: code.trim(), remember_device: remember,
      })
      finish(data)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Invalid or expired code')
    } finally {
      setLoading(false)
    }
  }

  const backToCreds = () => { setStep('creds'); setCode('') }

  return (
    <div className="relative min-h-screen bg-gradient-to-br from-primary via-primary to-primary-dark flex items-center justify-center p-4 overflow-hidden">
      <div className="pointer-events-none absolute -top-32 -left-32 w-96 h-96 rounded-full bg-accent/10 blur-3xl" />
      <div className="pointer-events-none absolute -bottom-40 -right-24 w-[28rem] h-[28rem] rounded-full bg-primary-400/20 blur-3xl" />

      <div className="relative w-full max-w-md animate-fade-in">
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center px-6 h-16 bg-accent/15 ring-1 ring-accent/30 rounded-2xl mb-4 shadow-lg">
            <EkoLogo variant="full" height={44} />
          </div>
          <h1 className="text-2xl font-bold text-white tracking-tight">Eko Bharat Ventures</h1>
          <p className="text-white/60 text-sm mt-1">Reconciliation Portal</p>
        </div>

        <div className="bg-white rounded-2xl shadow-2xl ring-1 ring-black/5 p-8">
          {step === 'creds' ? (
            <>
              <h2 className="text-lg font-semibold text-gray-800 mb-6">Sign in to your account</h2>
              <form onSubmit={submitCreds} className="space-y-4">
                <div>
                  <label className="text-sm font-medium text-gray-600 block mb-1">Username</label>
                  <input className="input" placeholder="Enter username" value={form.username}
                    onChange={e => setForm({ ...form, username: e.target.value })} required autoFocus />
                </div>
                <div>
                  <label className="text-sm font-medium text-gray-600 block mb-1">Password</label>
                  <div className="relative">
                    <input className="input pr-10" type={showPass ? 'text' : 'password'} placeholder="Enter password"
                      value={form.password} onChange={e => setForm({ ...form, password: e.target.value })} required />
                    <button type="button" onClick={() => setShowPass(!showPass)}
                      className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600">
                      {showPass ? <EyeOff size={16} /> : <Eye size={16} />}
                    </button>
                  </div>
                </div>
                <button type="submit" disabled={loading} className="btn-primary w-full py-2.5 mt-2 inline-flex items-center justify-center gap-2">
                  {loading && <Loader2 size={15} className="animate-spin" />}
                  {loading ? 'Signing in...' : 'Sign In'}
                </button>
              </form>
            </>
          ) : (
            <>
              <h2 className="text-lg font-semibold text-gray-800 mb-1">Two-factor verification</h2>
              <p className="text-xs text-gray-500 mb-5">We emailed a 6-digit code to <strong>{emailHint}</strong>. It expires in 10 minutes.</p>
              <form onSubmit={submitOtp} className="space-y-4">
                <div>
                  <label className="text-sm font-medium text-gray-600 block mb-1">6-digit code</label>
                  <div className="relative">
                    <KeyRound size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
                    <input className="input pl-9 tracking-[0.4em] font-semibold text-center" inputMode="numeric"
                      maxLength={6} placeholder="••••••" autoFocus
                      value={code} onChange={e => setCode(e.target.value.replace(/\D/g, ''))} required />
                  </div>
                </div>
                <label className="flex items-center gap-2 text-xs text-gray-600 cursor-pointer">
                  <input type="checkbox" className="w-4 h-4 rounded accent-primary cursor-pointer"
                    checked={remember} onChange={e => setRemember(e.target.checked)} />
                  Trust this device for 1 day (skip the code next time)
                </label>
                <button type="submit" disabled={loading} className="btn-primary w-full py-2.5 inline-flex items-center justify-center gap-2">
                  {loading && <Loader2 size={15} className="animate-spin" />}
                  {loading ? 'Verifying...' : 'Verify & sign in'}
                </button>
                <button type="button" onClick={backToCreds}
                  className="w-full text-xs text-gray-500 hover:text-gray-700 inline-flex items-center justify-center gap-1">
                  <ArrowLeft size={12} /> Back
                </button>
              </form>
            </>
          )}
        </div>
        <p className="text-center text-white/40 text-xs mt-6">Eko Bharat Ventures · Reconciliation Platform</p>
      </div>
    </div>
  )
}
