import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3000,
    // 端口被占用时报错而不是静默跳到 3001；同时监听 127.0.0.1
    strictPort: true,
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    // 产物输出到项目根的 static/（位于 frontend 之外，FastAPI 直接托管）。
    // emptyOutDir 必须显式开启（2026-09-03 Vite 7 升级）：outDir 在项目根之外时
    // Vite 默认**不**清空目录（防误删保护），旧 hash 产物会越积越多。
    outDir: '../static',
    emptyOutDir: true,
  },
})
