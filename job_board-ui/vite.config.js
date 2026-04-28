import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      "/job-board-api": {
        target: "http://127.0.0.1:5060",
        changeOrigin: true
      },
      "/orch-api": {
        target: "http://127.0.0.1:5050",
        changeOrigin: true
      }
    }
  }
});

