import * as ftp from 'basic-ftp';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, '..');
const distDir = path.resolve(rootDir, 'dist');
const envPath = path.resolve(rootDir, '.env');

if (fs.existsSync(envPath)) {
  try {
    const envContent = fs.readFileSync(envPath, 'utf8');
    envContent.split('\n').forEach(line => {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) return;
      const eqIdx = trimmed.indexOf('=');
      if (eqIdx > 0) {
        const key = trimmed.substring(0, eqIdx).trim();
        let val = trimmed.substring(eqIdx + 1).trim();
        if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
          val = val.substring(1, val.length - 1);
        }
        if (!process.env[key]) {
          process.env[key] = val;
        }
      }
    });
  } catch (err) {
    console.warn('⚠️ .env dosyası okunamadı:', err.message);
  }
}

const host = process.env.FTP_SERVER || process.env.FTP_HOST || process.argv[2];
const user = process.env.FTP_USERNAME || process.env.FTP_USER || process.argv[3];
const password = process.env.FTP_PASSWORD || process.argv[4];
const targetDir = process.env.FTP_REMOTE_PATH || process.argv[5] || '.';
const port = parseInt(process.env.FTP_PORT || '21', 10);

if (!host || !user || !password) {
  console.error('\n❌ Eksik FTP parametreleri!');
  process.exit(1);
}

if (!fs.existsSync(distDir)) {
  console.error('\n❌ dist/ klasörü bulunamadı! Lütfen önce "npm run build" çalıştırın.\n');
  process.exit(1);
}

/** dist/ içindeki dosyaların göreli yolları (ör. "assets/main-abc.css", "index.html"). */
function localFiles(dir, base = dir, out = new Set()) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) localFiles(full, base, out);
    else out.add(path.relative(base, full).split(path.sep).join('/'));
  }
  return out;
}

async function remoteFiles(client, dir, out = []) {
  for (const item of await client.list(dir)) {
    if (item.name === '.' || item.name === '..') continue;
    const rel = `${dir}/${item.name}`;
    if (item.isDirectory) await remoteFiles(client, rel, out);
    else out.push(rel);
  }
  return out;
}

/**
 * Sunucudaki eski dosyaları temizler. Kapsam bilerek dar tutulur:
 *  - assets/ altında yeni build'de olmayan dosyalar (site build'ine ait klasör)
 *  - kök dizindeki, yeni build'de olmayan .html sayfaları
 * api/, .htaccess ve diğer tüm sunucu klasörlerine dokunulmaz.
 * PRUNE=0 ile kapatılabilir.
 */
async function pruneOldFiles(client, rootPath) {
  if (process.env.PRUNE === '0') { console.log('↷ Temizlik kapalı (PRUNE=0).'); return; }
  const keep = localFiles(distDir);
  if ([...keep].filter(f => f.endsWith('.html')).length < 5) {
    console.warn('⚠️ dist/ beklenenden küçük; güvenlik için temizlik atlandı.');
    return;
  }

  await client.cd(rootPath);
  const rootList = await client.list('.');
  const candidates = rootList
    .filter(i => !i.isDirectory && i.name.endsWith('.html') && !keep.has(i.name))
    .map(i => i.name);
  if (rootList.some(i => i.isDirectory && i.name === 'assets')) {
    for (const f of await remoteFiles(client, 'assets')) if (!keep.has(f)) candidates.push(f);
  }

  if (candidates.length > 800) {
    console.warn(`⚠️ ${candidates.length} dosya silinecekti; beklenmedik derecede fazla, temizlik atlandı.`);
    return;
  }
  console.log(`🧹 Sunucuda eski ${candidates.length} dosya siliniyor...`);
  for (const f of candidates) {
    await client.remove(f);
    console.log('  − ' + f);
  }
  // Boşalan alt klasörleri kaldır (en derinden başlayarak)
  const dirs = [...new Set(candidates.filter(f => f.startsWith('assets/')).map(f => f.slice(0, f.lastIndexOf('/'))))]
    .sort((a, b) => b.split('/').length - a.split('/').length);
  for (const d of dirs) {
    if (keep.has(d) || [...keep].some(f => f.startsWith(d + '/'))) continue;
    try { await client.removeEmptyDir(d); console.log('  − ' + d + '/'); } catch { /* boş değilse kalsın */ }
  }
}

async function runDeploy() {
  const client = new ftp.Client();
  client.ftp.verbose = true;

  try {
    console.log(`\n🚀 Hostinger FTP Sunucusuna bağlanılıyor: ${host}:${port} (${user})...`);
    await client.access({
      host: host,
      user: user,
      password: password,
      port: port,
      secure: false
    });

    console.log(`✓ FTP Bağlantısı başarılı! Hedef dizin: ${targetDir}`);
    if (targetDir !== '.' && targetDir !== './') {
      await client.ensureDir(targetDir);
    }
    const rootPath = await client.pwd();

    console.log(`📦 dist/ klasöründeki tüm dosyalar ve .htaccess yükleniyor...`);
    await client.uploadFromDir(distDir);

    // Önce yükle, sonra temizle: site hiçbir an eksik dosyayla kalmaz
    await pruneOldFiles(client, rootPath);

    console.log(`\n🎉 BAŞARILI! Siteniz Hostinger üzerinde canlıya alındı.`);
    console.log(`🌐 Canlı Site: https://projenicdesign.com\n`);
  } catch (err) {
    console.error(`\n❌ Yükleme sırasında hata oluştu:`, err.message);
    process.exit(1);
  } finally {
    client.close();
  }
}

runDeploy();
