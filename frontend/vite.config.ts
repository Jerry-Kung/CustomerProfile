import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // 产物用相对路径引用资源：运行台由 FastAPI 挂在任意前缀下（见 §4 的 SERVE_UI），
  // 绝对路径 /assets/... 会在非根路径部署时全部 404。
  base: './',
  server: {
    // 开发模式把 API 请求转给后端，避免前端写死后端地址。
    proxy: {
      '/workflows': 'http://127.0.0.1:8000',
      '/runs': 'http://127.0.0.1:8000',
      '/definition-versions': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
  build: {
    outDir: 'dist',
    // 产物入库（docs/specs/V0.4只读运行台.md §4），因此关掉 sourcemap 缩小体积。
    sourcemap: false,
  },
})
