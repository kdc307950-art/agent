import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(() => {
  const proxy = {
    target: 'http://127.0.0.1:8000',
    changeOrigin: true,
    rewrite: (path) => path.replace(/^\/api/, ''),
  }

  return {
    plugins: [react()],
    envDir: '..',
    server: {
      host: '127.0.0.1',
      proxy: { '/api': proxy },
    },
  }
})
