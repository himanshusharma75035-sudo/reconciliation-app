// SRC (source / disposition) reason codes.
//
// The codes are now a MANAGED CATALOG in the backend (SrcCode table, editable in
// Configuration → SRC Codes). The array below is only a fallback used before the
// live list loads or if the request fails. Prefer the useSrcCodes() hook, which
// fetches the active catalog from GET /api/recon/src-codes so codes an admin
// creates show up in every dropdown without a redeploy.
import { useState, useEffect } from 'react'
import api from './utils/api'

export const SRC_CODES = [
  'UNCLAIMED', 'ADVANCE_CREDIT', 'BANK_CHARGES', 'TWICE_CREDITED',
  'INTERNAL_TXN', 'DELAYED_TXN', 'DUPLICATE', 'MISSING_TID', 'OTHER',
]

// Module-level cache so a code fetched once is reused across mounts (and is fresh
// for non-reactive consumers that read it after first load).
let _cache = null

export function getCachedSrcCodes() {
  return _cache || SRC_CODES
}

// React hook — returns the live active catalog (falls back to the static list).
export function useSrcCodes() {
  const [codes, setCodes] = useState(_cache || SRC_CODES)
  useEffect(() => {
    let alive = true
    api.get('/recon/src-codes')
      .then(({ data }) => { if (alive && Array.isArray(data) && data.length) { _cache = data; setCodes(data) } })
      .catch(() => {})
    return () => { alive = false }
  }, [])
  return codes
}
