// Theme registry — ids must match the [data-theme="…"] blocks in index.css.
// 'eko' is the default: it clears the attribute so :root (the original palette) applies.
// Dark themes invert the whole neutral scale (text, cards, borders), not just the brand.
export const THEMES = [
  { id: 'eko',      label: 'Eko (Light)', primary: '#094053', accent: '#F9AB10', bg: '#f8f9fc' },
  { id: 'grey',     label: 'Grey',        primary: '#334155', accent: '#0EA5E9', bg: '#f4f5f7' },
  { id: 'dark',     label: 'Dark',        primary: '#2A7A9A', accent: '#F9AB10', bg: '#0f172a' },
  { id: 'midnight', label: 'Midnight',    primary: '#3B82F6', accent: '#F59E0B', bg: '#0a0f1e' },
]

export const THEME_STORAGE_KEY = 'theme'
