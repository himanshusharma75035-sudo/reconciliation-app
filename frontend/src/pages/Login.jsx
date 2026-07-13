import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Scale, Eye, EyeOff, Loader2 } from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../utils/api'

export default function Login() {
  const [form, setForm] = useState({ username: '', password: '' })
  const [showPass, setShowPass] = useState(false)
  const [loading, setLoading] = useState(false)
  const navigate = useNavigate()

  const handleSubmit = async (e) => {
    e.preventDefault()
    setLoading(true)
    try {
      const params = new URLSearchParams()
      params.append('username', form.username)
      params.append('password', form.password)
      const { data } = await api.post('/auth/login', params, {
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
      })
      localStorage.setItem('token', data.access_token)
      localStorage.setItem('user', JSON.stringify(data.user))
      toast.success(`Welcome, ${data.user.full_name || data.user.username}`)
      navigate(data.user.role === 'viewer' ? '/exec' : '/dashboard')
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="relative min-h-screen bg-gradient-to-br from-primary via-primary to-primary-dark flex items-center justify-center p-4 overflow-hidden">
      {/* Decorative glow accents */}
      <div className="pointer-events-none absolute -top-32 -left-32 w-96 h-96 rounded-full bg-accent/10 blur-3xl" />
      <div className="pointer-events-none absolute -bottom-40 -right-24 w-[28rem] h-[28rem] rounded-full bg-primary-400/20 blur-3xl" />

      <div className="relative w-full max-w-md animate-fade-in">
        {/* Logo */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-16 h-16 bg-accent/15 ring-1 ring-accent/30 rounded-2xl mb-4 shadow-lg">
            <Scale size={30} className="text-accent" />
          </div>
          <h1 className="text-2xl font-bold text-white tracking-tight">Eko Bharat Ventures</h1>
          <p className="text-white/60 text-sm mt-1">Reconciliation Portal</p>
        </div>

        {/* Card */}
        <div className="bg-white rounded-2xl shadow-2xl ring-1 ring-black/5 p-8">
          <h2 className="text-lg font-semibold text-gray-800 mb-6">Sign in to your account</h2>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="text-sm font-medium text-gray-600 block mb-1">Username</label>
              <input
                className="input"
                placeholder="Enter username"
                value={form.username}
                onChange={e => setForm({ ...form, username: e.target.value })}
                required autoFocus
              />
            </div>
            <div>
              <label className="text-sm font-medium text-gray-600 block mb-1">Password</label>
              <div className="relative">
                <input
                  className="input pr-10"
                  type={showPass ? 'text' : 'password'}
                  placeholder="Enter password"
                  value={form.password}
                  onChange={e => setForm({ ...form, password: e.target.value })}
                  required
                />
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
        </div>
        <p className="text-center text-white/40 text-xs mt-6">Eko Bharat Ventures · Reconciliation Platform</p>
      </div>
    </div>
  )
}
