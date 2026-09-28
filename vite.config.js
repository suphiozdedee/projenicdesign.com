import { resolve } from 'path';
import { defineConfig } from 'vite';

export default defineConfig({
  server: {
    port: 3000,
    host: true,
    watch: {
      ignored: ['**/graphify-out/**', '**/.agents/**', '**/temp_repos/**']
    }
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1000,
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        projeler: resolve(__dirname, 'projeler.html'),
        projeAurora: resolve(__dirname, 'proje-aurora.html'),
        yetkinlikler: resolve(__dirname, 'yetkinlikler.html'),
        hakkimizda: resolve(__dirname, 'hakkimizda.html'),
        iletisim: resolve(__dirname, 'iletisim.html'),
        kvkk: resolve(__dirname, 'kvkk.html'),
        cerezPolitikasi: resolve(__dirname, 'cerez-politikasi.html'),
        aydinlatmaMetni: resolve(__dirname, 'aydinlatma-metni.html')
      }
    }
  }
});
