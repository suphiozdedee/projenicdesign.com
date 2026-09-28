import * as ftp from 'basic-ftp';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const distDir = path.resolve(__dirname, '..', 'dist');

const host = process.env.FTP_HOST || process.argv[2];
const user = process.env.FTP_USER || process.argv[3];
const password = process.env.FTP_PASSWORD || process.argv[4];
const remotePath = process.env.FTP_REMOTE_PATH || process.argv[5] || 'public_html';

if (!host || !user || !password) {
  console.error('\n❌ Eksik parametre! Kullanım:');
  console.error('node scripts/deploy.js <HOST_IP> <USER> <PASSWORD> [REMOTE_PATH]\n');
  process.exit(1);
}

async function runDeploy() {
  const client = new ftp.Client();
  client.ftp.verbose = true;

  try {
    console.log(`\n🚀 Hostinger FTP Sunucusuna bağlanılıyor: ${host}...`);
    await client.access({
      host: host,
      user: user,
      password: password,
      secure: false // Hostinger standard FTP / FTPS
    });

    console.log(`✓ Bağlantı başarılı! Hedef dizin açılıyor: ${remotePath}`);
    await client.ensureDir(remotePath);

    console.log(`📦 dist/ klasöründeki tüm dosyalar ve .htaccess yükleniyor...`);
    await client.uploadFromDir(distDir);

    console.log(`\n🎉 BAŞARILI! Siteniz Hostinger üzerinde canlıya alındı.`);
    console.log(`🌐 Kontrol edin: https://projenicdesign.com\n`);
  } catch (err) {
    console.error(`\n❌ Yükleme sırasında hata oluştu:`, err.message);
  } finally {
    client.close();
  }
}

runDeploy();
