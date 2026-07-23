// Theme registry — ids must match the [data-theme="…"] blocks in index.css.
// 'eko' is the default: it clears the attribute so :root (the original palette) applies.
export const THEMES = [
  { id: 'eko',    label: 'Eko Teal',  primary: '#094053', accent: '#F9AB10' },
  { id: 'indigo', label: 'Indigo',    primary: '#3730A3', accent: '#F59E0B' },
  { id: 'forest', label: 'Forest',    primary: '#166534', accent: '#F97316' },
  { id: 'slate',  label: 'Graphite',  primary: '#334155', accent: '#0EA5E9' },
]

export const THEME_STORAGE_KEY = 'theme'
