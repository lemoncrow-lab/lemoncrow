import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
var apiTarget = process.env.VITE_API_URL || "http://127.0.0.1:7420";
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
        proxy: {
            "/api": {
                target: apiTarget,
                changeOrigin: true,
                rewrite: function (path) { return path.replace(/^\/api/, ""); },
            },
        },
    },
    preview: {
        host: "0.0.0.0",
        port: 3125,
    },
});
