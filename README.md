# SmokeStack — Accounting-first ERP for U.S. smoke shops

Production web service (Node + Express) serving the SmokeStack application.

- App entry: `server.js`
- Static app: `public/index.html`
- Health check: `GET /health` -> `{"status":"ok"}`

## Run locally
npm install && npm start   # http://localhost:3000

## Deploy (Render)
Build Command: `npm install`
Start Command: `npm start`
Health Check Path: `/health`
No database or secrets required (client-side state).
