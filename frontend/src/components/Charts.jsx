// Lightweight dependency-free SVG charts (bar / line / pie / donut).
// Responsive via viewBox (width:100%, fixed aspect). Native <title> tooltips.
// Shared shape: categories = [labels], series = [{ name, color, values:[] }].
import { useState } from 'react'

export const PALETTE = ['#10b981', '#ef4444', '#f59e0b', '#6366f1', '#06b6d4',
  '#8b5cf6', '#ec4899', '#84cc16', '#f97316', '#14b8a6', '#64748b']

const W = 720
const fmtN = n => Number(n || 0).toLocaleString('en-IN')
const short = n => {
  n = Number(n || 0)
  if (Math.abs(n) >= 1e7) return (n / 1e7).toFixed(2) + 'Cr'
  if (Math.abs(n) >= 1e5) return (n / 1e5).toFixed(2) + 'L'
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'k'
  return String(n)
}

// ── Vertical / horizontal bar (grouped · stacked · 100%-normalized share) ──────
export function BarChart({ categories, series, height = 240, horizontal = false, stacked = false, normalize = false, valueFmt = fmtN }) {
  const isStacked = stacked || normalize        // normalize implies a single stacked bar per category
  const H = height
  const padL = horizontal ? 118 : 46, padR = 16, padT = 10, padB = horizontal ? 24 : 46
  const plotW = W - padL - padR, plotH = H - padT - padB
  const n = categories.length || 1
  const catTotal = i => Math.max(1, series.reduce((s, se) => s + (se.values[i] || 0), 0))
  const maxVal = isStacked
    ? Math.max(1, ...categories.map((_, i) => series.reduce((s, se) => s + (se.values[i] || 0), 0)))
    : Math.max(1, ...series.flatMap(se => se.values))
  const band = (horizontal ? plotH : plotW) / n
  const inner = band * (horizontal ? 0.6 : 0.66)   // slimmer bars → more whitespace, less bulk
  const groupW = isStacked ? inner : inner / series.length
  const dim = horizontal ? plotW : plotH
  const lenOf = (val, i) => normalize ? (dim / catTotal(i)) * val : (dim / maxVal) * val
  const ticks = 4
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto' }} role="img">
      {/* gridlines + axis labels (percentages in normalized mode) */}
      {Array.from({ length: ticks + 1 }).map((_, t) => {
        const frac = t / ticks
        const lbl = normalize ? `${t * 25}%` : short((maxVal / ticks) * t)
        if (horizontal) {
          const x = padL + plotW * frac
          return <g key={t}>
            <line x1={x} y1={padT} x2={x} y2={padT + plotH} stroke="#f1f5f9" />
            <text x={x} y={padT + plotH + 12} fontSize="9.5" fill="#a3adba" textAnchor="middle">{lbl}</text>
          </g>
        }
        const y = padT + plotH - plotH * frac
        return <g key={t}>
          <line x1={padL} y1={y} x2={padL + plotW} y2={y} stroke="#f1f5f9" />
          <text x={padL - 6} y={y + 3} fontSize="9.5" fill="#a3adba" textAnchor="end">{lbl}</text>
        </g>
      })}
      {categories.map((cat, i) => {
        const base = (horizontal ? padT : padL) + band * i + (band - inner) / 2
        let acc = 0
        return <g key={i}>
          {series.map((se, si) => {
            const val = se.values[i] || 0
            const len = lenOf(val, i)
            const pct = normalize ? ` (${Math.round(val / catTotal(i) * 100)}%)` : ''
            if (horizontal) {
              const y = base + (isStacked ? 0 : groupW * si)
              const x = padL + (isStacked ? lenOf(acc, i) : 0)
              isStacked && (acc += val)
              return <rect key={si} x={x} y={y} width={Math.max(0, len)} height={isStacked ? inner : Math.max(1, groupW - 2)}
                rx="2.5" fill={se.colors ? se.colors[i] : se.color} className="bar-h"
                style={{ animationDelay: `${Math.min(i, 24) * 0.03}s` }}><title>{`${cat} · ${se.name}: ${valueFmt(val)}${pct}`}</title></rect>
            }
            const x = base + (isStacked ? 0 : groupW * si)
            const y = padT + plotH - len - (isStacked ? lenOf(acc, i) : 0)
            isStacked && (acc += val)
            return <rect key={si} x={x} y={y} width={isStacked ? inner : Math.max(1, groupW - 2)} height={Math.max(0, len)}
              rx="2.5" fill={se.colors ? se.colors[i] : se.color} className="bar-v"
              style={{ animationDelay: `${Math.min(i, 24) * 0.03}s` }}><title>{`${cat} · ${se.name}: ${valueFmt(val)}${pct}`}</title></rect>
          })}
          {horizontal
            ? <text x={padL - 6} y={base + inner / 2 + 3} fontSize="10" fill="#475569" textAnchor="end">{cat}</text>
            : <text x={base + inner / 2} y={padT + plotH + 14} fontSize="9.5" fill="#475569"
                textAnchor={n > 8 ? 'start' : 'middle'}
                transform={n > 8 ? `rotate(35 ${base + inner / 2} ${padT + plotH + 14})` : undefined}>{cat}</text>}
        </g>
      })}
    </svg>
  )
}

// ── Multi-series line ─────────────────────────────────────────────────────────
export function LineChart({ categories, series, height = 240, valueFmt = fmtN }) {
  const H = height, padL = 46, padR = 16, padT = 10, padB = 46
  const plotW = W - padL - padR, plotH = H - padT - padB
  const n = categories.length || 1
  const maxVal = Math.max(1, ...series.flatMap(se => se.values))
  const x = i => padL + (n === 1 ? plotW / 2 : (plotW / (n - 1)) * i)
  const y = v => padT + plotH - (plotH / maxVal) * v
  const ticks = 4
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto' }} role="img">
      {Array.from({ length: ticks + 1 }).map((_, t) => {
        const v = (maxVal / ticks) * t, yy = y(v)
        return <g key={t}>
          <line x1={padL} y1={yy} x2={padL + plotW} y2={yy} stroke="#eef2f7" />
          <text x={padL - 6} y={yy + 3} fontSize="10" fill="#94a3b8" textAnchor="end">{short(v)}</text>
        </g>
      })}
      {series.map((se, si) => (
        <g key={si}>
          <polyline fill="none" stroke={se.color} strokeWidth="2.5" className="line-anim" pathLength="1"
            points={se.values.map((v, i) => `${x(i)},${y(v)}`).join(' ')} />
          {se.values.map((v, i) => (
            <circle key={i} cx={x(i)} cy={y(v)} r="3" fill={se.color} className="dot-anim"
              style={{ animationDelay: `${0.2 + Math.min(i, 24) * 0.02}s` }}>
              <title>{`${categories[i]} · ${se.name}: ${valueFmt(v)}`}</title></circle>
          ))}
        </g>
      ))}
      {categories.map((cat, i) => (
        <text key={i} x={x(i)} y={padT + plotH + 14} fontSize="10" fill="#475569"
          textAnchor={n > 8 ? 'start' : 'middle'}
          transform={n > 8 ? `rotate(35 ${x(i)} ${padT + plotH + 14})` : undefined}>{cat}</text>
      ))}
    </svg>
  )
}

// ── Pie / donut ───────────────────────────────────────────────────────────────
export function PieChart({ slices, donut = false, height = 280, valueFmt = fmtN }) {
  const total = slices.reduce((s, x) => s + (x.value || 0), 0) || 1
  const cx = 150, cy = height / 2, r = Math.min(cy, 150) - 8, ir = donut ? r * 0.58 : 0
  let a0 = -Math.PI / 2
  const arc = (a, b, rad) => [rad * Math.cos(a) + cx, rad * Math.sin(a) + cy, rad * Math.cos(b) + cx, rad * Math.sin(b) + cy]
  return (
    <svg viewBox={`0 0 ${W} ${height}`} style={{ width: '100%', height: 'auto' }} role="img">
      {slices.map((s, i) => {
        const frac = (s.value || 0) / total
        const a1 = a0 + frac * 2 * Math.PI
        const large = frac > 0.5 ? 1 : 0
        const [x0, y0, x1, y1] = arc(a0, a1, r)
        let d
        if (donut) {
          const [ix0, iy0, ix1, iy1] = arc(a0, a1, ir)
          d = `M ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1} L ${ix1} ${iy1} A ${ir} ${ir} 0 ${large} 0 ${ix0} ${iy0} Z`
        } else {
          d = `M ${cx} ${cy} L ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1} Z`
        }
        a0 = a1
        return <path key={i} d={d} fill={s.color} stroke="#fff" strokeWidth="1.5" className="slice-anim"
          style={{ animationDelay: `${Math.min(i, 12) * 0.05}s` }}>
          <title>{`${s.label}: ${valueFmt(s.value)} (${(frac * 100).toFixed(1)}%)`}</title></path>
      })}
      {/* legend — label and value on SEPARATE lines so long labels can never
          collide with their number (row height fixed, so rows never overlap). */}
      {slices.map((s, i) => {
        const rowH = 34
        const top = cy - (slices.length * rowH) / 2 + i * rowH
        return (
          <g key={i} transform={`translate(${cx + r + 34}, ${top})`}>
            <rect y="3" width="11" height="11" rx="2" fill={s.color} />
            <text x="18" y="8" fontSize="12" fill="#334155" fontWeight="600">{s.label}</text>
            <text x="18" y="24" fontSize="11" fill="#64748b">
              {valueFmt(s.value)} · {(((s.value || 0) / total) * 100).toFixed(1)}%</text>
          </g>
        )
      })}
    </svg>
  )
}

// ── Card wrapper with a chart-type toggle ─────────────────────────────────────
const TYPE_LABEL = { bar: 'Bar', barh: 'Bar (H)', stacked: 'Stacked', share: '% Share', line: 'Line', pie: 'Pie', donut: 'Donut' }
export function ChartCard({ title, subtitle, types, defaultType, legend, children }) {
  const [type, setType] = useState(defaultType || types[0])
  return (
    <div className="card p-0 overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-gray-100 bg-gray-50/50 flex-wrap">
        <div>
          <h3 className="font-bold text-gray-800 text-sm">{title}</h3>
          {subtitle && <p className="text-[11px] text-gray-400">{subtitle}</p>}
        </div>
        <div className="ml-auto flex items-center gap-1 bg-white border border-gray-200 rounded-lg p-0.5">
          {types.map(t => (
            <button key={t} onClick={() => setType(t)}
              className={`text-[11px] px-2 py-1 rounded-md font-medium transition ${type === t ? 'bg-emerald-600 text-white' : 'text-gray-500 hover:bg-gray-100'}`}>
              {TYPE_LABEL[t] || t}
            </button>
          ))}
        </div>
      </div>
      <div className="p-4">
        <div key={type} className="chart-anim">{children(type)}</div>
        {legend && (
          <div className="flex items-center gap-4 flex-wrap mt-2 justify-center">
            {legend.map((l, i) => (
              <span key={i} className="flex items-center gap-1.5 text-xs text-gray-600">
                <span className="w-3 h-3 rounded-sm inline-block" style={{ background: l.color }} />{l.name}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
