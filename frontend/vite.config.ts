import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.VITE_API_URL || "http://localhost:8787";

export default defineConfig({
  plugins: [react()],
  build: {
    chunkSizeWarningLimit: 2000,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: true,
  },
  server: {
    host: "0.0.0.0",
    port: 3125,
    // Containerised previews watch the workspace across a mount-namespace
    // boundary, where inotify events never arrive — those need polling. Opt-in
    // so native `npm run dev` keeps the cheaper default watcher.
    watch: process.env.VITE_USE_POLLING
      ? { usePolling: true, interval: 200 }
      : undefined,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  preview: {
    host: "0.0.0.0",
    port: 3125,
  },
});
