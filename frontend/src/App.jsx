import React from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Upload from './pages/Upload'
import RunRecon from './pages/RunRecon'
import OpenItems from './pages/OpenItems'
import ManualMatch from './pages/ManualMatch'
import Reports from './pages/Reports'
import Rules from './pages/Rules'
import Users from './pages/Users'
import AuditLog from './pages/AuditLog'
import Mismatches from './pages/Mismatches'
import AutoUpload from './pages/AutoUpload'
import Admin from './pages/Admin'
import AepsSettlement from './pages/AepsSettlement'
import QRSettlement   from './pages/QRSettlement'
import SBIKiosk       from './pages/SBIKiosk'
import Evalue         from './pages/Evalue'
import Insights       from './pages/Insights'
import Analytics      from './pages/Analytics'
import PublicDashboard from './pages/PublicDashboard'
import Bbps           from './pages/Bbps'
import ProductReconPage from './pages/ProductReconPage'
import Workflow from './pages/Workflow'
import IngestionMonitor from './pages/IngestionMonitor'
import DeveloperPortal from './pages/DeveloperPortal'
import ErrorBoundary from './components/ErrorBoundary'
import { isViewer } from './utils/permissions'

function RequireAuth({ children }) {
  const token = localStorage.getItem('token')
  if (!token) return <Navigate to="/login" replace />
  return children
}

// Dashboard-only "viewer" accounts never enter the main app shell — they are
// bounced to the standalone executive dashboard. (The server also blocks their
// token from every non-analytics endpoint, so this is just the UX layer.)
function RequireOperator({ children }) {
  if (isViewer()) return <Navigate to="/exec" replace />
  return children
}


export default function App() {
  return (
    <ErrorBoundary>
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={<RequireAuth><RequireOperator><Layout /></RequireOperator></RequireAuth>}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<Dashboard />} />
        <Route path="upload" element={<Upload />} />
        <Route path="run-recon" element={<RunRecon />} />
        <Route path="open-items" element={<OpenItems />} />
        <Route path="mismatches" element={<Mismatches />} />
        <Route path="manual-match" element={<ManualMatch />} />
        <Route path="reports" element={<Reports />} />
        <Route path="rules" element={<Rules />} />
        <Route path="users" element={<Users />} />
        <Route path="audit-log" element={<AuditLog />} />
        <Route path="auto-upload" element={<AutoUpload />} />
        <Route path="admin" element={<Admin />} />
        <Route path="aeps-settlement" element={<AepsSettlement />} />
        <Route path="qr-settlement"   element={<QRSettlement   />} />
        <Route path="sbi-kiosk"       element={<SBIKiosk       />} />
        <Route path="evalue"          element={<Evalue         />} />
        <Route path="bbps"            element={<Bbps           />} />
        <Route path="insights"        element={<Insights       />} />
        <Route path="analytics"       element={<Analytics      />} />
        <Route path="product/:slug"   element={<ProductReconPage />} />
        <Route path="workflow"        element={<Workflow />} />
        <Route path="ingestion-monitor" element={<IngestionMonitor />} />
        <Route path="developer-portal" element={<DeveloperPortal />} />
      </Route>
      {/* Standalone full-screen executive dashboard (no sidebar). Public @eko.co.in
          email-code access OR any existing session; PublicDashboard gates itself. */}
      <Route path="/exec" element={<PublicDashboard />} />
      <Route path="*" element={<Navigate to={isViewer() ? '/exec' : '/dashboard'} replace />} />
    </Routes>
    </ErrorBoundary>
  )
}
