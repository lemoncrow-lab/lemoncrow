import type { Config } from "tailwindcss";
import animate from "tailwindcss-animate";

import preset from "./src/design/lc-preset.cjs";

/** Semantic (shadcn) colour: an alias onto the themed ramps in src/index.css. */
const token = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  // Ink and Lemon: tokens, type scale, radii, shadows, widths and motion come from the shared preset
  // (src/design/, vendored from the repository's design/ directory). This file adds only what is
  // specific to the Review app: the legacy neutral/surface/hue ramps and the z-index scheme.
  presets: [preset],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    // Two elevation shadows exist (float for overlays, frame for the one hero artefact) plus the indicator
    // underline from the preset. Tailwind's default shadow-sm..2xl are retired.
    boxShadow: { none: "none" },
    extend: {
      /* Overlays share z-50 and stack by DOM order; only the global request indicator sits above them. */
      zIndex: { indicator: "100" },
      /* The one tracking value for uppercase mono labels (the preset's text-tag carries it too). */
      letterSpacing: { tag: "0.12em" },
      colors: {
        // background, foreground, card, popover, primary, muted, accent, border, input and ring come from the
        // preset. The status pairs and `secondary` are Review-app tokens defined in src/index.css.
        secondary: { DEFAULT: token("secondary"), foreground: token("secondary-foreground") },
        destructive: { DEFAULT: token("destructive"), foreground: token("destructive-foreground") },
        success: { DEFAULT: token("success"), foreground: token("success-foreground") },
        warning: { DEFAULT: token("warning"), foreground: token("warning-foreground") },
        info: { DEFAULT: token("info"), foreground: token("info-foreground") },
        neutral: {
          50: "rgb(var(--n-50) / <alpha-value>)",
          100: "rgb(var(--n-100) / <alpha-value>)",
          200: "rgb(var(--n-200) / <alpha-value>)",
          300: "rgb(var(--n-300) / <alpha-value>)",
          400: "rgb(var(--n-400) / <alpha-value>)",
          500: "rgb(var(--n-500) / <alpha-value>)",
          600: "rgb(var(--n-600) / <alpha-value>)",
          700: "rgb(var(--n-700) / <alpha-value>)",
          800: "rgb(var(--n-800) / <alpha-value>)",
          900: "rgb(var(--n-900) / <alpha-value>)",
          950: "rgb(var(--n-950) / <alpha-value>)",
        },
        surface: {
          DEFAULT: "rgb(var(--surface) / <alpha-value>)",
          sunken: "rgb(var(--surface-sunken) / <alpha-value>)",
          code: "rgb(var(--surface-code) / <alpha-value>)",
          raised: "rgb(var(--surface-raised) / <alpha-value>)",
          tint: "rgb(var(--surface-tint) / <alpha-value>)",
          overlay: "rgb(var(--surface-overlay) / <alpha-value>)",
        },
        emerald: {
          50: "rgb(var(--emerald-50) / <alpha-value>)",
          100: "rgb(var(--emerald-100) / <alpha-value>)",
          200: "rgb(var(--emerald-200) / <alpha-value>)",
          300: "rgb(var(--emerald-300) / <alpha-value>)",
          400: "rgb(var(--emerald-400) / <alpha-value>)",
          500: "rgb(var(--emerald-500) / <alpha-value>)",
          600: "rgb(var(--emerald-600) / <alpha-value>)",
          700: "rgb(var(--emerald-700) / <alpha-value>)",
          800: "rgb(var(--emerald-800) / <alpha-value>)",
          900: "rgb(var(--emerald-900) / <alpha-value>)",
          950: "rgb(var(--emerald-950) / <alpha-value>)",
        },
        amber: {
          50: "rgb(var(--amber-50) / <alpha-value>)",
          100: "rgb(var(--amber-100) / <alpha-value>)",
          200: "rgb(var(--amber-200) / <alpha-value>)",
          300: "rgb(var(--amber-300) / <alpha-value>)",
          400: "rgb(var(--amber-400) / <alpha-value>)",
          500: "rgb(var(--amber-500) / <alpha-value>)",
          600: "rgb(var(--amber-600) / <alpha-value>)",
          700: "rgb(var(--amber-700) / <alpha-value>)",
          800: "rgb(var(--amber-800) / <alpha-value>)",
          900: "rgb(var(--amber-900) / <alpha-value>)",
          950: "rgb(var(--amber-950) / <alpha-value>)",
        },
        red: {
          50: "rgb(var(--red-50) / <alpha-value>)",
          100: "rgb(var(--red-100) / <alpha-value>)",
          200: "rgb(var(--red-200) / <alpha-value>)",
          300: "rgb(var(--red-300) / <alpha-value>)",
          400: "rgb(var(--red-400) / <alpha-value>)",
          500: "rgb(var(--red-500) / <alpha-value>)",
          600: "rgb(var(--red-600) / <alpha-value>)",
          700: "rgb(var(--red-700) / <alpha-value>)",
          800: "rgb(var(--red-800) / <alpha-value>)",
          900: "rgb(var(--red-900) / <alpha-value>)",
          950: "rgb(var(--red-950) / <alpha-value>)",
        },
        cyan: {
          50: "rgb(var(--cyan-50) / <alpha-value>)",
          100: "rgb(var(--cyan-100) / <alpha-value>)",
          200: "rgb(var(--cyan-200) / <alpha-value>)",
          300: "rgb(var(--cyan-300) / <alpha-value>)",
          400: "rgb(var(--cyan-400) / <alpha-value>)",
          500: "rgb(var(--cyan-500) / <alpha-value>)",
          600: "rgb(var(--cyan-600) / <alpha-value>)",
          700: "rgb(var(--cyan-700) / <alpha-value>)",
          800: "rgb(var(--cyan-800) / <alpha-value>)",
          900: "rgb(var(--cyan-900) / <alpha-value>)",
          950: "rgb(var(--cyan-950) / <alpha-value>)",
        },
        violet: {
          50: "rgb(var(--violet-50) / <alpha-value>)",
          100: "rgb(var(--violet-100) / <alpha-value>)",
          200: "rgb(var(--violet-200) / <alpha-value>)",
          300: "rgb(var(--violet-300) / <alpha-value>)",
          400: "rgb(var(--violet-400) / <alpha-value>)",
          500: "rgb(var(--violet-500) / <alpha-value>)",
          600: "rgb(var(--violet-600) / <alpha-value>)",
          700: "rgb(var(--violet-700) / <alpha-value>)",
          800: "rgb(var(--violet-800) / <alpha-value>)",
          900: "rgb(var(--violet-900) / <alpha-value>)",
          950: "rgb(var(--violet-950) / <alpha-value>)",
        },
      },
    },
  },
  // The preset registers the pointer-coarse variant (controls grow to a 44px target, WCAG 2.5.5).
  plugins: [animate],
} satisfies Config;
