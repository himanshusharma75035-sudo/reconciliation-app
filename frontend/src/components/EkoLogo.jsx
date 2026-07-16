// ─────────────────────────────────────────────────────────────────────────────
// Eko brand mark — inline SVG recreation of the "eko" sunrise wordmark.
//
// SVG (not a raster asset) so it stays crisp at any size, themes with the brand
// amber (#F9AB10, tailwind `accent`), and ships inside the JS bundle — no extra
// file to deploy or 404. Two shapes:
//   variant="full" → sun rays + the "eko" wordmark  (login, wide spaces)
//   variant="mark" → the rising-sun mark only        (collapsed sidebar, favicon)
//
// TO USE THE OFFICIAL ASSET INSTEAD: drop the real file at
// frontend/public/eko-logo.svg and render <img src="/eko-logo.svg" .../> in place
// of <EkoLogo/> — every call site updates from that one swap. Until then this is a
// faithful stand-in, not the exact registered logo.
// ─────────────────────────────────────────────────────────────────────────────
const BRAND = '#F9AB10'
const RAYS = [-72, -48, -24, 0, 24, 48, 72]   // degrees from straight-up, fanned

export default function EkoLogo({ variant = 'full', height = 40, color = BRAND, className = '' }) {
  if (variant === 'mark') {
    const cx = 24, cy = 27
    return (
      <svg height={height} viewBox="0 0 48 48" className={className} role="img" aria-label="Eko">
        <g stroke={color} strokeWidth="3.2" strokeLinecap="round">
          {RAYS.map(a => {
            const r = (a * Math.PI) / 180, r1 = 12, r2 = a === 0 ? 20 : 18
            return <line key={a}
              x1={cx + r1 * Math.sin(r)} y1={cy - r1 * Math.cos(r)}
              x2={cx + r2 * Math.sin(r)} y2={cy - r2 * Math.cos(r)} />
          })}
        </g>
        {/* rising-sun dome */}
        <path d={`M ${cx - 11} ${cy + 3} a 11 11 0 0 1 22 0 Z`} fill={color} />
      </svg>
    )
  }

  const cx = 66, cy = 20
  return (
    <svg height={height} viewBox="0 0 132 56" className={className} role="img" aria-label="eko">
      <g stroke={color} strokeWidth="3" strokeLinecap="round">
        {RAYS.map(a => {
          const r = (a * Math.PI) / 180, r1 = 5, r2 = a === 0 ? 16 : 14
          return <line key={a}
            x1={cx + r1 * Math.sin(r)} y1={cy - r1 * Math.cos(r)}
            x2={cx + r2 * Math.sin(r)} y2={cy - r2 * Math.cos(r)} />
        })}
      </g>
      <text x="66" y="52" textAnchor="middle" fill={color}
        fontFamily="'Baloo 2','Nunito','Quicksand',system-ui,-apple-system,sans-serif"
        fontWeight="800" fontSize="36" letterSpacing="-1.5">eko</text>
    </svg>
  )
}
