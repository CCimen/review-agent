import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  base: "./",
  plugins: [react()],
  build: { outDir: "../dist-preview", emptyOutDir: true },
  server: { host: "127.0.0.1", port: 8092, strictPort: true },
  preview: { host: "127.0.0.1", port: 8092, strictPort: true },
});
