import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  // 相对资源路径：让 index.html 引用的 ./assets/* 随文档位置解析，
  // 配合 <base> 即可在根 / 或路径分租户 /<uid>/ 下都正确加载（见 docs/06 §5.4）。
  base: "./",
  plugins: [react(), tailwindcss()],
  build: {
    rollupOptions: {
      input: {
        browser: "index.html",
        setup: "setup.html",
      },
    },
  },
  server: {
    port: 5173,
    // 开发时把 API 代理到后端，避免跨域
    proxy: {
      "/api": "http://127.0.0.1:8800",
    },
  },
});
