// Per-user theme selection. The chosen theme id is stamped as data-theme on <html>
// (index.html applies it pre-paint from localStorage so there is no flash) and the
// CSS variables in index.css do the actual re-skinning. Purely additive: with the
// default 'eko' theme the attribute is removed and the app renders exactly as before.
import React, { createContext, useContext, useEffect, useState } from 'react'
import { THEMES, THEME_STORAGE_KEY } from './themes'

const ThemeCtx = createContext({ theme: 'eko', setTheme: () => {} })

export function ThemeProvider({ children }) {
  const [theme, setTheme] = useState(() => {
    try {
      const t = localStorage.getItem(THEME_STORAGE_KEY)
      return THEMES.some(x => x.id === t) ? t : 'eko'
    } catch { return 'eko' }
  })

  useEffect(() => {
    const el = document.documentElement
    if (theme === 'eko') delete el.dataset.theme
    else el.dataset.theme = theme
    try { localStorage.setItem(THEME_STORAGE_KEY, theme) } catch { /* private mode */ }
  }, [theme])

  return <ThemeCtx.Provider value={{ theme, setTheme }}>{children}</ThemeCtx.Provider>
}

export const useTheme = () => useContext(ThemeCtx)
