// Drives the applications board's own script (board_page.BOARD_SCRIPT) in node,
// on a small stand-in for the page's DOM, and prints PASS or FAIL lines.
// run_board_test.py writes the script to a file and runs:
//   node board_script_harness.js <script file>
// No browser and no network: fetch is a stub that records what was sent.
'use strict';
const fs = require('fs');
const vm = require('vm');

const SCRIPT = fs.readFileSync(process.argv[2], 'utf8');
let failed = 0;
function say(ok, label) { console.log((ok ? 'PASS  ' : 'FAIL  ') + label); if (!ok) failed++; }

// ---- a small DOM
function kebab(s) { return s.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase()); }
function camel(s) { return s.replace(/-([a-z])/g, (_m, c) => c.toUpperCase()); }
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
class El {
  constructor(tag, attrs, children) {
    this.tagName = tag.toUpperCase();
    this.attrs = {};
    this.children = [];
    this.parentNode = null;
    this.listeners = {};
    this.style = {};
    this.text = '';
    this.value = '';
    this.tabIndex = 0;
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
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  set textContent(v) { this.children = []; this.text = String(v); }
  get textContent() { return this.text + this.children.map((c) => c.textContent).join(''); }
  set innerHTML(_v) { this.children = []; }
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
  getBoundingClientRect() { return { top: 100, bottom: 132, left: 100, right: 200 }; }
  get offsetWidth() { return 240; }
  get offsetHeight() { return 200; }
  get offsetParent() { let e = this; while (e) { if (e.hidden) return null; e = e.parentNode; } return {}; }
  scrollIntoView() {}
  get elements() { const out = {}; this.all().forEach((e) => { if (e.attrs.name) out[e.attrs.name] = e; }); return out; }
}
function h(tag, attrs, ...children) { return new El(tag, attrs, children); }
function dispatch(target, type, init) {
  let stopped = false;
  const ev = Object.assign({ type, target, defaultPrevented: false, button: 0,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation() { stopped = true; } }, init || {});
  for (let e = target; e && !stopped; e = e.parentNode) {
    (e.listeners[type] || []).slice().forEach((fn) => { if (!stopped || true) fn(ev); });
  }
  return ev;
}

// ---- the page, as board_page draws it, for one Applied item
const doc = new El('#document');
let item, title, moveBtn, board, menu, undoBtn, input;
function buildBoard() {
  board.children = [];
  title = h('a', { class: 'item-title link', href: '/jobs/42?show=applied&layout=list' });
  moveBtn = h('button', { class: 'btn btn-secondary item-move', 'aria-haspopup': 'menu', 'aria-expanded': 'false', 'data-move': '7' });
  input = h('input', { type: 'text', name: 'text' });
  item = h('li', { class: 'item', id: 'app-7', 'data-app': '7', 'data-stage': 'applied', 'data-reason': '', 'data-title': 'Software Lead' },
    h('div', { class: 'item-top' }, h('h3', { class: 'item-h' }, title), moveBtn),
    h('form', { class: 'step-form', 'data-step-form': '7' }, input));
  board.appendChild(h('div', { class: 'cols' },
    h('section', { class: 'col', id: 'col-applied', 'data-stage': 'applied' },
      h('h2', { class: 'col-title', id: 'colh-applied' }), h('ul', { class: 'col-items' }, item)),
    h('section', { class: 'col', id: 'col-conversation', 'data-stage': 'conversation' },
      h('h2', { class: 'col-title', id: 'colh-conversation' }), h('ul', { class: 'col-items' }))));
}
board = h('section', { class: 'board', id: 'board' });
menu = h('div', { class: 'move-menu', id: 'move-menu', role: 'menu', hidden: '' });
undoBtn = h('button', { id: 'undo-last', class: 'btn undo-last' });
const body = h('body', {},
  h('div', { class: 'app', 'data-page': 'jobs', 'data-state': JSON.stringify({ view: 'applied', layout: 'board', loc: [] }) },
    h('div', { class: 'show-menu' }, h('button', { id: 'show-btn' }), h('ul', { id: 'show-list', hidden: '' })),
    undoBtn,
    h('main', { id: 'main' }, board),
    menu,
    h('p', { id: 'toast' }),
    h('dialog', { id: 'keys' })));
doc.appendChild(body);
doc.body = body;
doc.activeElement = body;
doc.getElementById = (id) => doc.all().find((e) => e.id === id) || null;
doc.createElement = (tag) => new El(tag);
buildBoard();
doc.getElementById('keys').open = false;

const store = (m) => ({ getItem: (k) => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); }, removeItem: (k) => { delete m[k]; } });
const session = {}, local = {};
const calls = [];
let reply = { ok: true };
const sandbox = {
  document: doc, console, JSON, Promise, Number, String, Array, Object, Math,
  sessionStorage: store(session), localStorage: store(local),
  location: { hash: '', href: '', reload() {} },
  history: { replaceState() {}, pushState() {} },
  setTimeout() {}, clearTimeout() {}, setInterval() {},
  confirm() { return true; },
  innerWidth: 1600, innerHeight: 900,
  matchMedia() { return { matches: false, addEventListener() {} }; },
  addEventListener() {},
  fetch(url, opts) {
    calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
    const out = url.indexOf('part=board') >= 0 ? { html: '', open: 1 } : reply;
    return Promise.resolve({ ok: true, json: () => Promise.resolve(out) });
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
try {
  vm.runInContext(SCRIPT, sandbox);
} catch (err) {
  say(false, 'the board script runs on the page (' + err.message + ')');
  process.exit(1);
}
const settle = () => new Promise((r) => setImmediate(r));
async function flush() { for (let i = 0; i < 6; i++) await settle(); }
function key(target, k, extra) { return dispatch(target, 'keydown', Object.assign({ key: k }, extra || {})); }
function posts() { return calls.filter((c) => c.body); }

function click(target) { return dispatch(target, 'click'); }
function entry(to, reason) {
  return menu.querySelectorAll('[role="menuitem"]').filter((b) => b.dataset.to === to
    && (b.dataset.reason || null) === (reason || null))[0];
}

(async () => {
  // Move to opens on its button, with focus inside the menu.
  click(moveBtn);
  const items = menu.querySelectorAll('[role="menuitem"]');
  say(!menu.hidden && moveBtn.getAttribute('aria-expanded') === 'true'
      && items.length === 6 && doc.activeElement === items[0]
      && items.every((b) => b.dataset.to !== 'applied' || b.dataset.reason),
      'Move to opens from its button, focus on its first entry, every column but its own');
  say(items.every((b) => !b.hasAttribute('aria-keyshortcuts') && !b.querySelector('kbd')),
      'the menu shows no key hints and takes no letter or digit keys');
  // Tab closes it and puts focus back on its button.
  const tab = key(doc.activeElement, 'Tab');
  say(menu.hidden && tab.defaultPrevented && doc.activeElement === moveBtn,
      'Tab closes Move to and puts focus back on its button');
  click(moveBtn);
  key(doc.activeElement, 'Escape');
  say(menu.hidden && doc.activeElement === moveBtn, 'Escape closes Move to and puts focus back');
  // In conversation, chosen in the menu, moves the item, and the move joins Undo last.
  click(moveBtn);
  calls.length = 0;
  click(entry('conversation'));
  await flush();
  const sent = posts()[0];
  say(sent && sent.url === '/application/move' && sent.body.id === 7 && sent.body.to === 'conversation'
      && sent.body.from === 'applied' && sent.body.reason === null && sent.body.undo === false,
      'In conversation in Move to posts the move, from the column the page drew');
  const stack = JSON.parse(session['radar-jobs-undo'] || '[]');
  say(stack.length === 1 && stack[0].kind === 'move' && stack[0].from === 'applied'
      && stack[0].to === 'conversation' && !undoBtn.disabled,
      'the move joins the Undo last stack the List shares');
  say(/Moved .Software Lead. to In conversation\./.test(doc.getElementById('toast').textContent)
      && doc.getElementById('toast').querySelector('button') !== null,
      'and shows an Undo notice with its own Undo button');
  // Undo last undoes it: the move back, marked as an undo.
  buildBoard();
  calls.length = 0;
  click(undoBtn);
  await flush();
  const back = posts()[0];
  say(back && back.body.to === 'applied' && back.body.from === 'conversation' && back.body.undo === true,
      'Undo last undoes the last move: back to Applied, sent as an undo');
  say(JSON.parse(session['radar-jobs-undo'] || '[]').length === 0, 'and the entry leaves the stack');
  // Closed, no reply closes with its reason.
  buildBoard();
  calls.length = 0;
  click(moveBtn);
  click(entry('closed', 'no-reply'));
  await flush();
  const close = posts()[0];
  say(close && close.body.to === 'closed' && close.body.reason === 'no-reply',
      'Closed, no reply in Move to closes it as no reply');
  // The single-key shortcuts are gone: M, Z and ? do nothing, anywhere.
  buildBoard();
  session['radar-jobs-undo'] = JSON.stringify([{ kind: 'move', id: 7, title: 'x', from: 'applied', to: 'conversation' }]);
  calls.length = 0;
  title.focus();
  key(title, 'm');
  key(body, 'z');
  key(body, '?');
  await flush();
  say(menu.hidden && posts().length === 0, 'M, Z and ? do nothing: the board has no single-key shortcuts');
  session['radar-jobs-undo'] = '[]';
  // A refused move says why and is not stacked.
  buildBoard();
  reply = { ok: false, note: 'That application has moved to Offer since the page was drawn.' };
  click(moveBtn);
  click(entry('interviewing'));
  await flush();
  say(/moved to Offer/.test(doc.getElementById('toast').textContent)
      && JSON.parse(session['radar-jobs-undo']).length === 0,
      'a refused move shows the reason and joins no Undo');
  process.exit(failed ? 1 : 0);
})();
