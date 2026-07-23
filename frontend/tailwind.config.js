/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        // THEME TOKENS (2026-07-23): every brand color resolves through a CSS variable
        // (RGB triplet + <alpha-value>, mandatory for /NN opacity utilities in v3.4) so
        // a [data-theme="…"] attribute on <html> re-skins the whole app. The :root
        // defaults in index.css are EXACTLY the old hexes — with no theme selected the
        // app renders byte-identical to before. Status/product colors stay hardcoded
        // (semantic, theme-independent) on purpose.
        primary: {
          DEFAULT: "rgb(var(--c-primary) / <alpha-value>)",
          light: "rgb(var(--c-primary-light) / <alpha-value>)",
          dark: "rgb(var(--c-primary-dark) / <alpha-value>)",
          50: "rgb(var(--c-primary-50) / <alpha-value>)",
          100: "rgb(var(--c-primary-100) / <alpha-value>)",
          200: "rgb(var(--c-primary-200) / <alpha-value>)",
          300: "rgb(var(--c-primary-300) / <alpha-value>)",
          400: "rgb(var(--c-primary-400) / <alpha-value>)",
          500: "rgb(var(--c-primary-500) / <alpha-value>)",
          600: "rgb(var(--c-primary-600) / <alpha-value>)",
          700: "rgb(var(--c-primary-700) / <alpha-value>)",
          800: "rgb(var(--c-primary-800) / <alpha-value>)",
          900: "rgb(var(--c-primary-900) / <alpha-value>)",
          950: "rgb(var(--c-primary-950) / <alpha-value>)",
        },
        accent: {
          DEFAULT: "rgb(var(--c-accent) / <alpha-value>)",
          light: "rgb(var(--c-accent-light) / <alpha-value>)",
          50: "rgb(var(--c-accent-50) / <alpha-value>)",
          100: "rgb(var(--c-accent-100) / <alpha-value>)",
          200: "rgb(var(--c-accent-200) / <alpha-value>)",
          300: "rgb(var(--c-accent-300) / <alpha-value>)",
          400: "rgb(var(--c-accent-400) / <alpha-value>)",
          500: "rgb(var(--c-accent-500) / <alpha-value>)",
          600: "rgb(var(--c-accent-600) / <alpha-value>)",
          700: "rgb(var(--c-accent-700) / <alpha-value>)",
          800: "rgb(var(--c-accent-800) / <alpha-value>)",
          900: "rgb(var(--c-accent-900) / <alpha-value>)",
        },
        surface: {
          DEFAULT: "rgb(var(--c-surface) / <alpha-value>)",
          dark: "rgb(var(--c-surface-dark) / <alpha-value>)",
        },
        // The NEUTRAL scale is themeable too — dark themes INVERT this ramp, which is
        // what flips every text-gray-800 / bg-gray-50 / border-gray-100 in the app to
        // readable light-on-dark without touching a single page. (`white` is NOT
        // remapped globally — sidebar text-white must stay white; card surfaces are
        // re-skinned via the [data-theme] component overrides in index.css.)
        gray: {
          50: "rgb(var(--c-gray-50) / <alpha-value>)",
          100: "rgb(var(--c-gray-100) / <alpha-value>)",
          200: "rgb(var(--c-gray-200) / <alpha-value>)",
          300: "rgb(var(--c-gray-300) / <alpha-value>)",
          400: "rgb(var(--c-gray-400) / <alpha-value>)",
          500: "rgb(var(--c-gray-500) / <alpha-value>)",
          600: "rgb(var(--c-gray-600) / <alpha-value>)",
          700: "rgb(var(--c-gray-700) / <alpha-value>)",
          800: "rgb(var(--c-gray-800) / <alpha-value>)",
          900: "rgb(var(--c-gray-900) / <alpha-value>)",
          950: "rgb(var(--c-gray-950) / <alpha-value>)",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
      boxShadow: {
        card: "0 1px 2px 0 rgb(16 24 40 / 0.04), 0 1px 3px 0 rgb(16 24 40 / 0.06)",
        "card-hover":
          "0 4px 6px -2px rgb(16 24 40 / 0.05), 0 12px 16px -4px rgb(16 24 40 / 0.10)",
        soft: "0 2px 4px -2px rgb(16 24 40 / 0.06), 0 4px 8px -2px rgb(16 24 40 / 0.08)",
      },
      keyframes: {
        // Opacity-only on purpose: a `transform` here (even translateY(0)) with
        // fill-mode `both` leaves a non-`none` transform on every
        // `.animate-fade-in` element forever, making it the containing block for
        // any descendant `position:fixed` modal and clipping it to the content
        // area instead of the viewport. Keeping this transform-free fixes that at
        // the root for ALL modals (App's page wrapper uses animate-fade-in).
        "fade-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
      },
      animation: {
        "fade-in": "fade-in 0.25s ease-out both",
      },
    },
  },
  plugins: [],
}
