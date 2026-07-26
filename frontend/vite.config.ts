import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const sharedHmr = env.PXY_SHARED_HMR === 'true'
  const port = Number(env.VITE_DEV_PORT) || 3021

  return {
    base: env.VITE_BASE_PATH || '/',
    plugins: [vue()],
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    },
    build: {
      // 搜狗高速模式可能使用较旧的 Chromium 内核，避免输出过新的 JS 语法。
      target: 'chrome64',
    },
    server: {
      host: sharedHmr ? '127.0.0.1' : undefined,
      port,
      strictPort: sharedHmr,
      allowedHosts: sharedHmr ? ['pxy.xyz.hr'] : undefined,
      hmr: sharedHmr
        ? {
            protocol: 'wss',
            host: 'pxy.xyz.hr',
            clientPort: 443,
            path: env.VITE_HMR_PATH || '/futures-app/__vite_hmr',
          }
        : undefined,
      watch: sharedHmr ? { usePolling: true, interval: 300 } : undefined,
      proxy: {
        '/api': {
          target: env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:3022',
          changeOrigin: true,
        },
      },
    },
  }
})
