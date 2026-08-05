/* UI wiring test for the Owner-only User Management page (Task: secure user mgmt).
 *
 * Verifies the page + client are wired to the live, server-enforced endpoints and
 * that the nav link is gated to Owners. Structural checks read the shipped
 * index.html; the behavioural check extracts the real syncNav()/isOwner() source
 * and drives it with stubs (matches the repo's plain-node frontend test style —
 * see e2e_pnl_back_nav.js). No secrets are referenced by the UI.
 *
 * Run:  node backend/tests/e2e_user_management.js
 */
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', '..', 'index.html'), 'utf8');
let pass = 0; const fail = [];
const ok = (c, m) => { if (c) pass++; else fail.push(m); };

// 1) Nav link exists, is Owner-gated (hidden by default), and points at the view.
ok(/<a data-v="users" id="navUsers"[^>]*style="display:none"/.test(html), 'nav link #navUsers present and hidden by default');
ok(/id="v-users"/.test(html), 'v-users view section present');
ok(/id="usrRows"/.test(html) && /id="usrAddBtn"/.test(html) && /id="usrGuard"/.test(html), 'users view has table body, add button, owner guard');

// 2) Client is wired to the server-enforced endpoints (PUT/activate/deactivate/reset).
ok(/listUsers:function\(\)\{return call\('\/api\/users'\)/.test(html), 'client.listUsers -> GET /api/users');
ok(/createUser:function\(b\)\{return call\('\/api\/users',\{method:'POST'/.test(html), 'client.createUser -> POST /api/users');
ok(/updateUser:function\(id,b\)\{return call\('\/api\/users\/'\+encodeURIComponent\(id\),\{method:'PUT'/.test(html), 'client.updateUser -> PUT /api/users/{id}');
ok(/activateUser:function\(id\)\{return call\('\/api\/users\/'\+encodeURIComponent\(id\)\+'\/activate'/.test(html), 'client.activateUser -> POST .../activate');
ok(/deactivateUser:function\(id\)\{return call\('\/api\/users\/'\+encodeURIComponent\(id\)\+'\/deactivate'/.test(html), 'client.deactivateUser -> POST .../deactivate');
ok(/resetUserPassword:function\(id\)\{return call\('\/api\/users\/'\+encodeURIComponent\(id\)\+'\/reset-password'/.test(html), 'client.resetUserPassword -> POST .../reset-password');

// 3) Module wiring: gate, render, one-time password reveal, guard on 403.
const mod = html.slice(html.indexOf('Owner-only User Management'));
ok(/function syncNav\(\)\{[^}]*navUsers[^}]*isOwner\(\)/.test(mod), 'syncNav toggles the nav by owner role');
ok(/API\.listUsers\(\)\.then/.test(mod), 'render() loads accounts from the server');
ok(/showTempPassword/.test(mod) && /temp_password/.test(mod), 'one-time password is surfaced via showTempPassword');
ok(/API\.createUser\(body\)/.test(mod) && /API\.resetUserPassword/.test(mod) && /API\.deactivateUser/.test(mod) && /API\.updateUser/.test(mod), 'all management actions call the API client');
ok(/if\(guard\)guard\.style\.display=''/.test(mod), 'a 403 (non-owner) reveals the Owner-access-required guard');
ok(!/password_hash/.test(mod), 'UI never references stored password hashes');

// 4) Behavioural: extract the REAL isOwner()/syncNav() and prove the gate works.
const s = mod.indexOf('function esc(');           // start of helper block
const iso = mod.indexOf('function isOwner(');
const syncEnd = mod.indexOf('// ---- lightweight modal');
const src = mod.slice(iso, syncEnd);              // isOwner + tt + syncNav
const fakeEl = { style: { display: 'START' } };
global.window = global;                            // window.ROLE === ROLE (both global)
global.document = { getElementById: (id) => (id === 'navUsers' ? fakeEl : null) };
global.toast = function () {};
// eslint-disable-next-line no-eval
eval(src + '\nglobal.__syncNav = syncNav;');
global.ROLE = { kind: 'owner' }; global.__syncNav();
ok(fakeEl.style.display === '', 'owner -> nav link shown');
global.ROLE = { kind: 'employee' }; global.__syncNav();
ok(fakeEl.style.display === 'none', 'non-owner -> nav link hidden');
global.ROLE = null; global.__syncNav();
ok(fakeEl.style.display === 'none', 'no role yet -> nav link hidden');
void s;

console.log(`\n=== USER MANAGEMENT UI WIRING: PASS ${pass}  FAIL ${fail.length} ===`);
if (fail.length) { fail.forEach(f => console.log('  FAIL:', f)); process.exit(1); }
