// Single source of truth for SRC (source / disposition) codes on the frontend.
// Mirrors the backend canonical list in backend/routes/recon.py (SRC_CODES), which is
// also served at GET /api/recon/src-codes and is the authority for validation.
// Import { SRC_CODES } from here instead of re-hardcoding the array in each page.
export const SRC_CODES = [
  'UNCLAIMED', 'ADVANCE_CREDIT', 'BANK_CHARGES', 'TWICE_CREDITED',
  'INTERNAL_TXN', 'DELAYED_TXN', 'DUPLICATE', 'MISSING_TID', 'OTHER',
]
