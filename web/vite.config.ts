import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 3000,
    proxy: {
      '/api': 'http://localhost:8080',
      '/auth': 'http://localhost:8080',
      '/stats': 'http://localhost:8080',
      '/accounts': 'http://localhost:8080',
      '/rotation': 'http://localhost:8080',
      '/config': 'http://localhost:8080',
      '/api-keys': 'http://localhost:8080',
      '/health': 'http://localhost:8080',
      // /settings 既是前端页面路由（系统配置页之外的另一页）又是运行期设置 API：
      // 浏览器导航（Accept 带 text/html）交给 Vite 的 SPA，只有 XHR/fetch 才转发给后端。
      '/settings': {
        target: 'http://localhost:8080',
        bypass: (req) => (req.headers.accept?.includes('text/html') ? '/index.html' : undefined),
      },
    },
  },
  build: {
    outDir: path.resolve(__dirname, '../src/aistudio_api/static'),
    emptyOutDir: true,
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name].js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/[name].[ext]',
        manualChunks: {
          'vendor-vue': ['vue', 'vue-router', 'pinia'],
          'vendor-icons': ['lucide-vue-next'],
        },
      },
    },
  },
})
