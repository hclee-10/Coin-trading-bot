import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 빌드 산출물은 FastAPI 가 서빙하는 bot/web/static 으로 바로 떨어뜨린다.
// 개발 중에는 `npm run dev` 가 /api 요청을 로컬 봇 서버로 프록시한다.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../bot/web/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/healthz': 'http://127.0.0.1:8000',
    },
  },
})
