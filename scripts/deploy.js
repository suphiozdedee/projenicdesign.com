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

    console.log(`📦 dist/ klasöründeki tüm dosyalar ve .htaccess yükleniyor...`);
    await client.uploadFromDir(distDir);

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
