# Wiring the existing UI to the backend (UI/UX unchanged)

The web app currently keeps state in JS arrays persisted to `localStorage`
(`STORES`, `INV.products`, `INV.moves`, `LEDGER`, `EMPLOYEES`, …) and re-renders
from them. You do **not** change any render function, layout, or style — you only
change **where the data comes from and goes to.** The pattern:

## 1. Add the client + a login gate
```html
<script src="frontend/api-client.js"></script>
<script>SS_API.base = 'https://smokestack-api.onrender.com';</script>
```
Show a small login screen (or reuse Settings) that calls
`await SS_API.login(username, password)` and then runs the app's existing
`init()` / `refreshAll()`.

## 2. Replace reads: hydrate state from the API instead of localStorage
Where the app did `restore from localStorage`, do:
```js
async function hydrate() {
  STORES        = await SS_API.branches();
  INV.products  = await SS_API.products();
  LEDGER        = await SS_API.sales();       // + expenses/purchases as needed
  EMPLOYEES     = await SS_API.employees();
  refreshAll();                                // unchanged render pipeline
}
```
Keep the exact same in-memory shapes the renderers expect (map fields if names
differ). Nothing visual changes.

## 3. Replace writes: call the API, then update state + re-render
Anywhere the app pushed to an array + saved to localStorage, call the endpoint:
```js
// before: LEDGER.push({...}); lsSet(...); refreshAll();
// after:
const rec = await SS_API.addExpense({ branch, category, amount });
LEDGER.unshift(rec); refreshAll();             // same UI, now durable in Postgres
```
Do the same for products (`createProduct`), stock (`receive`/`adjust`),
employees (`addEmployee`/`deactivateEmployee`), payroll (`finalizePayroll`),
purchases, transfers, approvals (`approve`/`reject`), clock, etc.

## 4. Permissions & branches come from the token
`SS_API.user` has `{role, branches}`. The backend already enforces every rule,
so the UI keeps using `can()` for show/hide exactly as today — the server is the
real gate (a 403 surfaces as a thrown error you can toast).

## 5. Historical / as-of and reports
Swap the local computation for `SS_API.asOf(date, branch)` and
`SS_API.dashboard(branch)` — identical fields, now server-computed from the
immutable movement ledger, so they scale to millions of rows.

## Suggested rollout (no big-bang)
Migrate one module at a time behind a flag: Auth → Dashboard → Inventory →
Ledger (sales/expenses/purchases) → Payroll → Partners → Workflow. Each is a
read-swap + write-swap; the screens never change. This keeps the app shippable
throughout the migration.
