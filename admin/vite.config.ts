import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const target = "http://127.0.0.1:8090";

export default defineConfig({
  plugins: [react()],
  build: {
    rolldownOptions: { input: ["index.html", "api-docs.html"] },
  },
  server: {
    proxy: {
      // The admin API rejects writes whose Origin is not its own. In dev the
      // API's configured public URL is this Vite server, so the browser's own
      // Origin is the one it expects; rewriting it here made every write fail
      // the check. Only the Host header is changed for the upstream hop.
      "/api": { target, changeOrigin: true },
    },
  },
});
