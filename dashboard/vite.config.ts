import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// ?source=server는 대시보드를 연 주소와 같은 출처로 /dashboard/...(조회 REST)와 /dashboard/stream(WebSocket)을 부른다.
// 개발·미리보기 서버가 이 경로를 NearPilot 서버로 넘기므로 서버에 CORS를 열지 않는다. 서버 주소는 NEARPILOT_SERVER로 바꾼다.
const proxy = { '/dashboard': { target: process.env.NEARPILOT_SERVER ?? 'http://127.0.0.1:8000', ws: true, changeOrigin: false } };

export default defineConfig({
  plugins: [react()],
  base: './',
  server: { proxy },
  preview: { proxy },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}']
  }
});
