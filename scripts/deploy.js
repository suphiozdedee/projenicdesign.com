import * as ftp from 'basic-ftp';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, '..');
const distDir = path.resolve(rootDir, 'dist');
const envPath = path.resolve(rootDir, '.env');

// Simple .env parser to avoid requiring external dependencies
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
const remotePath = process.env.FTP_REMOTE_PATH || process.argv[5] || 'public_html';
const port = parseInt(process.env.FTP_PORT || '21', 10);

if (!host || !user || !password) {
  console.error('\n❌ Eksik FTP parametreleri!');
  console.error('\nLütfen .env dosyasına aşağıdaki değişkenleri tanımlayın veya komut satırından iletin:');
  console.error('  FTP_SERVER=<Hostinger_FTP_Sunucusu>');
  console.error('  FTP_USERNAME=<FTP_Kullanici_Adi>');
  console.error('  FTP_PASSWORD=<FTP_Sifresi>');
  console.error('  FTP_REMOTE_PATH=public_html (opsiyonel)');
  console.error('\nAlternatif doğrudan kullanım:');
  console.error('  node scripts/deploy.js <HOST> <USER> <PASSWORD> [REMOTE_PATH]\n');
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
    console.log(`\n🚀 Hostinger FTP Sunucusuna bağlanılıyor: ${host}:${port}...`);
    await client.access({
      host: host,
      user: user,
      password: password,
      port: port,
      secure: false // Hostinger standard FTP (port 21)
    });

    console.log(`✓ Bağlantı başarılı! Hedef dizin kontrol ediliyor: ${remotePath}`);
    await client.ensureDir(remotePath);

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
