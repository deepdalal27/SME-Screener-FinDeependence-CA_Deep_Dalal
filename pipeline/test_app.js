/* Tests the screener app's own logic (metric registry, filter engine, sorting,
   derived metrics, CSV) by extracting the <script> from index.html and running
   it headless with a stub DOM. Run:  node pipeline/test_app.js  */
const fs = require('fs');
const path = require('path');
const ROOT = path.join(__dirname, '..');

let PASS = 0, FAIL = 0;
function ok(name, cond, extra) {
  if (cond) PASS++; else { FAIL++; console.log('  FAIL ' + name + (extra ? ' — ' + extra : '')); }
}
function eq(name, got, want, tol) {
  tol = tol == null ? 1e-6 : tol;
  const good = (got == null && want == null) ||
    (typeof got === 'number' && typeof want === 'number' && Math.abs(got - want) <= tol * Math.max(1, Math.abs(want))) ||
    got === want;
  ok(name, good, 'got ' + got + ', want ' + want);
}

/* ---- stub DOM good enough for the app's module-level code ---- */
const noop = () => {};
function stubEl() {
  const el = {
    style: {}, dataset: {}, textContent: '', innerHTML: '', value: '', classList: {
      add: noop, remove: noop, toggle: noop, contains: () => false
    },
    querySelectorAll: () => [], querySelector: () => stubEl(),
    addEventListener: noop, appendChild: noop, click: noop, onclick: null, oninput: null,
  };
  return el;
}
const EL = {};
global.document = {
  getElementById: (id) => (EL[id] = EL[id] || stubEl()),
  querySelectorAll: () => [],
  createElement: () => stubEl(),
  addEventListener: noop,
  head: { appendChild: noop },
  body: { style: {} },
};
global.window = {};
global.localStorage = {
  _d: {}, getItem(k) { return this._d[k] ?? null; },
  setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; },
};
global.fetch = () => Promise.reject(new Error('no network in test'));
global.crypto = { randomUUID: () => 'test-uuid' };

/* ---- extract and eval the app script ---- */
const html = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
ok('index.html has exactly one inline script', scripts.length === 1, 'found ' + scripts.length);
const code = scripts[scripts.length - 1];

// expose internals for testing
const wrapped = code + '\n;module.exports={M,MBY,GROUPS,g,state,rows,passes,fmt,cls,PRESETS,READY,' +
  'checkCode,fdHash,setData:d=>{DATA=d;},renderTable,renderFilters,renderSectors,renderCols,' +
  'renderScreens,renderQuals,openDetail,exportCSV,marketOpen,istNow,LIVE,shownPrice,srcHTML,QLABEL};';
const mod = { exports: {} };
new Function('module', 'exports', 'require', wrapped)(mod, mod.exports, require);
const A = mod.exports;

console.log('== registry ==');
ok('registry non-empty', A.M.length >= 40, A.M.length + ' metrics');
ok('no duplicate metric keys', new Set(A.M.map(m => m.k)).size === A.M.length);
const wanted = ['mcap', 'op', 'opm', 'ebitda', 'ebitda_pct', 'dep', 'ebit', 'ebit_pct', 'tax',
  'pat', 'pat_pct', 'eps', 'pe', 'cfo_pat', 'fcf_pat', 'capex'];
wanted.forEach(k => ok('user-requested metric present: ' + k, !!A.MBY[k]));
const extras = ['roe', 'roce', 'de', 'int_cov', 'sales_g1', 'pat_g1', 'sales_g3', 'pat_g3',
  'promoter', 'bvps', 'pb', 'div_yield', 'hi52', 'lo52', 'ret_1m', 'ret_3m', 'ret_1y'];
extras.forEach(k => ok('extra metric present: ' + k, !!A.MBY[k]));
A.M.forEach(m => ok('metric ' + m.k + ' has label+group', !!m.label && !!m.grp));
Object.values(A.PRESETS).forEach((p, i) =>
  p.forEach(k => ok('preset[' + i + '] key ' + k + ' is real', !!A.MBY[k])));
A.READY.forEach(s => s.f.forEach(f => ok('ready screen "' + s.n + '" key ' + f[0] + ' is real', !!A.MBY[f[0]])));

console.log('== every registry key is produced by the pipeline ==');
const sample = JSON.parse(fs.readFileSync(path.join(ROOT, 'data/sme_data.json'), 'utf8'));
const keysInData = new Set(Object.keys(sample.companies[0].m));
A.M.filter(m => !m.calc && m.type !== 'date').forEach(m =>
  ok('pipeline emits ' + m.k, keysInData.has(m.k)));
// date metrics are read from the company root, so verify they land there
A.M.filter(m => m.type === 'date').forEach(m =>
  ok('pipeline emits ' + m.k + ' at company root',
     sample.companies.some(c => c[m.k] != null)));

console.log('== derived (calc) metrics ==');
const c1 = { m: { price: 200, hi52: 250, lo52: 100, pe: 20 } };
eq('off 52w high', A.g(c1, 'off_high'), -20);
eq('up from 52w low', A.g(c1, 'up_low'), 100);
eq('earnings yield', A.g(c1, 'ev'), 5);
const c2 = { m: { price: 200, hi52: 0, pe: 0 } };
eq('off high null when hi52=0', A.g(c2, 'off_high'), null);
eq('earnings yield null when pe<=0', A.g(c2, 'ev'), null);
eq('missing metric -> null', A.g({ m: {} }, 'roce'), null);
eq('memoised calc stable', A.g(c1, 'off_high'), A.g(c1, 'off_high'));

console.log('== filter engine ==');
const mk = (o, ex, sec) => ({ name: o.name || 'X Ltd', symbol: 'X', exchange: ex || 'NSE-EMERGE', sector: sec || 'Chemicals', m: o });
A.setData({
  companies: [
    mk({ mcap: 100, roce: 25, de: 0.2, pat: 10, pe: 12, opm: 18 }, 'NSE-EMERGE', 'Chemicals'),
    mk({ mcap: 500, roce: 10, de: 1.5, pat: 40, pe: 30, opm: 9 }, 'BSE-SME', 'Textiles'),
    mk({ mcap: 250, roce: 22, de: 0.0, pat: 20, pe: 18, opm: 25 }, 'NSE-EMERGE', 'Textiles'),
    mk({ mcap: 80, roce: null, de: null, pat: 2, pe: null, opm: null }, 'BSE-SME', 'Chemicals'),
  ]
});
const S = A.state;
function reset() { S.q = ''; S.ex = 'all'; S.sectors = new Set(); S.quals = new Set();
  S.filters = []; S.sort = { k: 'mcap', dir: -1 }; }

reset();
eq('no filters -> all rows', A.rows().length, 4);
eq('default sort mcap desc', A.rows()[0].m.mcap, 500);
S.sort = { k: 'mcap', dir: 1 };
eq('sort asc', A.rows()[0].m.mcap, 80);

reset(); S.ex = 'NSE-EMERGE';
eq('exchange filter', A.rows().length, 2);
reset(); S.ex = 'BSE-SME';
eq('exchange filter BSE', A.rows().length, 2);

reset(); S.sectors = new Set(['Chemicals']);
eq('sector filter', A.rows().length, 2);
reset(); S.sectors = new Set(['Chemicals', 'Textiles']);
eq('multi sector filter', A.rows().length, 4);

reset(); S.filters = [{ k: 'roce', op: 'gte', v: 20, v2: '' }];
eq('roce >= 20', A.rows().length, 2);
reset(); S.filters = [{ k: 'roce', op: 'gte', v: 20, v2: '' }, { k: 'de', op: 'lte', v: 0.1, v2: '' }];
eq('AND of two criteria', A.rows().length, 1);
eq('AND result is right company', A.rows()[0].m.mcap, 250);

reset(); S.filters = [{ k: 'mcap', op: 'bt', v: 90, v2: 300 }];
eq('between', A.rows().length, 2);
reset(); S.filters = [{ k: 'mcap', op: 'bt', v: '', v2: 100 }];
eq('between with open lower bound', A.rows().length, 2);

reset(); S.filters = [{ k: 'pe', op: 'nn', v: '', v2: '' }];
eq('has value operator', A.rows().length, 3);
reset(); S.filters = [{ k: 'roce', op: 'gte', v: 20, v2: '' }];
ok('null data excluded when criterion set', !A.rows().some(c => c.m.roce == null));
reset(); S.filters = [{ k: 'roce', op: 'gte', v: '', v2: '' }];
eq('blank value ignores criterion', A.rows().length, 4);
reset(); S.filters = [{ k: 'pe', op: 'lt', v: 20, v2: '' }];
eq('less-than', A.rows().length, 2);
reset(); S.filters = [{ k: 'de', op: 'eq', v: 0, v2: '' }];
eq('equals', A.rows().length, 1);

reset(); S.q = 'textiles';
eq('search matches sector', A.rows().length, 2);
reset(); S.q = 'zzz';
eq('search no match', A.rows().length, 0);

reset(); S.sort = { k: 'roce', dir: -1 };
const sorted = A.rows();
eq('sort desc by roce top', sorted[0].m.roce, 25);
eq('nulls sort last (desc)', sorted[sorted.length - 1].m.roce, null);
S.sort = { k: 'roce', dir: 1 };
eq('nulls sort last (asc too)', A.rows()[A.rows().length - 1].m.roce, null);

console.log('== formatting ==');
ok('null renders dash', A.fmt(null, A.MBY.pe).includes('—'));
ok('percent suffix', A.fmt(18.3, A.MBY.opm).includes('%'));
ok('multiple suffix', A.fmt(1.25, A.MBY.cfo_pat).includes('x'));
ok('large number grouped', A.fmt(123456, A.MBY.mcap).includes(','));
eq('negative pat flagged', A.cls(-5, A.MBY.pat), 'neg');
eq('positive return flagged', A.cls(12, A.MBY.ret_1y), 'pos');
eq('negative return flagged', A.cls(-12, A.MBY.ret_1y), 'neg');

console.log('== license codes (same scheme as IPO tracker) ==');
function makeCode(daysFromNow) {
  const d = new Date(Date.now() + daysFromNow * 864e5);
  const p = n => String(n).padStart(2, '0');
  const payload = p(d.getFullYear() % 100) + p(d.getMonth() + 1) + p(d.getDate()) + 'AB';
  return 'FD-' + payload + '-' + A.fdHash(payload + 'FD!FreedomWithFinance#DeepDalal$2026');
}
ok('valid future code accepted', A.checkCode(makeCode(30)) instanceof Date);
eq('expired code -> false', A.checkCode(makeCode(-5)), false);
eq('garbage -> null', A.checkCode('FD-999999-XXXX'), null);
eq('empty -> null', A.checkCode(''), null);
ok('lowercase code accepted', A.checkCode(makeCode(30).toLowerCase()) instanceof Date);

console.log('== real sample dataset sanity ==');
A.setData(sample);
reset();
eq('sample rows all pass with no filters', A.rows().length, sample.companies.length);
// pick a filter over a metric the current dataset actually carries
const liveMetric = ['price', 'roce', 'ret_issue'].find(k =>
  sample.companies.filter(c => A.g(c, k) != null).length > 5);
ok('dataset carries at least one screenable metric', !!liveMetric, String(liveMetric));
reset();
const vals = sample.companies.map(c => A.g(c, liveMetric)).filter(v => v != null).sort((a, b) => a - b);
const median = vals[Math.floor(vals.length / 2)];
S.filters = [{ k: liveMetric, op: 'gte', v: median, v2: '' }];
const q = A.rows();
ok('screen returns a real subset', q.length > 0 && q.length < sample.companies.length, q.length + ' rows');
ok('screen results really satisfy the criterion', q.every(c => A.g(c, liveMetric) >= median));
reset(); S.filters = [{ k: liveMetric, op: 'lte', v: median, v2: '' }, { k: liveMetric, op: 'gte', v: 0, v2: '' }];
ok('two filters on same metric both apply',
  A.rows().every(c => { const v = A.g(c, liveMetric); return v >= 0 && v <= median; }));
ok('every company has a sector', sample.companies.every(c => !!c.sector));
ok('mcap positive wherever present', sample.companies.every(c => c.m.mcap == null || c.m.mcap > 0));
ok('price positive wherever present', sample.companies.every(c => c.m.price == null || c.m.price > 0));
ok('no fabricated zeros standing in for unknowns',
  sample.companies.every(c => c.m.revenue !== 0 && c.m.pat !== 0));

console.log('== listing metrics in the registry ==');
['listing_date', 'age_yrs', 'issue_price', 'list_open', 'list_close', 'list_pop', 'ret_issue',
 'ret_listclose', 'cagr_issue', 'cagr_listclose', 'ath', 'off_ath', 'bench_ret', 'alpha']
  .forEach(k => ok('listing metric present: ' + k, !!A.MBY[k]));
ok('Listing is its own group', A.GROUPS.includes('Listing'));
eq('listing_date is a date-type metric', A.MBY.listing_date.type, 'date');

console.log('== date metric handling ==');
const dc = { listing_date: '2024-03-15', m: {} };
eq('date read from company root', A.g(dc, 'listing_date'), '2024-03-15');
eq('date read from m as fallback', A.g({ m: { listing_date: '2022-01-02' } }, 'listing_date'), '2022-01-02');
eq('missing date -> null', A.g({ m: {} }, 'listing_date'), null);
ok('date formatted human-readably', /Mar/.test(A.fmt('2024-03-15', A.MBY.listing_date)));
ok('null date renders dash', A.fmt(null, A.MBY.listing_date).includes('—'));
eq('date column never colour-coded', A.cls('2024-03-15', A.MBY.listing_date), '');

console.log('== date filters ==');
const mkd = (d, ex) => ({ name: 'D', symbol: 'D', exchange: ex || 'NSE-EMERGE', sector: 'X',
  quality: 'exchange', listing_date: d, m: { mcap: 10 } });
A.setData({ companies: [mkd('2020-01-01'), mkd('2023-06-15'), mkd('2025-12-31'), mkd(null)] });
reset();
S.filters = [{ k: 'listing_date', op: 'gte', v: '2023-01-01', v2: '' }];
eq('listed on/after date', A.rows().length, 2);
S.filters = [{ k: 'listing_date', op: 'lte', v: '2023-01-01', v2: '' }];
eq('listed on/before date', A.rows().length, 1);
S.filters = [{ k: 'listing_date', op: 'bt', v: '2022-01-01', v2: '2024-01-01' }];
eq('listed between two dates', A.rows().length, 1);
S.filters = [{ k: 'listing_date', op: 'nn', v: '', v2: '' }];
eq('has a listing date', A.rows().length, 3);
S.filters = [{ k: 'listing_date', op: 'gte', v: '', v2: '' }];
eq('blank date criterion ignored', A.rows().length, 4);

console.log('== data-quality filter ==');
const mkq = q => ({ name: q, symbol: q, exchange: 'NSE-EMERGE', sector: 'X', quality: q, m: { mcap: 1 } });
A.setData({ companies: [mkq('exchange'), mkq('exchange'), mkq('thirdparty'), mkq('price-only')] });
reset();
S.quals = new Set(['exchange']);
eq('only exchange-sourced', A.rows().length, 2);
S.quals = new Set(['exchange', 'thirdparty']);
eq('exchange + third party', A.rows().length, 3);
S.quals = new Set();
eq('no quality filter -> all', A.rows().length, 4);
A.setData({ companies: [{ name: 'x', symbol: 'x', exchange: 'NSE-EMERGE', sector: 'X', m: {} }] });
reset(); S.quals = new Set(['price-only']);
eq('missing quality treated as price-only', A.rows().length, 1);
ok('quality labels defined', A.QLABEL.exchange && A.QLABEL.thirdparty && A.QLABEL['price-only']);

console.log('== market hours (IST, device-timezone independent) ==');
// an absolute instant whose IST wall-clock reading is exactly h:mi
const istDate = (y, mo, d, h, mi) => new Date(Date.UTC(y, mo - 1, d, h, mi) - 5.5 * 3600 * 1000);
ok('Tue 10:00 IST is open', A.marketOpen(istDate(2026, 7, 28, 10, 0)));
ok('Tue 09:14 IST is closed (pre-open)', !A.marketOpen(istDate(2026, 7, 28, 9, 14)));
ok('Tue 09:15 IST is open (bell)', A.marketOpen(istDate(2026, 7, 28, 9, 15)));
ok('Tue 15:30 IST is open (close bell)', A.marketOpen(istDate(2026, 7, 28, 15, 30)));
ok('Tue 15:31 IST is closed', !A.marketOpen(istDate(2026, 7, 28, 15, 31)));
ok('Saturday is closed', !A.marketOpen(istDate(2026, 8, 1, 11, 0)));
ok('Sunday is closed', !A.marketOpen(istDate(2026, 8, 2, 11, 0)));
ok('istNow returns a date', A.istNow() instanceof Date);

console.log('== live price overlay ==');
const lc = { symbol: 'LIVE1', m: { price: 100, pe: 20 } };
eq('no live tick -> official close', A.shownPrice(lc), 100);
A.LIVE['LIVE1'] = 108;
eq('live tick preferred for display', A.shownPrice(lc), 108);
eq('live change % computed', A.g(lc, 'live_chg'), 8, 1e-9);
eq('official close still intact underneath', lc.m.price, 100);
delete A.LIVE['LIVE1'];
eq('live cleared -> back to close', A.shownPrice(lc), 100);
eq('no live tick -> no live_chg', A.g(lc, 'live_chg'), null);

console.log('== dispute panel ==');
const dc2 = { name: 'D', symbol: 'D', quality: 'exchange', src: { revenue: 'BSE filing' },
  disputed: { revenue: { values: { 'BSE filing': 100, 'Yahoo': 150 }, spread_pct: 33.3 } }, m: {} };
const dh = A.srcHTML(dc2);
ok('provenance still renders alongside a dispute', dh.includes('BSE filing'));
A.setData({ companies: [dc2] });
reset();
A.renderTable();
ok('disputed badge shown in the table', EL.tblwrap.innerHTML.includes('q-disputed'));
A.openDetail(0);
const dd = EL.dp.innerHTML;
ok('dispute panel rendered', dd.includes('Sources disagree'));
ok('dispute shows both source values', dd.includes('BSE filing') && dd.includes('Yahoo'));
ok('dispute shows the spread', dd.includes('33.3'));
ok('dispute explains which one is used', /highest-priority source/.test(dd));
ok('no undefined in dispute panel', !/undefined/.test(dd));
const clean = { name: 'C', symbol: 'C', quality: 'exchange', src: {}, m: {} };
A.setData({ companies: [clean] });
reset(); A.renderTable();
ok('no dispute badge when sources agree', !EL.tblwrap.innerHTML.includes('q-disputed'));

console.log('== provenance panel ==');
const pc = { name: 'P', symbol: 'P', quality: 'exchange',
  src: { revenue: 'BSE filing', pat: 'NSE filing', price: 'Yahoo', issue_price: 'Chittorgarh IPO feed' },
  m: {} };
const ph = A.srcHTML(pc);
ok('provenance lists BSE filing', ph.includes('BSE filing'));
ok('provenance lists Chittorgarh', ph.includes('Chittorgarh'));
ok('provenance labels the field', ph.includes('Revenue') && ph.includes('IPO Issue Price'));
eq('no sources -> no panel', A.srcHTML({ src: {} }), '');
eq('missing src key -> no panel', A.srcHTML({}), '');

console.log('== rendered output ==');
A.setData(sample);
reset();
S.cols = A.PRESETS.all.slice();
A.renderTable();
const t = EL.tblwrap.innerHTML;
ok('table rendered', t.startsWith('<table'), t.slice(0, 40));
ok('no literal undefined in table', !/undefined/.test(t));
ok('no literal NaN in table', !/>NaN</.test(t));
ok('no literal null in table', !/>null</.test(t));
A.M.forEach(m => ok('column header rendered: ' + m.k, t.includes('data-s="' + m.k + '"')));
eq('row count matches data', (t.match(/<tr data-i=/g) || []).length, sample.companies.length);
ok('sticky company column present', t.includes('class="cn"'));
ok('exchange badge rendered', /exb (n|b)/.test(t));
ok('quality badge rendered', /qb q-/.test(t));
ok('listing dates rendered in table', /\d{2} \w{3} \d{4}/.test(t));
ok('shown/total counters set', EL.nShown.textContent === String(sample.companies.length));

A.renderFilters(); A.renderSectors(); A.renderCols(); A.renderScreens(); A.renderQuals();
ok('sector chips rendered', EL.secChips.innerHTML.includes('class="chip'));
ok('quality chips rendered', EL.qChips.innerHTML.includes('data-q="exchange"'));
ok('column picker rendered', EL.colList.innerHTML.includes('type="checkbox"'));
ok('ready-made screens rendered', (EL.pList.innerHTML.match(/data-p=/g) || []).length === A.READY.length);
S.filters = [{ k: 'roce', op: 'bt', v: 10, v2: 30 }];
A.renderFilters();
ok('between filter renders 2 value inputs', EL.fRows.innerHTML.includes('class="fv2"'));
ok('filter metric preselected', EL.fRows.innerHTML.includes('value="roce" selected'));
ok('no undefined in filter row', !/undefined/.test(EL.fRows.innerHTML));
S.filters = [{ k: 'listing_date', op: 'gte', v: '2024-01-01', v2: '' }];
A.renderFilters();
ok('date filter uses a date input', EL.fRows.innerHTML.includes('type="date"'));
ok('date filter uses date operators', EL.fRows.innerHTML.includes('on/after'));

const idxWithListing = sample.companies.findIndex(c => c.listing_date);
A.openDetail(idxWithListing >= 0 ? idxWithListing : 0);
const d = EL.dp.innerHTML;
ok('detail view rendered', d.includes('<h2>'));
const escHtml = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
ok('detail has every metric group', A.GROUPS.every(gr => d.includes('>' + escHtml(gr) + '<')));
ok('group name with & is html-escaped', d.includes('P&amp;L') && !d.includes('<h3>P&L<'));
ok('detail has verify links', d.includes('nseindia.com') && d.includes('screener.in'));
ok('detail shows provenance section', d.includes('Where each number came from'));
ok('detail shows quality label', d.includes('Exchange filing') || d.includes('Third-party') || d.includes('Price only'));
ok('detail shows listing date', d.includes('listed'));
ok('no undefined in detail view', !/undefined/.test(d));

// empty-state path
S.filters = [{ k: 'roce', op: 'gte', v: 99999, v2: '' }];
A.renderTable();
ok('empty state message shown', EL.tblwrap.innerHTML.includes('No company matches'));

console.log('== sample dataset integrity (listing block) ==');
reset();
const withIssue = sample.companies.filter(c => c.m.issue_price != null);
ok('sample has issue prices', withIssue.length > 0);
ok('ret_issue consistent with price/issue', withIssue.every(c => {
  const p = c.m.price, i = c.m.issue_price, r = c.m.ret_issue;
  if (p == null || !i || r == null) return true;
  return Math.abs(((p / i - 1) * 100) - r) < 0.01;
}));
ok('alpha equals stock minus benchmark', sample.companies.every(c => {
  const a = c.m.alpha, s = c.m.ret_listclose, b = c.m.bench_ret;
  if (a == null) return true;
  return Math.abs((s - b) - a) < 0.01;
}));
ok('no CAGR on companies younger than a year', sample.companies.every(c =>
  !(c.m.age_yrs != null && c.m.age_yrs < 1 && c.m.cagr_issue != null)));
ok('every company has a quality tag', sample.companies.every(c => !!c.quality));
ok('every company has a src map', sample.companies.every(c => c.src && Object.keys(c.src).length));

console.log('\n' + (FAIL === 0 ? 'ALL PASS' : 'FAILURES') + ': ' + PASS + ' passed, ' + FAIL + ' failed');
process.exit(FAIL ? 1 : 0);
