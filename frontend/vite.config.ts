import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api so the browser sees a single origin. That keeps
// CORS out of the development loop entirely, and makes the SSE stream
// same-origin -- one fewer thing to be wrong about when a stream silently fails
// to connect. The server already sends Cache-Control: no-cache and
// X-Accel-Buffering: no, which is what stops the stream being buffered.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
