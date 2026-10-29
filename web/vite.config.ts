import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  base: '/studio/',
  plugins: [react()],
  build: {
    outDir: '../clippie/static/studio',
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 800,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/static/fonts': 'http://127.0.0.1:8000',
    },
  },
  test: { environment: 'node', include: ['src/**/*.test.ts'] },
})
