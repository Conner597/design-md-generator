import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev server proxies /api to the FastAPI backend so the frontend can call
// it without CORS friction. Change the target if you run the backend elsewhere.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
