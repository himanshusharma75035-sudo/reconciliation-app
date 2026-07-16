// ─────────────────────────────────────────────────────────────────────────────
// SINGLE SOURCE OF TRUTH for products.
//
// Add a product here ONCE and it surfaces everywhere that imports this file:
//   • Upload product grid (the launcher)
//   • Dedicated per-product recon window (ProductReconPage)
//   • Dashboard product cards
//
// `kind`:
//   'core'      → reconciled by the generic engine. Opens ProductReconPage
//                 (Upload + Reconciliation [+ Settlement] tabs) at /product/<id>.
//   'module'    → has its own bespoke page/engine. Opens its own route.
//
// `settlement`: 'aeps' | 'qr' | null  — adds a Settlement tab inside the window
//               (only meaningful for core products).
//
// `partners`:   partner slugs this product reconciles (drives the in-window
//               Run Reconciliation partner picker). Multi-partner = DMT banks.
//
// Everything that is partner-driven (Reports dropdown, Run Recon, Configuration
// Partners / Format Presets / Matching Rules) is ALREADY dynamic — it reads
// /admin/partners-public, so seeding a partner on the backend auto-populates it.
// This registry only covers the bespoke front-end product UIs.
// ─────────────────────────────────────────────────────────────────────────────

export const PRODUCTS = [
  { id: 'dmt',       label: 'DMT',                 icon: '💸', color: 'blue',   kind: 'core',   route: '/product/dmt',       partners: ['fino', 'airtel', 'axis', 'levin'], settlement: null,
    desc: 'Domestic Money Transfer — Fino, Airtel, Axis & Levin. Upload + Reconciliation.' },
  { id: 'aeps',      label: 'AePS Cashout',        icon: '🏧', color: 'green',  kind: 'core',   route: '/product/aeps',      partners: ['aeps'],       settlement: 'aeps',
    desc: 'AePS Cashout — Upload, Reconciliation & T+ Settlement, all in one window.' },
  { id: 'pg',        label: 'Accept Payment (PG)', icon: '💳', color: 'purple', kind: 'core',   route: '/product/pg',        partners: ['pg'],         settlement: null,
    desc: 'Payment Gateway — PayU settlement vs Simplibank dump. Upload + Reconciliation.' },
  { id: 'qr',        label: 'QR Collection',       icon: '📱', color: 'cyan',   kind: 'core',   route: '/product/qr',        partners: ['qr'],         settlement: 'qr',
    desc: 'QR payments — Upload, Reconciliation & Settlement, all in one window.' },
  { id: 'digikhata', label: 'Digikhata (PPI)',     icon: '👛', color: 'orange', kind: 'core',   route: '/product/digikhata', partners: ['digikhata'],  settlement: null,
    desc: 'PPI Wallet Load — Digikhata bank report vs Simplibank dump. Upload + Reconciliation.' },
  { id: 'indonepal', label: 'Indo-Nepal',          icon: '🌏', color: 'teal',   kind: 'core',   route: '/product/indonepal', partners: ['indonepal'],  settlement: null,
    desc: 'Indo-Nepal Money Transfer — Indo-Nepal report vs Simplibank dump. Upload + Reconciliation.' },
  { id: 'kiosk',     label: 'SBI Kiosk',           icon: '🏦', color: 'rose',   kind: 'module', route: '/sbi-kiosk',         partners: ['sbi'],        settlement: null,
    desc: 'SBI Kiosk Banking — opens the SBI Kiosk Recon window (4 processes).' },
  { id: 'evalue',    label: 'E-Value',             icon: '👛', color: 'cyan',   kind: 'module', route: '/evalue',            partners: ['evalue'],     settlement: null,
    desc: 'CSP wallet loading — opens the E-Value Recon window (8 banks).' },
  { id: 'bbps',      label: 'BBPS',                icon: '🧾', color: 'purple', kind: 'module', route: '/bbps',              partners: ['bbps'],       settlement: null,
    desc: 'Bill payment / recharge — opens the BBPS Recon window (Moneyart & Levin).' },
]

// Lookup by id, e.g. PRODUCT_BY_ID['aeps']
export const PRODUCT_BY_ID = PRODUCTS.reduce((acc, p) => { acc[p.id] = p; return acc }, {})

// E-Value, BBPS and SBI Kiosk reconcile in their own tables — NOT the core
// transactions ledger. Pages that read the core ledger (Run Recon, Open Items,
// Manual Match, Mismatches, core Reports, Logic Builder, Insights, Fee Rules)
// must exclude them from partner dropdowns or they show misleading zeros.
const MODULE_PRODUCT_KEYS = ['kiosk', 'evalue', 'bbps']
const MODULE_PARTNER_SLUGS = ['kiosk', 'sbi', 'evalue', 'bbps']
export const isCoreLedgerPartner = (p) =>
  !MODULE_PRODUCT_KEYS.includes(p?.product) && !MODULE_PARTNER_SLUGS.includes(p?.slug)

// Open Items is the universal exceptions window — it ALSO surfaces BBPS and
// E-Value unmatched rows via the backend adapter. SBI Kiosk reconciles by
// process (P01–P04), not bank↔internal, so it stays in its own window.
const OPEN_ITEMS_EXCLUDE = ['kiosk', 'sbi']
export const isOpenItemsPartner = (p) =>
  !OPEN_ITEMS_EXCLUDE.includes(p?.product) && !OPEN_ITEMS_EXCLUDE.includes(p?.slug)

// Just the core products that use ProductReconPage
export const CORE_PRODUCTS = PRODUCTS.filter(p => p.kind === 'core')

// DMT member banks — the single list 'dmt' fans out to. Import this instead of
// re-hardcoding ['fino','airtel','axis','levin'] in Run Recon, Reports, Dashboard,
// ProductReconPage, Auto Upload, etc. (backend mirror: routes/reports.py PRODUCT_PARTNERS).
export const DMT_PARTNERS = PRODUCT_BY_ID.dmt.partners

// The match fields the core matching engine actually keys on (matching_engine.DEFAULT_RULES
// only ever uses these). Single source for BOTH rule editors — the Logic Builder and the
// Admin Matching Rules tab — which previously exposed different palettes. ('status' was a
// spurious option: the engine pairs rows by identifier/amount, never by status.)
export const MATCH_FIELDS = ['eko_tid', 'tracking_number', 'utr_number', 'amount']
