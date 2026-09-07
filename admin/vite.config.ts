import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const target = "http://127.0.0.1:8090";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // The admin API rejects writes whose Origin is not its own, so the dev
      // proxy has to present the target's origin instead of the Vite server's.
      "/api": {
        target,
        changeOrigin: true,
        configure: (proxy) =>
          proxy.on("proxyReq", (request) =>
            request.setHeader("origin", target),
          ),
      },
    },
  },
});
