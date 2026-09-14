// 原型静态服务器（常驻）：支持子目录、query、缓存禁用
const http = require('http');
const path = require('path');
const fs = require('fs');

const ROOT = __dirname;
http.createServer((req, res) => {
  let pathname = decodeURIComponent(req.url.split('?')[0]);
  if (pathname === '/') pathname = '/4.1-整体框架.html';
  let file = path.join(ROOT, pathname);
  if (!file.startsWith(ROOT)) { res.writeHead(403); res.end(); return; }
  try {
    const data = fs.readFileSync(file);
    const ext = path.extname(file);
    const type = ext === '.svg' ? 'image/svg+xml'
      : ext === '.json' ? 'application/json'
      : 'text/html; charset=utf-8';
    res.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' });
    res.end(data);
  } catch {
    res.writeHead(404); res.end('not found: ' + pathname);
  }
}).listen(8931, () => console.log('proto server (fixed) on 8931'));
