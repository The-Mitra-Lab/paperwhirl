import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    // Tauri's `devUrl` is hardcoded to :5173. If Vite silently falls
    // back to 5174 the Tauri window shows a blank "can't connect"
    // page. strictPort: true makes Vite fail loudly so we notice and
    // free the port instead of debugging a confusing blank window.
    strictPort: true,
    hmr: {
      clientPort: 5173,
    },
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
  preview: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
