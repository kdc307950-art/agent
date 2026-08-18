import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Load the parent .env for the local proxy only; the token is not bundled into React.
  const env = loadEnv(mode, '..', '')
  const proxy = {
    target: 'http://127.0.0.1:8000',
    changeOrigin: true,
    rewrite: (path) => path.replace(/^\/api/, ''),
  }

  if (env.DEV_TENANT_TOKEN) {
    proxy.headers = { Authorization: `Bearer ${env.DEV_TENANT_TOKEN}` }
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
