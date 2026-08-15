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
      // Mirrors nginx in production (frontend/nginx.conf.template). Keeping the
      // two the same shape is deliberate: an auth bug caused by crossing an
      // origin boundary should show up here, not for the first time on a
      // customer's browser.
      "/api": {
        target: process.env.BACKEND_URL || "http://localhost:8100",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      // The public consent API. Mounted at the SAME path the backend serves
      // (no rewrite), exactly as nginx does it — these paths are a published
      // contract and must not depend on a proxy's shape.
      "/public": {
        target: process.env.BACKEND_URL || "http://localhost:8100",
        changeOrigin: true,
      },
      "/gateway": {
        target: process.env.GATEWAY_URL || "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/gateway/, ""),
      },
    },
  },
  preview: { host: "0.0.0.0", port: 5173 },
});
