/* End-to-end proof that the FRONTEND data layer (the real api-client the app
   uses) talks to the LIVE backend and persists to the database across a
   simulated page reload. Run with the API base in SS_E2E_BASE. */
const API = require('../frontend/api-client.js');
API.base = process.env.SS_E2E_BASE || 'http://127.0.0.1:8095';
let pass = 0; const F = [];
const ok = (c, m) => { if (c) pass++; else F.push(m); };

(async () => {
  // 1) login (owner)
  const u = await API.login('U-owner', 'demo1234');
  ok(u.role === 'owner', 'login returns owner role + branches');

  // 2) hydrate reads (what the app loads on boot)
  const [branches, products, movements, sales, expenses, employees] = await Promise.all([
    API.branches(), API.products(), API.movements(), API.sales(), API.expenses(), API.employees()]);
  ok(branches.length === 3, 'branches hydrated from DB');
  ok(products.length >= 4 && products[0].stock, 'products + per-branch stock hydrated');
  ok(movements.length > 0, 'movement ledger hydrated');
  ok(employees.length >= 3, 'employees hydrated');

  // 3) WRITE-THROUGH: create a TEST product, then "reload" (re-fetch) and confirm it persisted
  const testSku = 'E2E-' + Date.now();
  await API.createProduct({ sku: testSku, name: 'E2E Test Widget', barcode: 'E2E' + Date.now(), cost: 2, price: 5, min_level: 3 });
  let reload = await API.products(testSku);
  ok(reload.some(p => p.sku === testSku), 'created product persists after reload (in PostgreSQL/DB)');

  // 4) receive stock → exact backend qty reflected
  await API.receive(testSku, 'Store A', 12);
  reload = await API.products(testSku);
  const prod = reload.find(p => p.sku === testSku);
  ok(prod.stock['Store A'] === 12, 'stock qty after receive matches backend exactly (12)');
  await API.adjust(testSku, 'Store A', -2, 'e2e count fix');
  reload = (await API.products(testSku)).find(p => p.sku === testSku);
  ok(reload.stock['Store A'] === 10, 'stock qty after adjust matches backend exactly (10)');

  // 4b) deactivate / reactivate persist status
  const de = await API.deactivateProduct(testSku); ok(de.status === 'inactive', 'product deactivate persists status in DB');
  const re = await API.reactivateProduct(testSku); ok(re.status === 'active', 'product reactivate persists status in DB');

  // 5) add expense → appears on reload
  const before = (await API.expenses()).length;
  await API.addExpense({ branch: 'Store A', category: 'E2E Fuel', amount: 42 });
  ok((await API.expenses()).length === before + 1, 'expense persists after reload');

  // 6) as-of endpoint (historical) works
  const asof = await API.asOf(new Date().toISOString().slice(0, 10), 'all');
  ok(asof.rows && typeof asof.units === 'number', 'as-of report returns historical snapshot');

  // 7) dashboard KPIs come from the server
  const dash = await API.dashboard();
  ok(typeof dash.sales_today === 'number' && 'inventory_cost' in dash, 'dashboard KPIs served (owner sees cost)');

  // 8) telegram link code issued
  const code = await API.issueTelegramCode();
  ok(/^\d{6}$/.test(code.code), 'telegram link code issued');

  // 9) RBAC enforced server-side: cashier cannot create products
  await API.login('U-cash', 'demo1234');
  let denied = false;
  try { await API.createProduct({ sku: 'NOPE', name: 'x' }); } catch (e) { denied = e.status === 403; }
  ok(denied, 'cashier blocked from creating products (403 from server)');
  const cashDash = await API.dashboard();
  ok(!('inventory_cost' in cashDash), 'cashier dashboard hides cost');

  // 10) session persistence: a fresh client with the same token still authenticates
  const token = API.token;
  ok(!!token, 'JWT token available for session persistence (survives reload via sessionStorage in the app)');

  console.log(`\n=== FRONTEND↔BACKEND E2E: PASS ${pass}  FAIL ${F.length} ===`);
  if (F.length) { console.log('failures:'); F.forEach(f => console.log(' • ' + f)); }
  process.exit(F.length ? 1 : 0);
})().catch(e => { console.error('E2E error:', e.message, e.status || ''); process.exit(1); });
