// ─────────────────────────────────────────────────────────────────────────────
// Central filter model — one source of truth for Status options and field
// applicability across Open Items and every product recon window.
//
// As products/recon processes grow, each has its own status vocabulary. Instead
// of one flat DMT-centric dropdown, the Status options are grouped by product
// scope; the UI greys out (disables) the groups and fields that don't apply to
// the currently selected partner.
//
// Backend contract: universal statuses (matched_all / unmatched / amount_mismatch
// / duplicate) are resolved across the core ledger AND module tables (BBPS,
// E-Value) by recon.py's _UNIVERSAL_STATUS map, so they work at Partner = All.
// ─────────────────────────────────────────────────────────────────────────────
import { isCoreLedgerPartner } from './productRegistry'

// scope:
//   'all'    → always selectable (works for every partner, incl. Partner = All)
//   'core'   → DMT / core-ledger products (or Partner = All)
//   'bbps'   → only when BBPS is the selected partner
//   'evalue' → only when E-Value is the selected partner
export const STATUS_GROUPS = [
  {
    label: 'Universal', scope: 'all', options: [
      { v: '',                l: 'Open Items (unmatched + SRC)' },
      { v: 'unmatched',       l: 'Unmatched' },
      { v: 'matched_all',     l: 'Matched' },
      { v: 'amount_mismatch', l: 'Amount Mismatch' },
      { v: 'duplicate',       l: 'Duplicates' },
      { v: 'all',             l: 'All Transactions' },
    ],
  },
  {
    label: 'DMT / Core Ledger', scope: 'core', options: [
      { v: 'src_assigned',      l: 'SRC Assigned' },
      { v: 'fee_matched',       l: 'Fees & Charges' },
      { v: 'fund_transfer',     l: 'Fund Transfers' },
      { v: 'interbank_matched', l: 'Interbank Matched' },
      { v: 'internal_matched',  l: 'Internal Matched' },
      { v: 'adhoc_settlement',  l: 'Adhoc Settlement' },
      { v: 'reversal_matched',  l: 'Reversals' },
      { v: 'human_override',    l: 'Human Override' },
      { v: 'failed',            l: 'Failed (Dump)' },
    ],
  },
  {
    label: 'BBPS', scope: 'bbps', options: [
      { v: 'unmatched_bank',       l: 'Unmatched (Bank)' },
      { v: 'unmatched_internal',   l: 'Unmatched (Internal)' },
      { v: 'failed_pending_refund',l: 'Failed – Pending Refund' },
      { v: 'refunded_but_success', l: 'Refunded but Success' },
      { v: 'src_assigned',         l: 'SRC Assigned' },
    ],
  },
  {
    label: 'E-Value', scope: 'evalue', options: [
      { v: 'unmatched_bank', l: 'Unmatched (Bank)' },
      { v: 'unmatched_load', l: 'Unmatched (Load)' },
      { v: 'twice_credit',   l: 'Twice Credit' },
      { v: 'wrong_amount',   l: 'Wrong Amount' },
      { v: 'src_assigned',   l: 'SRC Assigned' },
    ],
  },
]

// Is a whole status group selectable for the current partner?
export function groupEnabled(scope, partner) {
  if (scope === 'all')  return true
  if (scope === 'core') return !partner || isCoreLedgerPartner(partner)
  return scope === partner   // 'bbps' / 'evalue' → only when that module is selected
}

// Is a specific status value selectable for the current partner?
export function statusEnabled(value, partner) {
  for (const g of STATUS_GROUPS) {
    if (g.options.some(o => o.v === value)) return groupEnabled(g.scope, partner)
  }
  return true
}

// Human label for a status value (first match wins — universal labels take precedence).
export const STATUS_LABEL_BY_VALUE = (() => {
  const m = {}
  for (const g of STATUS_GROUPS) for (const o of g.options) if (!(o.v in m)) m[o.v] = o.l
  return m
})()

// Which scalar filter fields apply to the selected partner.
// SRC now applies to the core ledger AND the open-items module products (E-Value, BBPS),
// which gained assign-src parity. Still N/A for products without dispositionable rows.
export function fieldApplies(field, partner) {
  if (field === 'src_code')
    return !partner || isCoreLedgerPartner(partner) || partner === 'evalue' || partner === 'bbps'
  return true
}

// Flat status list for a single-product CORE recon window (ProductReconPage).
// Mirrors the Universal + DMT groups (the only ones a core product can be in).
export const CORE_RECON_STATUS_OPTS = [
  { v: 'all',             l: 'All Status' },
  { v: '',                l: 'Open items only' },
  { v: 'matched_all',     l: 'Matched' },
  { v: 'amount_mismatch', l: 'Amount mismatch' },
  { v: 'src_assigned',    l: 'SRC assigned' },
  { v: 'fee_matched',     l: 'Fees & charges' },
  { v: 'fund_transfer',   l: 'Fund transfers' },
  { v: 'interbank_matched', l: 'Interbank matched' },
  { v: 'internal_matched',  l: 'Internal matched' },
  { v: 'adhoc_settlement',  l: 'Adhoc settlement' },
  { v: 'reversal_matched',  l: 'Reversals' },
  { v: 'human_override',    l: 'Human override' },
  { v: 'duplicate',       l: 'Duplicates' },
  { v: 'failed',          l: 'Failed (dump)' },
]
