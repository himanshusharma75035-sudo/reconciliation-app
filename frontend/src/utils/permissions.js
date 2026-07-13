/**
 * Permission helpers — read from the JWT user object stored in localStorage.
 * The backend enforces every permission server-side; these helpers are for
 * hiding write actions in the UI so read-only users have a clean experience.
 */

export function getUser() {
  try { return JSON.parse(localStorage.getItem('user') || '{}') } catch { return {} }
}

/**
 * Returns true if the current user has the given permission.
 * Admins always return true regardless of the permission list.
 */
export function hasPermission(permission) {
  const user = getUser()
  if (!user?.username) return false
  if (user.role === 'admin') return true
  return !!(user.permissions?.[permission])
}

export function isAdmin() {
  return getUser().role === 'admin'
}

/**
 * Dashboard-only account: role 'viewer' can ONLY see the executive analytics
 * dashboard (the /exec route). Enforced server-side; the app redirects these
 * users to /exec and hides the rest of the UI.
 */
export function isViewer() {
  return getUser().role === 'viewer'
}

/**
 * Product access: user.allowed_products is a list of product ids.
 * Empty list / missing = access to ALL products. Admins always see everything.
 * (Backend enforces this on run-recon; the UI hides inaccessible products.)
 */
export function canAccessProduct(productId) {
  const user = getUser()
  if (!user?.username || user.role === 'admin') return true
  const allowed = user.allowed_products || []
  if (!Array.isArray(allowed) || allowed.length === 0) return true
  return allowed.includes(productId)
}

/** Filter a registry-style product list down to what this user may access. */
export function filterProductsByAccess(products) {
  return (products || []).filter(p => canAccessProduct(p.id))
}
