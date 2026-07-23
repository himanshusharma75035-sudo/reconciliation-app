import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { Toaster } from 'react-hot-toast'
import App from './App'
import { ThemeProvider } from './ThemeContext'
import './index.css'

// The app is served under a base path behind nginx (Vite `base`, e.g. /recon/).
// Derive the router basename from it so every route resolves under the subpath;
// at root (base "/") this collapses to "/" so nothing changes in plain dev.
const BASENAME = import.meta.env.BASE_URL.replace(/\/+$/, '') || '/'

ReactDOM.createRoot(document.getElementById('root')).render(
  <BrowserRouter basename={BASENAME}>
    <ThemeProvider>
    <App />
    </ThemeProvider>
    <Toaster
      position="top-right"
      toastOptions={{
        duration: 4000,
        style: {
          background: '#fff',
          color: '#1f2937',
          fontSize: '13.5px',
          fontWeight: 500,
          borderRadius: '10px',
          border: '1px solid #f3f4f6',
          boxShadow: '0 4px 6px -2px rgb(16 24 40 / 0.05), 0 12px 16px -4px rgb(16 24 40 / 0.10)',
          padding: '10px 14px',
        },
        success: { iconTheme: { primary: '#059669', secondary: '#fff' } },
        error: { iconTheme: { primary: '#dc2626', secondary: '#fff' } },
      }}
    />
  </BrowserRouter>
)
