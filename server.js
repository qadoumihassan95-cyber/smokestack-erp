'use strict';
const path = require('path');
const express = require('express');

const app = express();
const PORT = process.env.PORT || 3000;   // Render injects PORT
const HOST = '0.0.0.0';                  // required by Render
const NODE_ENV = process.env.NODE_ENV || 'production';

app.disable('x-powered-by');

app.use((req, res, next) => {
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('X-Frame-Options', 'SAMEORIGIN');
  res.setHeader('Referrer-Policy', 'no-referrer');
  next();
});

app.use((req, res, next) => {
  const t = Date.now();
  res.on('finish', () => console.log(JSON.stringify({
    level: 'info', method: req.method, path: req.path, status: res.statusCode, ms: Date.now() - t
  })));
  next();
});

app.get('/health', (_req, res) => res.status(200).json({ status: 'ok', uptime: Math.round(process.uptime()) }));

app.get('/', (_req, res) => res.sendFile(path.join(__dirname, 'index.html')));
app.get('/erp', (_req, res) => res.sendFile(path.join(__dirname, 'erp-v2-preview.html')));

app.use((req, res) => {
  if (req.accepts('html')) {
    return res.status(404).send('<body style="font-family:system-ui;background:#0f1420;color:#e8edf7;display:flex;align-items:center;justify-content:center;height:100vh;margin:0"><div style="text-align:center"><h1>404</h1><p>Page not found.</p><a style="color:#ff7a45" href="/">&larr; Back to SmokeStack</a></div></body>');
  }
  res.status(404).json({ error: 'not_found' });
});

app.use((err, _req, res, _next) => {
  console.error(JSON.stringify({ level: 'error', message: err.message }));
  res.status(500).json({ error: 'internal_error' });
});

const server = app.listen(PORT, HOST, () => console.log(`SmokeStack listening on ${HOST}:${PORT} (${NODE_ENV})`));
process.on('SIGTERM', () => server.close(() => process.exit(0)));
