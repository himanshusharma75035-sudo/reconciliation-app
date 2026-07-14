import axios from 'axios'

const api = axios.create({ baseURL: '/api' })

api.interceptors.request.use(config => {
  const token = localStorage.getItem('token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// ── UTC → IST display conversion ─────────────────────────────────────────────
// The backend stores and returns timestamps in UTC (datetime.utcnow), as naive
// strings like "2026-06-09T09:05:42" or "2026-06-09 09:05:42.123456". This tool is
// used only in India, so we render every timestamp in IST (UTC+5:30). We rewrite
// full datetime strings in API responses to an IST wall-clock string
// "YYYY-MM-DD HH:MM:SS". Date-only values (e.g. recon_date "2026-06-09") have no
// time component and are left untouched. Centralising it here means every screen
// shows IST without each page needing its own conversion. Excel exports are built
// server-side and bypass this — they convert to IST in the backend export code.
const _DT_RE = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z?$/
const _pad = n => String(n).padStart(2, '0')

function utcToIST(s) {
  const m = _DT_RE.exec(s)
  if (!m) return s
  const utcMs = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6])
  const d = new Date(utcMs + 330 * 60 * 1000) // +5h30m
  return `${d.getUTCFullYear()}-${_pad(d.getUTCMonth() + 1)}-${_pad(d.getUTCDate())} ` +
         `${_pad(d.getUTCHours())}:${_pad(d.getUTCMinutes())}:${_pad(d.getUTCSeconds())}`
}

function convertDatesToIST(o) {
  if (!o || typeof o !== 'object') return o
  if (Array.isArray(o)) {
    for (let i = 0; i < o.length; i++) {
      if (typeof o[i] === 'string') o[i] = utcToIST(o[i])
      else convertDatesToIST(o[i])
    }
    return o
  }
  for (const k in o) {
    const v = o[k]
    if (typeof v === 'string') o[k] = utcToIST(v)
    else if (v && typeof v === 'object') convertDatesToIST(v)
  }
  return o
}

api.interceptors.response.use(
  res => {
    const rt = res.config?.responseType
    if (rt !== 'blob' && rt !== 'arraybuffer') {
      try { convertDatesToIST(res.data) } catch { /* never break a response over date formatting */ }
    }
    return res
  },
  err => {
    if (err.response?.status === 401) {
      localStorage.removeItem('token')
      localStorage.removeItem('user')
      // BASE_URL carries the nginx subpath (e.g. /recon/) so this lands on
      // /recon/login, not a 404 at the server root. On the public /exec dashboard
      // an expired session should return to the email-code gate (also at /exec),
      // NOT the operator login.
      const onExec = window.location.pathname.replace(/\/+$/, '').endsWith('/exec')
      window.location.href = import.meta.env.BASE_URL + (onExec ? 'exec' : 'login')
    }
    return Promise.reject(err)
  }
)

export default api
