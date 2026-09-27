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
