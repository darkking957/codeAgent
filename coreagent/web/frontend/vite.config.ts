import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 产物绝对引用 `/assets/...`，由 FastAPI 在 `/assets` 挂载 dist/assets（见 web/app.py）。
// dev server 把 API 调用代理到本地后端（npm run dev 时），prod 走同源静态托管。
export default defineConfig({
  base: "/",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    proxy: {
      "/auth": "http://127.0.0.1:8000",
      "/conversations": "http://127.0.0.1:8000",
      "/usage": "http://127.0.0.1:8000",
    },
  },
});
