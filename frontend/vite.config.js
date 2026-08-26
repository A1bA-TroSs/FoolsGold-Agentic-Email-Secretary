import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// base: './' so the built bundle works when Electron loads it from file://
// or when FastAPI serves it from its own origin.
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:8765' },
  },
  build: { outDir: 'dist', emptyOutDir: true },
});
