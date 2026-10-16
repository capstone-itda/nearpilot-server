import { defineConfig } from '@playwright/test';

// 시스템 Chrome으로 빌드 결과(vite preview)를 검사한다.
export default defineConfig({
  testDir: 'e2e',
  timeout: 120_000,
  fullyParallel: true,
  reporter: 'list',
  use: { baseURL: 'http://localhost:4318', channel: 'chrome' },
  webServer: { command: 'npm run build && npx vite preview --port 4318 --strictPort', url: 'http://localhost:4318', reuseExistingServer: false, timeout: 120_000 }
});
