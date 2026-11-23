import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` (and the Compose frontend) proxies /api to the API process.
// A static build has no API: there public/api/plans, the API's answer for the
// same data/plans.csv, is served instead, so the pricing page still renders in
// Review's sandboxed visual previews. The demo tests keep the two equal.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": process.env.API_ORIGIN || "http://127.0.0.1:8000" },
  },
});
