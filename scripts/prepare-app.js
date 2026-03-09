/**
 * 构建前准备：清空 dist-win 与 .electron-app，并把 Python 应用拷到 .electron-app，供 electron-builder 单目录复制，减少 EPERM。
 */
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const staging = path.join(root, '.electron-app');
const outDir = path.join(root, 'dist-win');
const pythonEmbedDir = path.join(root, 'python-3.11.8-embed-amd64');

function rm(dir) {
  if (fs.existsSync(dir)) fs.rmSync(dir, { recursive: true, force: true });
}

function copy(src, dest) {
  const stat = fs.statSync(src);
  if (stat.isDirectory()) {
    fs.mkdirSync(dest, { recursive: true });
    for (const name of fs.readdirSync(src)) {
      copy(path.join(src, name), path.join(dest, name));
    }
  } else {
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(src, dest);
  }
}

rm(outDir);
rm(staging);
fs.mkdirSync(staging, { recursive: true });
copy(path.join(root, 'main.py'), path.join(staging, 'main.py'));
copy(path.join(root, 'src'), path.join(staging, 'src'));
copy(path.join(root, 'requirements.txt'), path.join(staging, 'requirements.txt'));
if (fs.existsSync(pythonEmbedDir)) {
  copy(pythonEmbedDir, path.join(staging, 'python'));
  console.log('prepare-app: embedded Python copied');
} else {
  console.warn('prepare-app: python-3.11.8-embed-amd64 not found, skip embedding Python');
}
console.log('prepare-app: .electron-app ready');
