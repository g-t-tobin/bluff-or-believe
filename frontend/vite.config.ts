import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, ".", "VITE_");

  if (command === "build" && mode === "production") {
    if (env.VITE_API_BASE_URL?.trim() !== "/api" || env.VITE_WS_BASE_URL?.trim()) {
      throw new Error(
        "Production builds must use the same-origin /api proxy for HTTP and WebSocket requests.",
      );
    }
  }

  return {
    plugins: [react()],
    server: { host: "127.0.0.1", port: 5173 },
  };
});
