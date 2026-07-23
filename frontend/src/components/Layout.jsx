import React, { useState } from 'react'
import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import {
  LayoutDashboard, Upload, Play, AlertTriangle, GitMerge,
  BarChart2, Users, LogOut, ChevronLeft, ChevronRight, BookOpen, Scale, Shield, AlertOctagon, Zap, Settings, TrendingUp, Activity, Code2, PieChart, Trash2, Palette
} from 'lucide-react'
import { hasPermission, isAdmin } from '../utils/permissions'
import EkoLogo from './EkoLogo'
import { useTheme } from '../ThemeContext'
import { THEMES } from '../themes'

// Small per-user theme picker for the sidebar footer — swatch buttons, one per theme.
function ThemePicker({ collapsed }) {
  const { theme, setTheme } = useTheme()
  const [open, setOpen] = useState(false)
  return (
    <div className="relative">
      <button
        onClick={() => setOpen(o => !o)}
        title="Theme"
        className={`flex items-center gap-2 rounded-lg px-2 py-1.5 w-full text-xs
                    text-white/60 hover:text-white hover:bg-white/10 transition-colors
                    ${collapsed ? 'justify-center' : ''}`}
      >
        <Palette size={15} />
        {!collapsed && <>Theme <span className="ml-auto text-white/40 capitalize">{THEMES.find(t => t.id === theme)?.label}</span></>}
      </button>
      {open && (
        <div className="absolute bottom-full left-0 mb-1 w-44 rounded-lg bg-white shadow-xl border border-gray-100 p-2 z-50">
          {THEMES.map(t => (
            <button key={t.id}
              onClick={() => { setTheme(t.id); setOpen(false) }}
              className={`flex items-center gap-2 w-full px-2 py-1.5 rounded-md text-xs text-left transition-colors
                          ${theme === t.id ? 'bg-gray-100 font-semibold text-gray-800' : 'text-gray-600 hover:bg-gray-50'}`}>
              <span className="inline-flex -space-x-1">
                <span className="w-3.5 h-3.5 rounded-full ring-1 ring-white" style={{ background: t.primary }} />
                <span className="w-3.5 h-3.5 rounded-full ring-1 ring-white" style={{ background: t.accent }} />
              </span>
              {t.label}
              {theme === t.id && <span className="ml-auto text-emerald-500">✓</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// requiredPerm: nav item is hidden if user doesn't have this permission
// adminOnly:    nav item is hidden for non-admins
const navItems = [
  { to: '/dashboard',    icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/analytics',    icon: PieChart,        label: 'Analytics' },
  { to: '/upload',       icon: Upload,          label: 'Upload Files',  requiredPerm: 'upload' },
  { to: '/auto-upload',  icon: Zap,             label: 'Auto Upload',   requiredPerm: 'upload' },
  { to: '/ingestion-monitor', icon: Activity,   label: 'Ingestion Monitor', requiredPerm: 'upload' },
  { to: '/run-recon',    icon: Play,            label: 'Run Recon',     requiredPerm: 'run_recon' },
  { to: '/open-items',   icon: AlertTriangle,   label: 'Open Items' },
  { to: '/mismatches',   icon: AlertOctagon,    label: 'Mismatches' },
  { to: '/manual-match', icon: GitMerge,        label: 'Manual Match',  requiredPerm: 'manual_match' },
  { to: '/reports',      icon: BarChart2,       label: 'Reports',       requiredPerm: 'reports' },
  { to: '/rules',        icon: BookOpen,        label: 'Logic Builder', requiredPerm: 'logic_builder' },
  // Per-product recon windows (AePS, QR, SBI Kiosk, E-Value, BBPS) open from the
  // product cards in Upload Files — intentionally NOT in the sidebar to keep it clean.
  { to: '/workflow',     icon: Scale,           label: 'Workflow' },
  { to: '/insights',     icon: TrendingUp,      label: 'Insights' },
  { to: '/developer-portal', icon: Code2,       label: 'Developer Portal', requiredPerm: 'portal_access' },
  { to: '/recycle-bin',  icon: Trash2,          label: 'Recycle Bin',   requiredPerm: 'clear_data' },
  { to: '/audit-log',    icon: Shield,          label: 'Audit Log',     requiredPerm: 'audit_read' },
  { to: '/users',        icon: Users,           label: 'Users',         adminOnly: true },
  { to: '/admin',        icon: Settings,        label: 'Configuration', adminOnly: true },
]

function initials(name = '') {
  const parts = String(name).trim().split(/\s+/).filter(Boolean)
  if (!parts.length) return '?'
  return (parts[0][0] + (parts[1]?.[0] || '')).toUpperCase()
}

export default function Layout() {
  const [collapsed, setCollapsed] = useState(false)
  const navigate = useNavigate()
  const user = JSON.parse(localStorage.getItem('user') || '{}')

  const handleLogout = () => {
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    navigate('/login')
  }

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar */}
      <aside
        className={`${collapsed ? 'w-[68px]' : 'w-60'} bg-gradient-to-b from-primary to-primary-dark
                    flex flex-col transition-all duration-200 flex-shrink-0 relative`}
      >
        {/* Logo */}
        <div className={`flex items-center gap-3 px-4 py-4 border-b border-white/10 ${collapsed ? 'justify-center px-2' : ''}`}>
          <div className="w-9 h-9 rounded-xl bg-accent/15 ring-1 ring-accent/30 flex items-center justify-center flex-shrink-0">
            <EkoLogo variant="mark" height={22} />
          </div>
          {!collapsed && (
            <div className="min-w-0 leading-tight">
              <div className="text-white font-bold text-[13px] tracking-wide truncate">Eko Bharat Ventures</div>
              <div className="text-white/50 text-[10.5px] uppercase tracking-widest">Reconciliation</div>
            </div>
          )}
        </div>

        {/* Nav */}
        <nav className="flex-1 py-3 px-2 overflow-y-auto overflow-x-hidden space-y-0.5">
          {navItems.map(({ to, icon: Icon, label, adminOnly, requiredPerm }) => {
            if (adminOnly && user.role !== 'admin') return null
            if (requiredPerm && !hasPermission(requiredPerm)) return null
            return (
              <NavLink
                key={to}
                to={to}
                title={collapsed ? label : undefined}
                className={({ isActive }) =>
                  `group relative flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition-colors
                   ${collapsed ? 'justify-center px-0' : ''}
                   ${isActive
                     ? 'bg-white/15 text-white font-medium shadow-sm'
                     : 'text-white/65 hover:bg-white/10 hover:text-white'}`
                }
              >
                {({ isActive }) => (
                  <>
                    {isActive && (
                      <span className="absolute left-0 top-1/2 -translate-y-1/2 h-5 w-1 rounded-r-full bg-accent" />
                    )}
                    <Icon size={18} className="flex-shrink-0" />
                    {!collapsed && <span className="truncate">{label}</span>}
                  </>
                )}
              </NavLink>
            )
          })}
        </nav>

        {/* Bottom — user chip + logout + collapse */}
        <div className="border-t border-white/10 p-3 space-y-2">
          <div className={`flex items-center gap-2.5 ${collapsed ? 'justify-center' : ''}`}>
            <div
              className="w-8 h-8 rounded-full bg-accent/20 ring-1 ring-accent/40 text-accent
                         flex items-center justify-center text-[11px] font-bold flex-shrink-0"
              title={user.full_name || user.username}
            >
              {initials(user.full_name || user.username)}
            </div>
            {!collapsed && (
              <div className="min-w-0 text-xs leading-tight">
                <div className="font-medium text-white/90 truncate">{user.full_name || user.username}</div>
                <div className="text-white/50 capitalize">{user.role}</div>
              </div>
            )}
          </div>
          <ThemePicker collapsed={collapsed} />
          <button
            onClick={handleLogout}
            title="Logout"
            className={`flex items-center gap-2 rounded-lg px-2 py-1.5 w-full text-xs
                        text-white/60 hover:text-white hover:bg-white/10 transition-colors
                        ${collapsed ? 'justify-center' : ''}`}
          >
            <LogOut size={15} />
            {!collapsed && 'Logout'}
          </button>
          <button
            onClick={() => setCollapsed(!collapsed)}
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            className={`flex items-center gap-2 rounded-lg px-2 py-1.5 w-full text-xs
                        text-white/40 hover:text-white hover:bg-white/10 transition-colors
                        ${collapsed ? 'justify-center' : ''}`}
          >
            {collapsed ? <ChevronRight size={15} /> : <ChevronLeft size={15} />}
            {!collapsed && 'Collapse'}
          </button>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-y-auto bg-surface">
        <div className="p-6 max-w-7xl mx-auto animate-fade-in">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
