/* Navigation regression test for Task 4 — Profit & Loss "Back".
 *
 * A drilled-in report (e.g. Profit & Loss) is a SUB-VIEW of Reports: opening it
 * sets REP.current and re-renders without a NAV push, so the NAV stack still
 * points at whatever preceded Reports (usually Dashboard). The fix makes global
 * Back / browser Back / swipe-back CLOSE the open report and return to the
 * Reports list instead of jumping to the Dashboard.
 *
 * This test extracts the real shipped goBack()/_reportOpen() source from
 * index.html and drives it with stubs (no jsdom dependency, matching the repo's
 * plain-node frontend test convention). It fails if the guard is removed.
 *
 * Run:  node backend/tests/e2e_pnl_back_nav.js
 */
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', '..', 'index.html'), 'utf8');
let pass = 0; const fail = [];
const ok = (c, m) => { if (c) pass++; else fail.push(m); };

// 1) Structural: the guard must be wired into BOTH goBack and the popstate handler.
ok(/function _reportOpen\(\)/.test(html), '_reportOpen() helper present');
ok(/function goBack\(\)\{[\s\S]*?_reportOpen\(\)[\s\S]*?repBackCat\(\)/.test(html),
   'goBack() closes the open report before popping the NAV stack');
ok(/addEventListener\('popstate'[\s\S]*?_reportOpen\(\)[\s\S]*?repBackCat\(\)/.test(html),
   'popstate (browser Back) closes the open report first');

// 2) Behavioral: extract the real source of _reportOpen + goBack and execute it.
const start = html.indexOf('function _reportOpen()');
const END_MARK = "else{go('dash',{back:true});}\n}";      // exact end of goBack()
const end = html.indexOf(END_MARK, start) + END_MARK.length;
const src = html.slice(start, end);
ok(src.includes('function goBack()'), 'extracted goBack() source');

const factory = new Function(
  '_anyOverlay', 'uiCloseAll', 'repBackCat', 'go', 'NAV', 'REP', '_view',
  src + '\n; return { goBack: goBack, _reportOpen: _reportOpen };'
);

function run({ view, reportOpen, stack }) {
  const calls = [];
  const NAV = { stack: stack.slice() };
  const REP = { current: reportOpen ? 'pnl' : null };
  const api = factory(
    () => false,                       // _anyOverlay
    () => calls.push('uiCloseAll'),    // uiCloseAll
    () => calls.push('repBackCat'),    // repBackCat
    (v) => calls.push('go:' + v),      // go
    NAV, REP,
    () => view                         // _view
  );
  api.goBack();
  return { calls, NAV };
}

// Case A: in Reports with P&L open → Back closes the report (NOT go to dash).
let r = run({ view: 'reports', reportOpen: true, stack: ['dash'] });
ok(r.calls.includes('repBackCat'), 'A: P&L open → repBackCat called');
ok(!r.calls.some(c => c.startsWith('go:')), 'A: P&L open → does NOT navigate away');
ok(r.NAV.stack.length === 1, 'A: NAV stack preserved (Reports/Dashboard intact)');

// Case B: in Reports with no report open → Back pops the stack normally.
r = run({ view: 'reports', reportOpen: false, stack: ['dash'] });
ok(r.calls.includes('go:dash') && !r.calls.includes('repBackCat'),
   'B: no report open → normal Back to previous view');

// Case C: elsewhere (dashboard) → unchanged normal behavior.
r = run({ view: 'dash', reportOpen: false, stack: ['sales'] });
ok(r.calls.includes('go:sales'), 'C: non-reports view → normal Back unaffected');

console.log(`\n=== P&L BACK-NAV REGRESSION: PASS ${pass}  FAIL ${fail.length} ===`);
if (fail.length) { fail.forEach(f => console.log('  FAIL:', f)); process.exit(1); }
