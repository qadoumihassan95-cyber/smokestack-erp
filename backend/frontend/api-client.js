/* SmokeStack ERP — browser API client.
   Drop this into the existing single-file app (or load as a module). It handles
   JWT login + authed JSON calls to the FastAPI backend. The UI stays identical;
   you only swap the app's data reads/writes to call these methods.

   Usage:
     SS_API.base = 'https://smokestack-api.onrender.com';
     await SS_API.login('U-owner','demo1234');   // stores token in memory
     const dash = await SS_API.dashboard();
*/
(function (global) {
  const state = { base: '', token: null, user: null };

  async function call(path, { method = 'GET', body, form } = {}) {
    const headers = {};
    if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
    let payload;
    if (form) { headers['Content-Type'] = 'application/x-www-form-urlencoded'; payload = new URLSearchParams(form).toString(); }
    else if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
    const res = await fetch(state.base + path, { method, headers, body: payload });
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;
    if (!res.ok) throw Object.assign(new Error((data && data.detail) || res.statusText), { status: res.status, data });
    return data;
  }

  const SS_API = {
    get base() { return state.base; }, set base(v) { state.base = v.replace(/\/$/, ''); },
    get user() { return state.user; },
    get token() { return state.token; },

    async login(username, password) {
      const r = await call('/api/auth/login', { method: 'POST', form: { username, password } });
      state.token = r.access_token; state.user = r.user; return r.user;
    },
    logout() { state.token = null; state.user = null; },
    me() { return call('/api/auth/me'); },

    // core / reports
    branches() { return call('/api/branches'); },
    dashboard(branch = 'all') { return call('/api/reports/dashboard?branch=' + encodeURIComponent(branch)); },
    dailyReport(branch = 'all') { return call('/api/reports/daily?branch=' + encodeURIComponent(branch)); },
    audit(limit = 100) { return call('/api/audit?limit=' + limit); },

    // inventory
    products(q = '', branch = 'all') { return call(`/api/inventory/products?q=${encodeURIComponent(q)}&branch=${encodeURIComponent(branch)}`); },
    productByBarcode(code) { return call('/api/inventory/barcode/' + encodeURIComponent(code)); },
    createProduct(p) { return call('/api/inventory/products', { method: 'POST', body: p }); },
    updateProduct(sku, p) { return call('/api/inventory/products/' + encodeURIComponent(sku), { method: 'PATCH', body: p }); },
    deactivateProduct(sku) { return call('/api/inventory/products/' + encodeURIComponent(sku) + '/deactivate', { method: 'POST' }); },
    reactivateProduct(sku) { return call('/api/inventory/products/' + encodeURIComponent(sku) + '/reactivate', { method: 'POST' }); },
    updateEmployee(id, e) { return call('/api/employees/' + encodeURIComponent(id), { method: 'PUT', body: e }); },
    receive(sku, branch, qty) { return call('/api/inventory/receive', { method: 'POST', body: { sku, branch, qty } }); },
    adjust(sku, branch, qty, reason) { return call('/api/inventory/adjust', { method: 'POST', body: { sku, branch, qty, reason } }); },
    movements(branch = 'all', start = '', end = '') { return call(`/api/inventory/movements?branch=${encodeURIComponent(branch)}&start=${start}&end=${end}`); },
    asOf(date, branch = 'all') { return call(`/api/inventory/asof?date=${date}&branch=${encodeURIComponent(branch)}`); },

    // ledger
    sales(branch = 'all') { return call('/api/sales?branch=' + encodeURIComponent(branch)); },
    addSale(s) { return call('/api/sales', { method: 'POST', body: s }); },
    expenses(branch = 'all') { return call('/api/expenses?branch=' + encodeURIComponent(branch)); },
    addExpense(e) { return call('/api/expenses', { method: 'POST', body: e }); },
    purchases(branch = 'all') { return call('/api/purchases?branch=' + encodeURIComponent(branch)); },
    addPurchase(p) { return call('/api/purchases', { method: 'POST', body: p }); },

    // hr
    employees(branch = 'all') { return call('/api/employees?branch=' + encodeURIComponent(branch)); },
    addEmployee(e) { return call('/api/employees', { method: 'POST', body: e }); },
    deactivateEmployee(id) { return call(`/api/employees/${id}/deactivate`, { method: 'POST' }); },
    payroll(start, end, branch = 'all') { return call(`/api/payroll?start=${start}&end=${end}&branch=${encodeURIComponent(branch)}`); },
    finalizePayroll(start, end, branch = 'all') { return call(`/api/payroll/finalize?start=${start}&end=${end}&branch=${encodeURIComponent(branch)}`, { method: 'POST' }); },

    // partners
    customers() { return call('/api/customers'); },
    customer(id) { return call('/api/customers/' + encodeURIComponent(id)); },
    suppliers() { return call('/api/suppliers'); },
    supplier(id) { return call('/api/suppliers/' + encodeURIComponent(id)); },

    // workflow
    transfers() { return call('/api/transfers'); },
    createTransfer(t) { return call('/api/transfers', { method: 'POST', body: t }); },
    approvals() { return call('/api/approvals'); },
    approve(id, comment = '') { return call(`/api/approvals/${id}/approve`, { method: 'POST', body: { comment } }); },
    reject(id, comment = '') { return call(`/api/approvals/${id}/reject`, { method: 'POST', body: { comment } }); },
    clock(employee, branch, direction) { return call('/api/clock', { method: 'POST', body: { employee, branch, direction } }); },

    // telegram linking
    issueTelegramCode() { return call('/api/telegram/link/issue', { method: 'POST' }); },
  };

  global.SS_API = SS_API;
  if (typeof module !== 'undefined') module.exports = SS_API;
})(typeof window !== 'undefined' ? window : globalThis);
