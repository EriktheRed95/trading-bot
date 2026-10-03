// Research imports panel: run the shipped script block against a small fake DOM. No browser, no network.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(__dirname + '/trading_ui.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const marker = '// Research imports:';
assert(script.includes(marker), 'research imports block is missing');
const block = script.slice(script.indexOf(marker));

// Hard rules for any code that shows text from links, file names or a model.
assert(!/innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function|srcdoc/.test(block), 'unsafe DOM API in the imports block');
assert(!/GEMINI|API_KEY|apikey/i.test(block.replace(/Google Gemini|with '\+c\.provider/g, '')), 'provider key names must not appear in the page');
assert(html.includes('data-tab="imports"') && html.includes('<section id="imports"'));

function makeNode(tag) {
  const n = {
    tag, children: [], style: {}, className: '', disabled: false, checked: false, value: '', type: '', attrs: {}, _text: '',
    append(...c) { n.children.push(...c); }, replaceChildren(...c) { n.children = c; },
    setAttribute(k, v) { n.attrs[k] = v; }, classList: { contains: () => false },
  };
  Object.defineProperty(n, 'textContent', { get() { return n._text + n.children.map(c => c.textContent).join(''); }, set(v) { n._text = String(v); n.children = []; } });
  Object.defineProperty(n, 'innerHTML', { set() { throw new Error('innerHTML must never be used'); }, get() { throw new Error('innerHTML must never be used'); } });
  return n;
}

function harness({ configured = true, items = [], detail = {} } = {}) {
  const nodes = {}, fetches = [];
  const el = id => (nodes[id] ??= Object.assign(makeNode('#' + id), { files: [] }));
  const caps = { enabled: true, provider: 'Google Gemini', supported: 'links', notice_version: 'v1',
    limits: { video_mb: 80, image_mb: 12, staged_files: 10, staging_hours: 6, upload_types: 'mp4 or png' },
    capabilities: { configured, missing: configured ? [] : ['a Gemini API key in the server environment'] } };
  const routes = {
    '/api/research-import/status': () => caps,
    '/api/research-import/items': () => ({ items }),
  };
  const ctx = {
    console, Promise, Object, Array, String, Number, Date, JSON, URLSearchParams, Error, encodeURIComponent, clearTimeout, setTimeout,
    setInterval() {}, token: 'TOKEN', confirm: () => true, nodes, fetches,
    document: { createElement: makeNode }, el,
    table: (headers, rows) => { const t = makeNode('table'); t._text = headers.join('|') + '\n' + rows.map(r => r.join('|')).join('\n'); return t; },
    fetch: async (url, opts = {}) => {
      fetches.push({ url, opts });
      const path = url.split('?')[0];
      const match = Object.keys(detail).find(k => path === '/api/research-import/items/' + k);
      const body = match ? detail[match] : routes[path] ? routes[path]() : { ok: true, item: items[0] || {}, duplicate: false };
      return { ok: true, json: async () => body };
    },
  };
  vm.createContext(ctx);
  vm.runInContext(block, ctx);
  return { ctx, el, fetches, nodes };
}
const tick = () => new Promise(r => setTimeout(r, 5));
const find = (node, pred, out = []) => { if (pred(node)) out.push(node); node.children.forEach(c => find(c, pred, out)); return out; };
const consentItem = (over = {}) => ({
  id: 'a'.repeat(32), kind: 'link', platform: 'youtube', source_url: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', status: 'pending_consent',
  message: 'Waiting for your approval. Nothing has been sent anywhere.', review_state: 'new', review_note: '', title: null, creator: null, validated: false,
  consent: { required: true, notice: 'Send the public video at https://www.youtube.com/watch?v=dQw4w9WgXcQ to Google Gemini? This approval covers this one item only.',
    fingerprint: 'f'.repeat(64), notice_version: 'v1', provider: 'Google Gemini' }, ...over,
});
const reading = (over = {}) => ({
  title: 'ORB', creator: { name: 'T', handle: 'trader' }, summary: 'Trades the opening range.', strategy: { name: 'ORB', type: 'breakout', description: 'Buy a break.' },
  instruments: ['SPY'], timeframes: ['5 minute'], entry_rules: [{ text: 'Buy above the high', at: '01:10' }], exit_rules: [{ text: 'Sell at close', at: '' }], risk_rules: [],
  stated_numbers: [{ label: 'Window', value: '15', unit: 'min', context: '', at: '' }],
  claimed_returns: [{ claim: 'Made 300%', value: '300%', period: 'last year', basis: 'screenshot', label: 'Source claim, unverified', source_claim: true }],
  evidence_shown: 'screenshot_of_results', missing_details: ['Position size'], commercial_disclosures: ['Sells a course'], validated: false, ...over,
});

(async () => {
  // 1. Hostile text from links, files and models is only ever text.
  const hostile = '<img src=x onerror=alert(1)><script>alert(2)</script>';
  let h = harness({ items: [consentItem({ title: hostile, creator: hostile, message: hostile, source_url: hostile })] });
  await tick();
  const listText = h.el('import-list').textContent;
  assert(listText.includes(hostile), 'hostile text should be shown verbatim, as text');
  assert(find(h.el('import-list'), n => n.tag === 'img' || n.tag === 'script').length === 0);

  // 2. Consent: per item, explicit, never pre-ticked, and the exact server notice is shown.
  const item = consentItem();
  h = harness({ items: [item], detail: { [item.id]: item } });
  await tick();
  await h.ctx.importOpen(item.id);
  const detailText = h.el('import-detail').textContent;
  assert(detailText.includes(item.consent.notice), 'the consent notice must be the server-provided one');
  assert(detailText.includes('Approve this one item'));
  const [box] = find(h.el('import-detail'), n => n.tag === 'input' && n.type === 'checkbox');
  const [go] = find(h.el('import-detail'), n => n.tag === 'button' && n.textContent.startsWith('Approve and read'));
  assert.strictEqual(box.checked, false, 'the approval box must start unticked');
  assert.strictEqual(go.disabled, true, 'approve is disabled until the box is ticked');
  const before = h.fetches.length;
  await go.onclick();
  assert.strictEqual(h.fetches.length, before, 'clicking approve without ticking sends nothing');
  box.checked = true; box.onchange();
  assert.strictEqual(go.disabled, false);
  await go.onclick();
  const approve = h.fetches.filter(f => f.url === '/api/research-import/approve');
  assert.strictEqual(approve.length, 1);
  assert.deepStrictEqual(JSON.parse(approve[0].opts.body), { id: item.id, acknowledged: true, fingerprint: item.consent.fingerprint, notice_version: 'v1' });
  assert.strictEqual(approve[0].opts.headers['X-Paper-Token'], 'TOKEN');
  assert.strictEqual(approve[0].opts.method, 'POST');

  // 3. A reader that is not set up cannot be approved, and says why.
  h = harness({ configured: false, items: [item], detail: { [item.id]: item } });
  await tick();
  await h.ctx.importOpen(item.id);
  const [box2] = find(h.el('import-detail'), n => n.tag === 'input' && n.type === 'checkbox');
  const [go2] = find(h.el('import-detail'), n => n.tag === 'button' && n.textContent.startsWith('Approve and read'));
  box2.checked = true; box2.onchange();
  assert.strictEqual(go2.disabled, true);
  assert(h.el('import-detail').textContent.includes('not set up'));
  assert(h.el('import-setup').textContent.includes('Reader not set up'));

  // 4. A finished item shows claims as source claims, the tier as not validated, and every gap.
  const done = { ...item, status: 'done', consent: undefined, title: 'ORB', message: 'Read complete.', consents: [{ granted_at: '2026-09-30T12:00:00+00:00', provider: 'Google Gemini', outcome: 'done' }],
    result: { reading: reading(), evidence: { tier: 'E2', label: 'Results claimed, nothing reproducible shown', reasons: ['Rules and claimed results are stated.'], dashboard_tier: 'Watchlist material: content-derived, can never be VALIDATED from a video', validated: false },
      source: { platform: 'youtube', url: item.source_url, filename: null, creator: { name: 'T', handle: 'trader' } }, label: 'Imported source material: unverified and not validated.' } };
  h = harness({ items: [done], detail: { [done.id]: done } });
  await tick();
  await h.ctx.importOpen(done.id);
  const text = h.el('import-detail').textContent;
  for (const needle of ['Claimed returns — source claims, unverified', 'Source claim, unverified', 'NOT VALIDATED', 'Evidence tier E2', 'Missing details', 'Position size',
    'Entry rules (as stated)', 'Risk rules (as stated): none stated', 'Sells a course', 'can never be VALIDATED', 'Approval record']) assert(text.includes(needle), 'missing: ' + needle);
  assert(!/\bvalidated\b(?!.*(not|never|NOT|unverified))/i.test(text.replace(/NOT VALIDATED|never be VALIDATED|not validated/g, '')), 'nothing may present the import as validated');
  assert.strictEqual(find(h.el('import-detail'), n => n.tag === 'button' && n.textContent.startsWith('Approve and read')).length, 0, 'a finished item cannot be approved again');

  // 5. Review and delete carry only the item id, state and note.
  const [save] = find(h.el('import-detail'), n => n.tag === 'button' && n.textContent === 'Save review');
  await save.onclick();
  const review = h.fetches.find(f => f.url === '/api/research-import/review');
  assert.deepStrictEqual(Object.keys(JSON.parse(review.opts.body)).sort(), ['id', 'note', 'state']);

  // 6. Adding links and files: size is checked before anything is sent, duplicates are reported.
  h = harness({ items: [item], detail: { [item.id]: item } });
  await tick();
  h.el('import-url').value = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';
  await h.el('import-add-link').onclick();
  const link = h.fetches.find(f => f.url === '/api/research-import/link');
  assert.deepStrictEqual(JSON.parse(link.opts.body), { url: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' });
  assert(h.el('import-message').textContent.includes('Staged'));
  assert(h.el('import-message').textContent.includes('Nothing has been sent'));
  h.el('import-file').files = [{ name: 'huge.mp4', size: 81 * 1048576, type: 'video/mp4' }];
  const uploads = () => h.fetches.filter(f => f.url === '/api/research-import/upload');
  await h.el('import-add-file').onclick();
  assert.strictEqual(uploads().length, 0, 'an over-limit file is refused before it is sent');
  assert(h.el('import-message').textContent.includes('80 MB'));
  h.el('import-file').files = [{ name: 'Weird name #1.png', size: 1000, type: 'image/png' }];
  await h.el('import-add-file').onclick();
  assert.strictEqual(uploads().length, 1);
  assert.strictEqual(uploads()[0].opts.headers['X-Filename'], encodeURIComponent('Weird name #1.png'));
  assert.strictEqual(uploads()[0].opts.headers['X-Paper-Token'], 'TOKEN');
  h.el('import-file').files = [{ name: 'big.png', size: 13 * 1048576, type: 'image/png' }];
  await h.el('import-add-file').onclick();
  assert.strictEqual(uploads().length, 1, 'images have the smaller limit');

  // 7. A disconnected server is reported, and the paper controls elsewhere are not involved.
  h = harness({ items: [] });
  await tick();
  h.ctx.fetch = async () => { throw new Error('offline'); };
  await h.ctx.importRefresh();
  assert(h.el('import-setup').textContent.includes('unavailable'));
  console.log('PASS research imports UI: text-only rendering, per-item consent gating, unconfigured reader, claim labelling, size pre-check, review payloads');
})().catch(e => { console.error(e); process.exit(1); });
