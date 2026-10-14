/**
 * Proje iç sayfalarını src/js/projects.js verisinden üretir.
 * Kullanım: node scripts/build-project-pages.mjs
 * Proje bilgisi değişince bu komutu çalıştırıp çıkan proje-*.html dosyalarını commit'leyin.
 */
import { writeFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { projectData } from '../src/js/projects.js';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SITE = 'https://projenicdesign.com';

// Sıra = önceki / sonraki gezinmesi ve "01 / 06" numarası
export const PROJECT_PAGES = [
  { key: 'yamaha', file: 'proje-yamaha.html', meta: 'Fuar standı · Boat Show 2024 · İstanbul', desc: 'Ağır deniz motorları sergilemesi için özel statik güçlendirilmiş platform mimarisi, monolitik lake bankolar ve VIP ağırlama alanı.' },
  { key: 'mercedes', file: 'proje-mercedes.html', meta: 'Etkinlik standı · 2023 · İstanbul', desc: 'Mercedes-Benz Türk\'ün kurumsal kimliğini yansıtan minimalist ahşap hacimler, özel karşılama bankoları ve akustik bölmeler.' },
  { key: 'sonmezler', file: 'proje-sonmezler.html', meta: 'Fuar standı · 2023–2024', desc: 'Çelik üreticisi için iki farklı fuarda kurduğumuz standlar: ışıklı logo duvarları, LED ekranlar, görüşme ve karşılama alanları.' },
  { key: 'jeanologia', file: 'proje-jeanologia.html', meta: 'Fuar standı · ITM 2024 · İstanbul', desc: 'Tekstil bitim teknolojilerini sergileyen açık kurgulu fuar standı: makine teşhir alanları, ürün duvarları ve görüşme bölümleri.' },
  { key: 'dardanel', file: 'proje-dardanel.html', meta: 'Fuar standı · WorldFood 2024 · İstanbul', desc: 'Canlı şef mutfak istasyonu, interaktif tadım barı ve ışıklı pleksi teşhir duvarlarıyla lezzet ve mimariyi buluşturan fuar standı.' }
];

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const base = (src) => src.split('/').pop().replace(/\.jpg$/, '');
const pad = (n) => String(n).padStart(2, '0');

function picture(src, alt, sizes, eager) {
  const b = base(src);
  return `<picture><source type="image/webp" srcset="/assets/images/opt/${b}-640.webp 640w, /assets/images/opt/${b}-1024.webp 1024w, /assets/images/opt/${b}-1600.webp 1600w" sizes="${sizes}">` +
    `<img src="${src}" alt="${esc(alt)}" width="1920" height="1200" ${eager ? 'fetchpriority="high"' : 'loading="lazy"'} decoding="async"></picture>`;
}

PROJECT_PAGES.forEach((page, i) => {
  const d = projectData[page.key];
  const prev = PROJECT_PAGES[(i - 1 + PROJECT_PAGES.length) % PROJECT_PAGES.length];
  const next = PROJECT_PAGES[(i + 1) % PROJECT_PAGES.length];
  const title = d.title.replace(/ Showroom$/, '');
  const images = [{ src: d.heroImg, caption: d.heroCaption || d.title }, ...d.gallery].slice(0, 4);
  const [main, ...thumbs] = images;
  const url = `${SITE}/${page.file}`;

  const thumbHtml = thumbs.map((img, n) => `
            <button type="button" class="pp-thumb" data-pp-thumb aria-label="Görsel ${n + 2}: ${esc(img.caption)}" aria-pressed="false" data-src="${img.src}" data-alt="${esc(img.caption)}" data-index="${pad(n + 2)}">
              ${picture(img.src, '', '(min-width: 1024px) 20vw, 33vw', false)}
            </button>`).join('');
  // ana görsel küçükler arasında da yer alır ki geri dönülebilsin
  const mainThumb = `
            <button type="button" class="pp-thumb" data-pp-thumb aria-label="Görsel 1: ${esc(main.caption)}" aria-pressed="true" data-src="${main.src}" data-alt="${esc(main.caption)}" data-index="01">
              ${picture(main.src, '', '(min-width: 1024px) 15vw, 25vw', false)}
            </button>`;

  const html = `<!DOCTYPE html>
<html lang="tr" data-theme="light">
<head>
  <meta charset="UTF-8">
  <title>${esc(title)} · ${esc(page.meta.split(' · ')[0])} | Projenic</title>
  <meta name="description" content="${esc(d.lead)}">
  <link rel="canonical" href="${url}">
  <meta property="og:type" content="article">
  <meta property="og:locale" content="tr_TR">
  <meta property="og:site_name" content="Projenic">
  <meta property="og:title" content="${esc(title)} | Projenic">
  <meta property="og:description" content="${esc(d.lead)}">
  <meta property="og:url" content="${url}">
  <meta property="og:image" content="${SITE}${d.heroImg}">
  <meta property="og:image:width" content="1920">
  <meta property="og:image:height" content="1200">
  <meta property="og:image:alt" content="${title} — Projenic">
  <meta name="twitter:card" content="summary_large_image">
<!-- @include head-common -->
  <script type="application/ld+json">
  {"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[{"@type":"ListItem","position":1,"name":"Ana sayfa","item":"${SITE}/"},{"@type":"ListItem","position":2,"name":"Projeler","item":"${SITE}/projeler.html"},{"@type":"ListItem","position":3,"name":"${esc(title)}","item":"${url}"}]}
  </script>
</head>
<body data-page="projeler">
<!-- @include header -->

  <main id="icerik">
    <article class="pp wrap">
      <nav class="pp-bar" aria-label="Proje gezinmesi">
        <a href="/projeler.html" class="pp-bar__back">← Projeler</a>
        <div class="pp-bar__pager">
          <a href="/${prev.file}" rel="prev" class="pp-bar__step"><span>← Önceki</span><span class="pp-bar__name">${esc(projectData[prev.key].title.replace(/ Showroom$/, ''))}</span></a>
          <span class="pp-bar__count" aria-label="Proje ${i + 1} / ${PROJECT_PAGES.length}">${pad(i + 1)} / ${pad(PROJECT_PAGES.length)}</span>
          <a href="/${next.file}" rel="next" class="pp-bar__step pp-bar__step--next"><span>Sonraki →</span><span class="pp-bar__name">${esc(projectData[next.key].title.replace(/ Showroom$/, ''))}</span></a>
        </div>
      </nav>

      <header class="pp-head">
        <p class="t-data">${esc(page.meta)}</p>
        <h1 class="pp-title">${esc(title)}</h1>
        <p class="t-lead pp-desc">${esc(page.desc)}</p>
      </header>

      <div class="pp-grid">
        <div class="pp-media">
          ${d.area ? `<div class="dim pp-dim" aria-hidden="true"><span class="dim__tick dim__tick--a"></span><span class="dim__label">${esc(d.area)}${d.height ? ` · yükseklik ${esc(d.height)}` : ''}</span><span class="dim__tick dim__tick--b"></span></div>` : ''}
          <div class="pp-main" data-pp-main>
            ${picture(main.src, main.caption, '(min-width: 1024px) 70vw, 100vw', true)}
          </div>
          <div class="pp-thumbs" style="--n:${thumbs.length + 1}" role="group" aria-label="Proje görselleri">${mainThumb}${thumbHtml}
          </div>
          <div class="pp-cta">
            <a href="/iletisim.html#talep" class="btn btn--primary" data-open-drawer>Proje talebi gönderin</a>
          </div>
        </div>

        <dl class="pp-spec">
          <div class="pp-spec__client"><dt>Müşteri</dt><dd>${esc(d.client)}</dd></div>
          <div><dt>Yer</dt><dd>${esc(d.location)}</dd></div>
          <div><dt>Yıl</dt><dd class="num">${esc(d.year)}</dd></div>
          ${d.area ? `<div><dt>Ölçü</dt><dd class="num">${esc(d.area)}</dd></div>` : ''}
          ${d.height ? `<div><dt>Yükseklik</dt><dd class="num">${esc(d.height)}</dd></div>` : ''}
        </dl>
      </div>

      <nav class="case-next" aria-label="Sonraki proje">
        <a href="/projeler.html" class="text-link">Tüm projeler</a>
        <a href="/${next.file}" class="case-next__link"><span class="t-small">Sonraki proje</span><span class="case-next__title">${esc(projectData[next.key].title.replace(/ Showroom$/, ''))}</span></a>
      </nav>
    </article>
  </main>

  <aside aria-label="Hızlı iletişim">
  <a href="https://wa.me/905512047851?text=${encodeURIComponent(`Merhaba, ${title} projenizi gördüm; benzer bir proje için bilgi almak istiyorum.`)}" class="wa-float" target="_blank" rel="noopener noreferrer" aria-label="WhatsApp ile yazın (yeni sekmede açılır)">
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M12 2a10 10 0 0 0-8.6 15.1L2 22l5-1.3A10 10 0 1 0 12 2Zm0 18.2a8.2 8.2 0 0 1-4.2-1.2l-.3-.2-3 .8.8-2.9-.2-.3A8.2 8.2 0 1 1 12 20.2Zm4.5-6.1c-.2-.1-1.5-.7-1.7-.8-.2-.1-.4-.1-.6.1l-.8 1c-.1.2-.3.2-.5.1a6.7 6.7 0 0 1-3.3-2.9c-.3-.4.2-.4.7-1.4.1-.2 0-.3 0-.4l-.8-1.8c-.2-.5-.4-.4-.6-.4h-.5a1 1 0 0 0-.7.3 3 3 0 0 0-.9 2.2 5.2 5.2 0 0 0 1.1 2.8 11.9 11.9 0 0 0 4.6 4c1.7.7 2.4.8 3.2.7a2.8 2.8 0 0 0 1.8-1.3 2.3 2.3 0 0 0 .2-1.3c-.1-.1-.3-.2-.5-.3Z"/></svg>
    <span>WhatsApp</span>
  </a>
  </aside>

<!-- @include footer -->
<!-- @include drawer -->

  <script type="module" src="/src/js/main.js"></script>
</body>
</html>
`;
  writeFileSync(resolve(root, page.file), html);
  console.log('✓', page.file);
});
