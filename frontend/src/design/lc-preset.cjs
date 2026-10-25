/*
 * LemonCrow shared Tailwind preset ("Ink and Lemon"). CANONICAL SOURCE. Tailwind 3.4.
 *
 * Used by the Review app (frontend/) and, as a synced copy, by the landing site.
 * Colours read the CSS variables in lc-tokens.css, so one class works in both
 * themes; the accent ramp reads --brand-50..950 with the lemon values as a
 * fallback, so an older tokens copy still renders. App-only or landing-only
 * overrides live in each repo's tailwind config, not here.
 */
const plugin = require("tailwindcss/plugin");
const v = (name) => `rgb(var(--${name}) / <alpha-value>)`;
const sev = (n) => ({ DEFAULT: v(n), bg: v(`${n}-bg`), line: v(`${n}-line`), solid: v(`${n}-solid`) });
// Lemon ramp: [step, rgb triplet]. The variables swap with the accent group; these are the fallbacks.
const LEMON = {
  50: "255 251 224", 100: "255 246 176", 200: "255 239 122", 300: "255 232 77", 400: "255 220 0",
  500: "242 206 0", 600: "201 171 0", 700: "122 102 0", 800: "107 90 0", 900: "74 62 0", 950: "43 36 0",
};
const ramp = Object.fromEntries(
  Object.entries(LEMON).map(([step, rgb]) => [step, `rgb(var(--brand-${step}, ${rgb}) / <alpha-value>)`]),
);

/** @type {import('tailwindcss').Config} */
module.exports = {
  theme: {
    extend: {
      colors: {
        background: v("background"),
        foreground: v("foreground"),
        card: { DEFAULT: v("card"), foreground: v("card-foreground") },
        popover: { DEFAULT: v("popover"), foreground: v("popover-foreground") },
        primary: { DEFAULT: v("primary"), foreground: v("primary-foreground") }, // solid ink button
        muted: { DEFAULT: v("muted"), foreground: v("muted-foreground") },
        subtle: v("subtle-foreground"),
        accent: { DEFAULT: v("accent"), foreground: v("accent-foreground") },
        border: v("border"),
        input: v("input"),
        ring: v("ring"),
        indicator: v("indicator"),
        brand: {
          DEFAULT: v("brand"), foreground: v("brand-foreground"), hover: v("brand-hover"), border: v("brand-border"),
          ...ramp,
        },
        p0: sev("p0"), p1: sev("p1"), p2: sev("p2"), ok: sev("ok"), ai: sev("ai"),
      },
      borderColor: { DEFAULT: v("border") },
      ringColor: { DEFAULT: v("ring") },
      fontFamily: {
        sans: ['"Red Hat Text"', "ui-sans-serif", "system-ui", "-apple-system", '"Segoe UI"', "Roboto", "sans-serif"],
        display: ['"Red Hat Display"', '"Red Hat Text"', "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", '"SFMono-Regular"', "Menlo", "Consolas", "monospace"],
      },
      fontSize: {
        // one scale, nine steps
        tag:     ["0.6875rem", { lineHeight: "0.875rem", letterSpacing: "0.12em", fontWeight: "500" }], // 11/14 mono eyebrow, badge
        meta:    ["0.75rem",   { lineHeight: "1rem" }],        // 12/16
        ui:      ["0.8125rem", { lineHeight: "1.125rem" }],    // 13/18 app body
        small:   ["0.875rem",  { lineHeight: "1.375rem" }],    // 14/22
        copy:    ["1rem",      { lineHeight: "1.625rem" }],    // 16/26 marketing body
        lead:    ["1.125rem",  { lineHeight: "1.75rem" }],     // 18/28
        h3:      ["1.5rem",    { lineHeight: "2rem", letterSpacing: "-0.015em", fontWeight: "600" }],
        h2:      ["clamp(1.875rem, 1.35rem + 1.8vw, 2.5rem)", { lineHeight: "1.1", letterSpacing: "-0.025em", fontWeight: "600" }],
        display: ["clamp(2.5rem, 1.4rem + 4vw, 4rem)",        { lineHeight: "1",   letterSpacing: "-0.035em", fontWeight: "600" }],
        code:    ["0.8125rem",  { lineHeight: "1.375rem" }],    // 13/22 mono
        // frontend names kept, values +1px (font-size only; line-height stays inherited)
        micro: "0.625rem", caption: "0.6875rem", label: "0.75rem", body: "0.8125rem", heading: "0.875rem", title: "1rem",
      },
      borderRadius: { sm: "4px", DEFAULT: "6px", md: "6px", lg: "8px" },
      boxShadow: {
        float: "0 12px 32px -12px rgb(10 10 10 / 0.28), 0 0 0 1px rgb(var(--border))",
        frame: "0 32px 64px -32px rgb(10 10 10 / 0.35)",
        indicator: "inset 0 -2px 0 rgb(var(--indicator))", // active tab / current nav underline, not elevation
      },
      borderWidth: { rail: "3px" },
      letterSpacing: { wordmark: "0.14em" },
      maxWidth: { content: "72rem", wide: "80rem", prose: "44rem", narrow: "48rem" },
      transitionDuration: { fast: "120ms", base: "200ms", slow: "400ms" },
      transitionTimingFunction: { DEFAULT: "cubic-bezier(0.2, 0, 0, 1)" },
      keyframes: { rise: { from: { opacity: "0", transform: "translateY(8px)" }, to: { opacity: "1", transform: "none" } } },
      animation: { rise: "rise 400ms cubic-bezier(0.2, 0, 0, 1) both" },
    },
  },
  plugins: [
    plugin(({ addBase, addVariant }) => {
      addVariant("pointer-coarse", "@media (pointer: coarse)"); // controls grow to a 44px target on touch
      addBase({
        // One quiet 1px outline flush against the control, no gap (ink on light, lemon on dark via --ring).
        ":focus-visible": { outline: "1px solid rgb(var(--ring))", outlineOffset: "0px" },
        "::selection": { backgroundColor: "rgb(var(--brand) / 0.55)", color: "rgb(10 10 10)" },
        "@media (prefers-reduced-motion: reduce)": {
          "*, *::before, *::after": { animationDuration: "0.01ms !important", animationIterationCount: "1 !important", transitionDuration: "0.01ms !important", scrollBehavior: "auto !important" },
        },
      });
    }),
  ],
};
