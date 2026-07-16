import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Nothing here depends on a backend. `npm run build` produces a fully static
// site that renders from public/results.json, which is why the deployed link
// works with no API keys and no server running.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
  build: {
    outDir: 'dist',
    // The composites are large PNGs and are served as files from public/,
    // never inlined as base64.
    assetsInlineLimit: 0,
  },
});
