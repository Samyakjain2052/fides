import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The CMS runs as its own app on :5173, separate from the DSAR console the
// FastAPI gateway serves at :8000/ui.
//
// `/gateway` is proxied to the real DSAR backend in this repo. The mock API in
// src/api/index.js is the default (as the spec requires); flipping
// USE_REAL_DSAR_BACKEND there routes the three DSAR functions through this
// proxy instead, which also avoids any CORS setup.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/gateway": {
        target: process.env.GATEWAY_URL || "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/gateway/, ""),
      },
    },
  },
  preview: { host: "0.0.0.0", port: 5173 },
});
