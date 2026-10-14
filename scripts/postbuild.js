import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, '..');
const publicHtaccess = path.join(rootDir, 'public', '.htaccess');
const distHtaccess = path.join(rootDir, 'dist', '.htaccess');

if (fs.existsSync(publicHtaccess)) {
  fs.copyFileSync(publicHtaccess, distHtaccess);
  console.log('✓ Copied .htaccess to dist/.htaccess');
}

// Görsel adreslerine içerik özeti ekle (?v=…): fotoğraf aynı adla değiştirildiğinde
// bir yıllık tarayıcı önbelleği eski kareyi göstermesin; değişmeyen dosyanın adresi aynı kalır.
import crypto from 'crypto';
const distDir = path.join(rootDir, 'dist');
const hashes = new Map();
const verOf = (rel) => {
  if (!hashes.has(rel)) {
    const file = path.join(distDir, rel);
    hashes.set(rel, fs.existsSync(file) ? crypto.createHash('md5').update(fs.readFileSync(file)).digest('hex').slice(0, 8) : null);
  }
  return hashes.get(rel);
};
let tagged = 0;
for (const name of fs.readdirSync(distDir).filter((f) => f.endsWith('.html'))) {
  const file = path.join(distDir, name);
  const html = fs.readFileSync(file, 'utf8').replace(/\/assets\/images\/[\w./-]+\.(?:jpg|jpeg|png|webp)(?![\w?])/g, (url) => {
    const v = verOf(url.slice(1));
    if (!v) return url;
    tagged++;
    return `${url}?v=${v}`;
  });
  fs.writeFileSync(file, html);
}
console.log(`✓ ${tagged} görsel adresine sürüm eklendi`);
