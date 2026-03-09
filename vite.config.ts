import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ command }) => {
  const backendTarget = process.env.VITE_BACKEND_PROXY_TARGET || 'http://127.0.0.1:18080';

  return {
    // Electron file:// load requires relative asset paths in production build.
    base: command === 'build' ? './' : '/',
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      port: 5173,
      strictPort: true,
      proxy: {
        '/api': {
          target: backendTarget,
          changeOrigin: true,
          rewrite: (requestPath) => requestPath.replace(/^\/api/, ''),
        },
      },
    },
  };
});
