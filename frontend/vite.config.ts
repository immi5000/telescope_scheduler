import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The project has no @types/node, and one environment read does not justify it.
declare const process: { env: Record<string, string | undefined> };

// The dev server proxies /api so the browser sees a single origin. That keeps
// CORS out of the development loop entirely, and makes the SSE stream
// same-origin -- one fewer thing to be wrong about when a stream silently fails
// to connect. The server already sends Cache-Control: no-cache and
// X-Accel-Buffering: no, which is what stops the stream being buffered.
//
// TSCHED_API_TARGET points the proxy at a second backend, so a scratch server
// can run beside the one you are using without either stopping the other.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.TSCHED_API_TARGET ?? "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
