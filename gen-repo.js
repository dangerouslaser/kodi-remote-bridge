#!/usr/bin/env node
// Write the Homebrew Channel manifest and a one-package repository index for a built ipk.
// usage: node gen-repo.js <ipk-path> <ipk-download-url> <icon-url> [out-dir]
const fs = require('fs'), crypto = require('crypto'), path = require('path');
const [ipk, url, iconUri, outDir = '.'] = process.argv.slice(2);
if (!ipk || !url || !iconUri) { console.error('usage: gen-repo.js <ipk> <url> <icon-url> [out-dir]'); process.exit(2); }
const appinfo = JSON.parse(fs.readFileSync(path.join(__dirname, 'src', 'appinfo.json')));
const bytes = fs.readFileSync(ipk);
const manifest = {
  id: appinfo.id,
  version: appinfo.version,
  type: appinfo.type,
  title: appinfo.title,
  appDescription: appinfo.appDescription,
  iconUri,
  sourceUrl: 'https://github.com/dangerouslaser/kodi-remote-bridge',
  rootRequired: true,
  ipkUrl: url,
  ipkSize: bytes.length,
  ipkHash: { sha256: crypto.createHash('sha256').update(bytes).digest('hex') },
};
const repo = {
  paging: { page: 1, count: 1, maxPage: 1, itemsTotal: 1 },
  packages: [{ id: appinfo.id, title: appinfo.title, iconUri, manifest }],
};
fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(path.join(outDir, `${appinfo.id}.manifest.json`), JSON.stringify(manifest, null, 2));
fs.writeFileSync(path.join(outDir, 'repo.json'), JSON.stringify(repo, null, 2));
console.log('Wrote', path.join(outDir, 'repo.json'));
