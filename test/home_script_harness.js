// Drives Home's own script (home_page.HOME_SCRIPT) in node, on a small
// stand-in for the page's DOM, and prints PASS or FAIL lines.
// run_home_test.py writes the script to a file and runs:
//   node home_script_harness.js <script file>
// No browser and no network: fetch is a stub that records what was sent.
// The DOM stand-in follows test/board_script_harness.js.
'use strict';
const fs = require('fs');
const vm = require('vm');

const SCRIPT = fs.readFileSync(process.argv[2], 'utf8');
let failed = 0;
function say(ok, label) { console.log((ok ? 'PASS  ' : 'FAIL  ') + label); if (!ok) failed++; }

// ---- a small DOM
function kebab(s) { return s.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase()); }
function compound(el, sel) {
  const re = /([a-zA-Z][a-zA-Z0-9-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
  let m, any = false;
  while ((m = re.exec(sel))) {
    any = true;
    if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
    if (m[2] && el.getAttribute('id') !== m[2]) return false;
    if (m[3] && !el.classList.contains(m[3])) return false;
    if (m[4] && (m[5] === undefined ? !el.hasAttribute(m[4]) : el.getAttribute(m[4]) !== m[5])) return false;
  }
  return any;
}
const clicked = [];
class El {
  constructor(tag, attrs, children) {
    this.tagName = tag.toUpperCase();
    this.attrs = {};
    this.children = [];
    this.parentNode = null;
    this.listeners = {};
    this.style = {};
    this.text = '';
    this.tabIndex = 0;
    this.scrollTop = 0;
    const self = this;
    this.classList = {
      list() { return (self.attrs.class || '').split(/\s+/).filter(Boolean); },
      contains(c) { return this.list().includes(c); },
      add(c) { if (!this.contains(c)) self.attrs.class = this.list().concat([c]).join(' '); },
      remove(c) { self.attrs.class = this.list().filter((x) => x !== c).join(' '); },
      toggle(c, on) { if (on === undefined) on = !this.contains(c); if (on) this.add(c); else this.remove(c); return on; },
    };
    this.dataset = new Proxy({}, {
      get: (_t, k) => self.getAttribute('data-' + kebab(String(k))) ?? undefined,
      set: (_t, k, v) => { self.setAttribute('data-' + kebab(String(k)), v); return true; },
    });
    Object.entries(attrs || {}).forEach(([k, v]) => this.setAttribute(k, v));
    (children || []).forEach((c) => this.appendChild(c));
  }
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(v) { if (v) this.attrs.hidden = ''; else delete this.attrs.hidden; }
  get id() { return this.getAttribute('id') || ''; }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = v; }
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(v) { if (v) this.attrs.disabled = ''; else delete this.attrs.disabled; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); }
  removeAttribute(k) { delete this.attrs[k]; }
  detach() { if (this.parentNode) { const p = this.parentNode; p.children = p.children.filter((c) => c !== this); } this.parentNode = null; }
  appendChild(c) { c.detach(); c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c, ref) {
    c.detach();
    const i = ref ? this.children.indexOf(ref) : -1;
    c.parentNode = this;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    return c;
  }
  get nextSibling() { if (!this.parentNode) return null; const s = this.parentNode.children; return s[s.indexOf(this) + 1] || null; }
  set textContent(v) { this.children = []; this.text = String(v); }
  get textContent() { return this.text + this.children.map((c) => c.textContent).join(''); }
  set innerHTML(v) { this.children = []; if (this.onInnerHTML) this.onInnerHTML(v); }
  matches(sel) {
    return sel.split(',').some((one) => {
      const parts = one.trim().split(/\s+/);
      if (!compound(this, parts[parts.length - 1])) return false;
      let el = this.parentNode;
      for (let i = parts.length - 2; i >= 0; i--) {
        while (el && !(el.matches && compound(el, parts[i]))) el = el.parentNode;
        if (!el) return false;
        el = el.parentNode;
      }
      return true;
    });
  }
  closest(sel) { let e = this; while (e && e.matches) { if (e.matches(sel)) return e; e = e.parentNode; } return null; }
  all() { return this.children.flatMap((c) => [c].concat(c.all())); }
  querySelectorAll(sel) { return this.all().filter((e) => e.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  focus() { doc.activeElement = this; }
  click() { clicked.push(this); dispatch(this, 'click'); }
  getBoundingClientRect() { return { top: 100, bottom: 132, left: 0, right: 1200, width: 1200, height: 32 }; }
  get scrollHeight() { return 900; }
  get clientHeight() { return 900; }
  scrollIntoView() {}
}
function h(tag, attrs, ...children) { return new El(tag, attrs, children); }
function dispatch(target, type, init) {
  let stopped = false;
  const ev = Object.assign({ type, target, defaultPrevented: false, button: 0,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation() { stopped = true; } }, init || {});
  for (let e = target; e && !stopped; e = e.parentNode) {
    (e.listeners[type] || []).slice().forEach((fn) => fn(ev));
  }
  return ev;
}

// ---- the page, as home_page draws it, for two new Top prospects
const doc = new El('#document');
const ITEMS = [['role-1', 'role:top:alpha|orrin', 'Head of Software'], ['role-2', 'role:top:beta|kinross', 'Quality Lead']];
function row(id, key, title, current) {
  const attrs = { class: 'row row-3', id: 'row-' + id, href: '/?item=' + id, 'data-item': id, 'data-key': key };
  if (current) attrs['aria-current'] = 'true';
  return h('a', attrs, h('span', { class: 'r1' }, Object.assign(h('span', { class: 'r-title' }), { text: title })));
}
function detail(id, key, title) {
  return h('article', { class: 'ny-detail-item', id: 'd-' + id, 'data-for': id },
    h('header', { class: 'ny-dh' },
      h('a', { class: 'btn btn-primary', href: '/jobs/' + id.split('-')[1], 'data-open': '' }),
      h('button', { class: 'btn btn-secondary', 'data-done': key, 'data-title': title })),
    h('div', { class: 'ny-scroll' }));
}
let main;
function buildMain() {
  main.children = [];
  const list = h('section', { class: 'pane ny-list', id: 'ny-list' },
    h('section', { class: 'ny-group' }, ...ITEMS.map(([id, key, title], n) => row(id, key, title, n === 0))));
  main.appendChild(h('section', { class: 'ny', id: 'ny' },
    h('header', { class: 'ny-head' }, h('h2', { class: 'ny-title', id: 'ny-title' }),
      h('button', { class: 'btn btn-ghost undo-last', id: 'undo-last', disabled: '' })),
    h('div', { class: 'ny-body' }, list,
      h('section', { class: 'ny-detail', id: 'ny-pane' }, detail(...ITEMS[0]))),
    h('div', { class: 'ny-store', id: 'ny-store', hidden: '' }, detail(...ITEMS[1]))));
}
main = h('main', { class: 'content hf', id: 'main' });
main.onInnerHTML = buildMain;
const input = h('input', { type: 'text' });
const keysOn = h('input', { type: 'checkbox', id: 'keys-on' });
const body = h('body', {},
  h('div', { class: 'app', 'data-page': 'home', 'data-state': JSON.stringify({ show: 'todo', item: 'role-1' }) },
    main, h('p', { class: 'toast', id: 'toast' }), h('dialog', { class: 'keys', id: 'keys' }, keysOn), input));
doc.appendChild(body);
doc.body = body;
doc.activeElement = body;
doc.title = 'Home · MedTech Radar';
doc.getElementById = (id) => doc.all().find((e) => e.id === id) || null;
doc.createElement = (tag) => new El(tag);
buildMain();
doc.getElementById('keys').open = false;

const store = (m) => ({ getItem: (k) => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); }, removeItem: (k) => { delete m[k]; } });
const session = {}, local = {};
const calls = [];
const addresses = [];
const timers = [];
const sandbox = {
  document: doc, console, JSON, Promise, Number, String, Array, Object, Math, encodeURIComponent,
  sessionStorage: store(session), localStorage: store(local),
  location: { search: '', href: '', reload() {} },
  history: { replaceState(_s, _t, url) { addresses.push(url); }, pushState() {} },
  setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; }, clearTimeout() {}, setInterval() {},
  innerWidth: 1600, innerHeight: 900,
  matchMedia() { return { matches: false, addEventListener() {} }; },
  addEventListener() {},
  fetch(url, opts) {
    calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
    const out = url.indexOf('part=main') >= 0 ? { html: '', title: 'Home · MedTech Radar', item: 'role-1', count: 2 }
      : { ok: true };
    return Promise.resolve({ ok: true, json: () => Promise.resolve(out) });
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
try {
  vm.runInContext(SCRIPT, sandbox);
} catch (err) {
  say(false, 'the Home script runs on the page (' + err.message + ')');
  process.exit(1);
}
const settle = () => new Promise((r) => setImmediate(r));
async function flush() { for (let i = 0; i < 8; i++) await settle(); }
function key(target, k) { return dispatch(target, 'keydown', { key: k }); }
function posts() { return calls.filter((c) => c.body); }
const $ = (id) => doc.getElementById(id);
const current = () => main.querySelectorAll('.row[aria-current="true"]').map((r) => r.id);

(async () => {
  // J and K move the selection, its address and the title.
  key(body, 'j');
  say(current().join() === 'row-role-2' && addresses[addresses.length - 1] === '/?item=role-2'
      && doc.activeElement === $('row-role-2') && doc.title === 'Quality Lead · Home · MedTech Radar',
      'J selects the next item: its row, its address, the focus and the title');
  say($('d-role-2').parentNode === $('ny-pane') && $('d-role-1').parentNode === $('ny-store'),
      'and its detail takes the pane beside the list');
  key(body, 'k');
  say(current().join() === 'row-role-1' && addresses[addresses.length - 1] === '/?item=role-1',
      'K selects the one before');
  // Enter on the selected row opens it where it lives.
  clicked.length = 0;
  $('row-role-1').focus();
  const enter = key($('row-role-1'), 'Enter');
  say(enter.defaultPrevented && clicked.length === 1 && clicked[0].getAttribute('href') === '/jobs/1',
      'Enter on the selected row opens the item where it lives');
  // E posts Done with the item's key, joins Undo last, and says so with its own Undo.
  calls.length = 0;
  key($('row-role-1'), 'e');
  await flush();
  const done = posts()[0];
  say(done && done.url === '/home/done' && done.body.key === 'role:top:alpha|orrin'
      && done.body.title === 'Head of Software', 'E posts /home/done with the item key');
  const stack = JSON.parse(session['radar-home-undo'] || '[]');
  say(stack.length === 1 && stack[0].kind === 'done' && stack[0].key === 'role:top:alpha|orrin',
      'and the Done joins the Undo last stack');
  say(calls.some((c) => c.url.indexOf('part=main') >= 0), 'and the main element is fetched again');
  const toast = $('toast');
  const undoInToast = toast.querySelector('button');
  say(/Done: .Head of Software. is off Home/.test(toast.textContent) && undoInToast !== null,
      'the toast says it is done, with an Undo of its own');
  // The toast's Undo posts /home/undone.
  calls.length = 0;
  undoInToast.click();
  await flush();
  const undone = posts()[0];
  say(undone && undone.url === '/home/undone' && undone.body.key === 'role:top:alpha|orrin',
      "the toast's Undo posts /home/undone with the same key");
  say(JSON.parse(session['radar-home-undo'] || '[]').length === 0, 'and the entry leaves the stack');
  // Z undoes the last Done.
  calls.length = 0;
  key($('row-role-1'), 'e');
  await flush();
  calls.length = 0;
  key(body, 'z');
  await flush();
  const z = posts()[0];
  say(z && z.url === '/home/undone' && z.body.key === 'role:top:alpha|orrin', 'Z posts /home/undone');
  // The toast clears itself after about eight seconds, or on the next J.
  const later = timers.filter((t) => t.ms === 8000).pop();
  say(later !== undefined && toast.textContent !== '', 'the toast is timed to clear after eight seconds');
  later.fn();
  say(toast.textContent === '', 'and clears when the time is up');
  key($('row-role-1'), 'e');
  await flush();
  say(toast.textContent !== '', 'a new Done says so again');
  key(body, 'j');
  say(toast.textContent === '', 'and the next J clears it');
  // Keys off, and typing in a field, do nothing.
  calls.length = 0;
  local['radar-home-keys'] = 'off';
  const before = current().join();
  key(body, 'j');
  key(body, 'e');
  key(body, 'z');
  await flush();
  say(current().join() === before && posts().length === 0, 'with single-key shortcuts off, J, E and Z do nothing');
  delete local['radar-home-keys'];
  input.focus();
  key(input, 'j');
  key(input, 'e');
  await flush();
  say(current().join() === before && posts().length === 0, 'and while typing in a field they do nothing either');
  process.exit(failed ? 1 : 0);
})();
