import { readFileSync } from 'fs';
import { resolve } from 'path';
import { defineConfig } from 'vite';

// Ortak parçalar (header, footer, drawer, modal) tek kaynaktan gelir:
// sayfada <!-- @include header --> yazan yere src/partials/header.html eklenir.
function htmlPartials() {
  const dir = resolve(__dirname, 'src/partials');
  return {
    name: 'projenic-html-partials',
    transformIndexHtml: {
      order: 'pre',
      handler(html) {
        const pattern = /<!--\s*@include\s+([\w-]+)\s*-->/g;
        // Parçalar başka parça içerebilir (ör. drawer → brief-form); en fazla 3 seviye.
        for (let depth = 0; depth < 3 && pattern.test(html); depth++) {
          html = html.replace(pattern, (_, name) => readFileSync(resolve(dir, `${name}.html`), 'utf8'));
        }
        return html;
      }
    },
    handleHotUpdate({ file, server }) {
      if (file.startsWith(dir)) server.ws.send({ type: 'full-reload' });
    }
  };
}

export default defineConfig({
  plugins: [htmlPartials()],
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
        projeYamaha: resolve(__dirname, 'proje-yamaha.html'),
        projeMercedes: resolve(__dirname, 'proje-mercedes.html'),
        projeSonmezler: resolve(__dirname, 'proje-sonmezler.html'),
        projeJeanologia: resolve(__dirname, 'proje-jeanologia.html'),
        projeDardanel: resolve(__dirname, 'proje-dardanel.html'),
        yetkinlikler: resolve(__dirname, 'yetkinlikler.html'),
        hakkimizda: resolve(__dirname, 'hakkimizda.html'),
        iletisim: resolve(__dirname, 'iletisim.html'),
        kvkk: resolve(__dirname, 'kvkk.html'),
        cerezPolitikasi: resolve(__dirname, 'cerez-politikasi.html'),
        aydinlatmaMetni: resolve(__dirname, 'aydinlatma-metni.html'),
        notFound: resolve(__dirname, '404.html')
      }
    }
  }
});
