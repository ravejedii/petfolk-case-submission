import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

// Dev server proxies /api to the Node API (server/index.js) on :4600 so the
// browser talks to one origin. `vite build web` emits web/dist, which the API
// server also serves statically for a single-port production-style run.
//
// ws: true matters — the app's whole session (every API call and every live
// event) rides one WebSocket at /api/ws, and without this the dev server would
// answer the upgrade itself instead of forwarding it, leaving `npm run dev` on
// the fetch fallback while the built app used the tunnel.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    fs: {
      // WorkflowView imports the diagram from docs/assets/ (repo root).
      allow: [repoRoot],
    },
    proxy: {
      // PETFOLK_API points the dev server at an API on another port (a second
      // server, a work copy); unset it and this is the default `npm start` one.
      "/api": {
        target: process.env.PETFOLK_API || "http://localhost:4600",
        ws: true,
      },
    },
  },
});
