// Drives the CV page's scripts (cv_page's editor and cv_screen.CV_SCREEN_JS)
// in node, on a small stand-in for the page's DOM, and prints PASS or FAIL.
// test/run_cv_final_test.py renders the pages and runs:
//   node cv_screen_harness.js <cases.json>
// cases.json names the rendered pages and what the server expects of them.
// No browser and no network: fetch is a stub that records what was sent.
// The DOM stand-in follows the one in test/run_cv_test.py's editor checks.
'use strict';
const fs = require('fs');
const vm = require('vm');

const CASES = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
let failed = 0;
function say(ok, label) { console.log((ok ? 'PASS  ' : 'FAIL  ') + label); if (!ok) failed++; }

const ENT = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', rsquo: '’',
              ldquo: '“', rdquo: '”', bull: '•', mdash: '—', middot: '·' };
function decode(s) {
  return String(s).replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
    if (e[0] === '#') return String.fromCodePoint(e[1] === 'x' || e[1] === 'X'
      ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10));
    return ENT[e.toLowerCase()] !== undefined ? ENT[e.toLowerCase()] : m;
  });
}
function compound(s) {
  const out = { tag: null, id: null, classes: [], attrs: [] };
  const re = /([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
  let m;
  while ((m = re.exec(s))) {
    if (m[1]) out.tag = m[1].toLowerCase();
    else if (m[2]) out.id = m[2];
    else if (m[3]) out.classes.push(m[3]);
    else out.attrs.push([m[4], m[5] === undefined ? null : m[5]]);
  }
  return out;
}
function matchOne(el, c) {
  if (!el || !el.tagName) return false;
  if (c.tag && el.tagName.toLowerCase() !== c.tag) return false;
  if (c.id && el.getAttribute('id') !== c.id) return false;
  for (const k of c.classes) if (!el.classList.contains(k)) return false;
  for (const [n, v] of c.attrs) {
    const a = el.getAttribute(n);
    if (a === null || (v !== null && a !== v)) return false;
  }
  return true;
}
function matchChain(el, sel) {
  const parts = sel.trim().split(/\s+/).map(compound);
  if (!matchOne(el, parts[parts.length - 1])) return false;
  let i = parts.length - 2;
  let cur = el.parentNode;
  while (i >= 0 && cur) { if (matchOne(cur, parts[i])) i -= 1; cur = cur.parentNode; }
  return i < 0;
}
const matches = (el, sel) => sel.split(',').some((s) => matchChain(el, s));

function world(html, opts) {
  opts = opts || {};
  const calls = [];
  const pending = [];
  const timers = [];
  const confirms = [];
  const store = {};
  const events = [];
  let document;
  class Text { constructor(t) { this.nodeType = 3; this._t = t; this.parentNode = null; }
    get textContent() { return this._t; } set textContent(v) { this._t = String(v); } }
  class El {
    constructor(tag) {
      this.tagName = tag.toUpperCase(); this.nodeType = 1; this.attrs = {}; this.children = [];
      this.parentNode = null; this.listeners = {}; this._text = ''; this.value = '';
      this.disabled = false; this.rows = 2; this.checked = false;
      const me = this;
      this.style = { setProperty(k, v) { this[k] = String(v); } };
      this.classList = {
        contains: (k) => (me.attrs['class'] || '').split(/\s+/).includes(k),
        add: (k) => { if (!me.classList.contains(k)) me.attrs['class'] = ((me.attrs['class'] || '') + ' ' + k).trim(); },
        remove: (k) => { me.attrs['class'] = (me.attrs['class'] || '').split(/\s+/).filter((x) => x && x !== k).join(' '); },
        toggle: (k, on) => { const want = on === undefined ? !me.classList.contains(k) : !!on;
          if (want) me.classList.add(k); else me.classList.remove(k); return want; },
      };
      if (this.tagName === 'DIALOG') {
        this.open = false;
        this.showModal = () => { this.open = true; this.attrs.open = ''; };
        this.close = () => { this.open = false; delete this.attrs.open; fire(this, 'close'); };
      }
    }
    get hidden() { return Object.prototype.hasOwnProperty.call(this.attrs, 'hidden'); }
    set hidden(v) { if (v) this.attrs.hidden = ''; else delete this.attrs.hidden; }
    get id() { return this.getAttribute('id') || ''; }
    set id(v) { this.attrs.id = String(v); }
    setAttribute(n, v) { this.attrs[n] = String(v); if (n === 'disabled') this.disabled = true;
      if (n === 'checked') this.checked = true; }
    getAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null; }
    hasAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n); }
    removeAttribute(n) { delete this.attrs[n]; }
    get className() { return this.attrs['class'] || ''; }
    set className(v) { this.attrs['class'] = String(v); }
    get title() { return this.attrs.title || ''; } set title(v) { this.attrs.title = String(v); }
    get type() { return this.attrs.type || ''; } set type(v) { this.attrs.type = String(v); }
    appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
    removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; return c; }
    insertBefore(c, ref) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this;
      const i = this.children.indexOf(ref);
      if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; }
    get firstChild() { return this.children[0] || null; }
    get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join('') : this._text; }
    set textContent(v) {
      const active = document.activeElement;
      if (active && active !== this && this.contains(active)) {
        document.activeElement = null;
        fire(active, 'focusout');
      }
      this.children.forEach((c) => { c.parentNode = null; }); this.children = []; this._text = String(v);
    }
    set innerHTML(h) { this.textContent = ''; parse(String(h), this); }
    walk(fn) { for (const c of this.children) { if (c.nodeType === 1) { fn(c); c.walk(fn); } } }
    querySelectorAll(sel) { const out = []; this.walk((el) => { if (matches(el, sel)) out.push(el); }); return out; }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    closest(sel) { let cur = this; while (cur && cur.nodeType === 1) { if (matches(cur, sel)) return cur; cur = cur.parentNode; } return null; }
    contains(node) { let cur = node; while (cur) { if (cur === this) return true; cur = cur.parentNode; } return false; }
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
    focus() { const was = document.activeElement; if (was === this) return;
      document.activeElement = this; if (was) fire(was, 'focusout'); fire(this, 'focusin'); }
    blur() { if (document.activeElement === this) document.activeElement = null; }
    click() { return fire(this, 'click'); }
    setSelectionRange(a) { this.selectionStart = this.selectionEnd = a; }
    scrollIntoView() { events.push('scroll:' + this.id); }
    getBoundingClientRect() { return { left: 10, right: 100, top: 10, bottom: 40 }; }
    get scrollHeight() { return 20; }
    get clientWidth() { return 600; }
    get children_() { return this.children; }
  }
  document = new El('#document');
  document.documentElement = new El('html');
  document.createElement = (t) => new El(t);
  document.createTextNode = (t) => new Text(t);
  document.getElementById = (id) => document.querySelector('#' + id);
  const VOID = new Set(['br', 'img', 'input', 'meta', 'link', 'hr', 'col', 'source', 'wbr', 'path', 'circle']);
  const RAW = new Set(['script', 'style', 'textarea', 'noscript', 'title']);
  function parse(h, root) {
    const stack = [root];
    const re = /<!--[\s\S]*?-->|<![^>]*>|<\/([a-zA-Z0-9]+)\s*>|<([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^\s=>\/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>/g;
    let m;
    let last = 0;
    const text = (t) => { if (t) stack[stack.length - 1].appendChild(new Text(decode(t))); };
    while ((m = re.exec(h))) {
      text(h.slice(last, m.index));
      last = re.lastIndex;
      if (m[0][1] === '!') continue;
      if (m[1]) {
        const tag = m[1].toLowerCase();
        for (let k = stack.length - 1; k > 0; k -= 1) {
          if (stack[k].tagName.toLowerCase() === tag) { stack.length = k; break; }
        }
        continue;
      }
      const tag = m[2].toLowerCase();
      const el = new El(tag);
      const are = /([^\s=>\/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let a;
      while ((a = are.exec(m[3]))) {
        const v = a[2] !== undefined ? a[2] : a[3] !== undefined ? a[3] : a[4] !== undefined ? a[4] : '';
        el.setAttribute(a[1], decode(v));
      }
      if (tag === 'input' && el.hasAttribute('value')) el.value = el.getAttribute('value');
      stack[stack.length - 1].appendChild(el);
      if (RAW.has(tag)) {
        const end = h.toLowerCase().indexOf('</' + tag, last);
        const inner = h.slice(last, end < 0 ? h.length : end);
        if (tag === 'textarea') el.value = decode(inner);
        if (tag === 'script') el._text = inner;
        re.lastIndex = end < 0 ? h.length : h.indexOf('>', end) + 1;
        last = re.lastIndex;
        continue;
      }
      if (!VOID.has(tag) && !m[4]) stack.push(el);
    }
    text(h.slice(last));
  }
  const body = new El('body');
  document.appendChild(body);
  const bodyHtml = html.split(/<body[^>]*>/)[1].split('</body>')[0];
  const scripts = [];
  parse(bodyHtml.replace(/<script>([\s\S]*?)<\/script>/g, (_m, s) => { scripts.push(s); return ''; }), body);
  const windowListeners = {};
  const location = { pathname: '/cv', search: '', hash: opts.hash || '', href: '/cv',
                     reload() { events.push('reload'); } };
  const sandbox = {
    Headers, JSON, Array, Object, String, Number, Promise, Math, Error, Date, RegExp,
    document, location,
    localStorage: { getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
                    setItem(k, v) { store[k] = String(v); } },
    history: { replaceState(_s, _t, url) { const i = url.indexOf('#'); location.hash = i >= 0 ? url.slice(i) : ''; } },
    setTimeout(fn) { timers.push(fn); return timers.length; }, clearTimeout() {}, setInterval() {},
    confirm(q) { confirms.push(q); return opts.confirm !== false; },
    MutationObserver: class { observe() {} },
    ResizeObserver: class { observe() {} },
    addEventListener(t, fn) { (windowListeners[t] = windowListeners[t] || []).push(fn); },
    scrollX: 0, scrollY: 0,
  };
  sandbox.window = sandbox;
  sandbox.fetch = (url, o) => {
    calls.push({ url, opts: o || {} });
    return new Promise((resolve, reject) => { pending.push({ resolve, reject }); });
  };
  function fire(target, type, extra) {
    const ev = Object.assign({ type, target, defaultPrevented: false, key: undefined,
      preventDefault() { this.defaultPrevented = true; } }, extra || {});
    let cur = target;
    while (cur) { (cur.listeners && cur.listeners[type] || []).slice().forEach((fn) => fn(ev)); cur = cur.parentNode; }
    (windowListeners[type] || []).forEach((fn) => fn(ev));
    return ev;
  }
  vm.createContext(sandbox);
  scripts.forEach((s) => vm.runInContext(s, sandbox));
  const q = (sel) => document.querySelector(sel);
  const qa = (sel) => document.querySelectorAll(sel);
  return {
    document, sandbox, calls, pending, timers, confirms, store, events, q, qa, fire, location,
    key(target, key, mods) { return fire(target || document.activeElement || body, 'keydown', Object.assign({ key }, mods || {})); },
    input(el, value) { el.value = value; return fire(el, 'input'); },
    click(el) { return fire(el, 'click'); },
    answer(value, status) { pending.shift().resolve({ status: status || 200, json: () => Promise.resolve(value) }); },
    flush() { const t = timers.splice(0); t.forEach((fn) => fn()); },
    body: (i) => JSON.parse(calls[i].opts.body),
    api: sandbox.RadarCvEditor,
  };
}
const settle = () => new Promise((r) => setImmediate(r));
const text = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
function numbers(w) {
  return w.qa('#cv-editor .cvl[data-n]').map((r) => Number(r.getAttribute('data-n')));
}
function words(w) {
  return w.qa('#cv-editor .cvl[data-n]').map((r) => {
    const t = r.querySelector('.cvl-t');
    return t.tagName === 'TEXTAREA' ? t.value : t.textContent;
  });
}

(async () => {
  if (typeof Headers === 'undefined') { console.log('NOTE  this node has no Headers, so the page script was not run'); process.exit(0); }
  const C = CASES;
  const page = fs.readFileSync(C.pages.master, 'utf8');
  let w = world(page);
  await settle();
  const n = C.expect.lines.length;
  const every = Array.from({ length: n }, (_x, i) => i + 1);
  say(w.calls.length === 0, 'the CV page sends nothing when it opens');
  const liveRun = w.q('#rescore-live');
  say(liveRun && liveRun.textContent === '' && !liveRun.hidden && !liveRun.closest('[hidden]')
      && liveRun.getAttribute('role') === 'status' && w.q('#rescore-run').getAttribute('aria-live') === null,
      'Rescore\'s status region is on the page from the start, empty and not hidden, and the box shown in one step has none of its own');
  say(JSON.stringify(numbers(w)) === JSON.stringify(every)
      && JSON.stringify(words(w)) === JSON.stringify(C.expect.words),
      `read, every line of the master is one .cvl[data-n] element, numbered 1 to ${n} as cv_store.cv_lines numbers the text, inside the part wrappers`);
  const parts = w.qa('#cv-editor .cv-part');
  say(parts.length === C.expect.parts.length
      && parts.every((p, i) => p.getAttribute('aria-label') === C.expect.parts[i] && p.getAttribute('tabindex') === '0'),
      'each part, the head and every section, is a Tab stop named with its lines and what Enter does: ' + C.expect.parts[1]);
  say(w.q('#cv-editor .cvl[data-n="' + C.expect.sectionLine + '"]').getAttribute('role') === 'heading'
      && w.q('#cv-editor .cvl[data-n="' + C.expect.sectionLine + '"]').getAttribute('aria-level') === '3',
      'a section heading on the sheet is a level 3 heading, under the page\'s h1 and the panels\' h2');
  const pagesSeen = w.api.state().pages;
  say(JSON.stringify(pagesSeen) === JSON.stringify(C.expect.pages),
      `the page script breaks the sheet into A4 pages where the server does (${JSON.stringify(pagesSeen)})`);
  say(w.qa('#cv-editor .cv-break').length === C.expect.pages.length - 1,
      'and draws one break before each page after the first');

  // ---- Enter opens a part, Escape leaves the line and then the part
  const s0 = w.q('.cv-part[data-part="s0"]');
  s0.focus();
  w.key(s0, 'Enter');
  let field = w.q('.cv-part[data-part="s0"] textarea');
  say(field && w.sandbox.document.activeElement === field && !w.q('.cv-part[data-part="s1"] textarea'),
      'Enter on a section opens only that section\'s lines as fields, with focus on its first line');
  say(JSON.stringify(numbers(w)) === JSON.stringify(every)
      && JSON.stringify(words(w)) === JSON.stringify(C.expect.words),
      'and while it is open the lines keep the same elements and numbers, fields included');
  w.key(field, 'Escape');
  say(w.sandbox.document.activeElement === w.q('.cv-part[data-part="s0"]'),
      'Escape in a line goes back to its section');
  w.key(w.q('.cv-part[data-part="s0"]'), 'Escape');
  say(!w.q('#cv-editor textarea') && w.sandbox.document.activeElement === w.q('.cv-part[data-part="s0"]'),
      'and Escape again closes it, keeping focus on the section');

  // ---- typing, the save state, Ctrl S, Undo
  w.click(w.q('.cv-part[data-part="s0"]'));
  field = w.q('.cv-part[data-part="s0"] textarea');
  w.input(field, field.value + ' More.');
  say(/1 unsaved line/.test(text(w.q('#save-state'))) && !w.q('#tb-save').hidden && !w.q('#tb-discard').hidden,
      'an edit shows in words in the toolbar, "1 unsaved line", with Discard and Save version beside it');
  say(w.q('#tb-undo').getAttribute('aria-disabled') === 'false', 'and Undo comes on');
  w.key(field, 's', { ctrlKey: true });
  await settle();
  say(w.calls.length === 1 && w.calls[0].url === '/cv/master/save'
      && w.body(0).base === C.expect.version && w.body(0).text.includes(' More.'),
      'Ctrl S saves a version from anywhere, the edit in it, with the version the page was opened on');
  w.answer({ ok: false, note: 'The master CV has changed since this page was opened, it is now cv-x.md.' }, 409);
  await settle(); await settle();
  say(/Not saved/.test(text(w.q('#save-state'))) && !w.q('#tb-reload').hidden && w.q('#tb-save').hidden,
      'a save refused because another tab saved first says Not saved, with a cross and Reload');
  w = world(page);
  await settle();
  w.click(w.q('.cv-part[data-part="s0"]'));
  field = w.q('.cv-part[data-part="s0"] textarea');
  const was = field.value;
  w.input(field, was + ' More.');
  w.sandbox.document.activeElement = w.q('#main');
  w.key(w.q('#main'), 'z', { ctrlKey: true });
  say(w.api.state().edited === false && w.q('#tb-save').hidden && /Saved/.test(text(w.q('#save-state'))),
      'Ctrl Z outside a line undoes the unsaved edit, and the save state is Saved again');
  w.key(w.q('#main'), 'z', { ctrlKey: true, shiftKey: true });
  say(w.api.state().edited === true, 'and Ctrl Shift Z redoes it');
  w.key(w.q('#main'), 'z', { ctrlKey: true });
  const before = w.calls.length;
  w.key(w.q('#main'), 's', { ctrlKey: true });
  say(w.calls.length === before, 'Ctrl S with nothing to save sends nothing');

  // ---- Alt Shift arrows move a section
  const titles = () => w.api.sections().map((s) => s.title).join('|');
  const was0 = titles();
  w.q('.cv-part[data-part="s0"]').focus();
  w.key(w.q('.cv-part[data-part="s0"]'), 'ArrowDown', { altKey: true, shiftKey: true });
  say(titles() !== was0 && w.api.sections()[1].title === C.expect.sections[0]
      && w.sandbox.document.activeElement === w.q('.cv-part[data-part="s1"]')
      && /moved to position 2/.test(text(w.q('#cv-live'))),
      'Alt Shift Down moves the section in focus, keeps focus on it and says where it went');
  say(JSON.stringify(numbers(w)) === JSON.stringify(Array.from({ length: w.api.state().lines.length }, (_x, i) => i + 1)),
      'and the lines are numbered again from 1 in their new order');
  w.api.undo();
  say(titles() === was0, 'Undo puts it back');
  w.q('.cv-part[data-part="s1"]').focus();
  w.key(w.q('.cv-part[data-part="s1"]'), 'ArrowUp', { altKey: true, shiftKey: true });
  say(w.api.sections()[0].title === C.expect.sections[1]
      && w.sandbox.document.activeElement === w.q('.cv-part[data-part="s0"]'),
      'Alt Shift Up moves it the other way');
  w.api.undo();

  // ---- no single-key shortcuts, only the combinations the frames draw
  const snapshot = () => JSON.stringify([w.qa('[role="tab"]').map((t) => t.getAttribute('aria-selected')),
    w.q('#add-menu').hidden, w.q('#rearrange-dlg').open, w.q('#design-dlg').open, titles(),
    w.api.state().edited, w.calls.length]);
  const quiet = snapshot();
  const places = [['#main', 'the page'], ['.cv-part[data-part="s0"]', 'a section of the sheet'],
                  ['#tab-history', 'a tab'], ['#tb-add', 'the toolbar']];
  let still = 0;
  let unclaimed = 0;
  places.forEach(([sel, _where]) => {
    const el = w.q(sel);
    w.sandbox.document.activeElement = el;
    ['h', 'p', 'w', 'a', 'r', 'd', 'e', 'j', 'k', 'm', '?', '1'].forEach((k) => {
      const ev = w.key(el, k, k === '?' ? { shiftKey: true } : {});
      if (snapshot() === quiet && w.sandbox.document.activeElement === el) still += 1;
      if (!ev.defaultPrevented) unclaimed += 1;
    });
  });
  say(still === 48 && unclaimed === 48,
      'no single key does anything anywhere on the page: H, P, W, A, R, D, ? and the rest change no tab, open no menu or dialog, move no focus and are not claimed');
  say(!w.q('#keys-on') && !/Single/.test(text(w.q('.a-keys')))
      && w.qa('[aria-keyshortcuts]').every((b) => /^(Control|Alt)\+/.test(b.getAttribute('aria-keyshortcuts'))),
      'there is no single-key switch, the keys panel lists none, and every aria-keyshortcuts names Ctrl or Alt');
  say(JSON.stringify(w.qa('.a-keys dt').map((d) => text(d))) === JSON.stringify(C.expect.keys),
      'the keys panel lists exactly the keys the frames draw: ' + w.qa('.a-keys dt').map((d) => text(d)).join(', '));
  say(w.q('#tb-save').getAttribute('aria-keyshortcuts') === 'Control+S'
      && w.q('#tb-undo').getAttribute('aria-keyshortcuts') === 'Control+Z'
      && w.q('#tb-redo').getAttribute('aria-keyshortcuts') === 'Control+Shift+Z',
      'Save version, Undo and Redo carry their combinations in aria-keyshortcuts');
  // Ctrl Y, Cmd Z and Alt Z are not the frames' and do nothing.
  w.click(w.q('.cv-part[data-part="s0"]'));
  const fy = w.q('.cv-part[data-part="s0"] textarea');
  w.input(fy, fy.value + ' Y.');
  w.sandbox.document.activeElement = w.q('#main');
  w.key(w.q('#main'), 'y', { ctrlKey: true });
  w.key(w.q('#main'), 'z', { metaKey: true });
  w.key(w.q('#main'), 'z', { altKey: true });
  say(w.api.state().edited === true, 'Ctrl Y, Cmd Z and Alt Z undo nothing');
  // While typing: Ctrl Z is the field's own undo, never the page's.
  const fz = w.q('.cv-part[data-part="s0"] textarea');
  w.sandbox.document.activeElement = fz;
  const evz = w.key(fz, 'z', { ctrlKey: true });
  say(!evz.defaultPrevented && w.api.state().edited === true,
      'Ctrl Z in a line is left to the line itself, so the page undoes nothing while you type');
  w.key(fz, 'ArrowDown', { altKey: true, shiftKey: true });
  say(titles() === was0, 'Alt Shift Down in a line being edited moves nothing');
  w.sandbox.document.activeElement = w.q('#main');
  w.key(w.q('#main'), 'z', { ctrlKey: true });
  say(w.api.state().edited === false, 'and Ctrl Z outside the line undoes it');
  w.key(w.q('.cv-part[data-part="s0"]'), 'Escape');

  // ---- Ctrl S does nothing while Rearrange or Design is open
  w.click(w.q('.cv-part[data-part="s0"]'));
  const fs2 = w.q('.cv-part[data-part="s0"] textarea');
  w.input(fs2, fs2.value + ' S.');
  w.key(fs2, 'Escape');
  w.key(w.q('.cv-part[data-part="s0"]'), 'Escape');
  let quietCalls = w.calls.length;
  [['#tb-rearrange', '#rearrange-dlg', '#rearrange-cancel'], ['#tb-design', '#design-dlg', '#design-cancel']]
    .forEach(([btn, dlgSel, cancel]) => {
      w.click(w.q(btn));
      const evs = w.key(w.sandbox.document.activeElement || w.q(dlgSel), 's', { ctrlKey: true });
      say(w.q(dlgSel).open && w.calls.length === quietCalls && evs.defaultPrevented,
          'Ctrl S with ' + dlgSel.slice(1, -4) + ' open saves nothing, and is still kept from the browser');
      w.click(w.q(cancel));
    });
  w.sandbox.document.activeElement = w.q('#main');
  w.key(w.q('#main'), 's', { ctrlKey: true });
  await settle();
  say(w.calls.length === quietCalls + 1 && w.calls[w.calls.length - 1].url === '/cv/master/save',
      'the control: with the dialog closed, the same Ctrl S saves');
  w.answer({ ok: false, note: 'Not now.' }, 409);
  await settle(); await settle();
  w = world(page);
  await settle();

  // ---- the toolbar and the tabs are one Tab stop each
  const tb = w.qa('#cv-toolbar button').filter((b) => !b.hidden && !b.closest('[hidden]'));
  say(tb.filter((b) => b.getAttribute('tabindex') === '0').length === 1, 'the toolbar is one Tab stop');
  tb[0].focus();
  w.key(tb[0], 'ArrowRight');
  say(w.sandbox.document.activeElement === tb[1] && tb[1].getAttribute('tabindex') === '0',
      'and Right moves along it');
  w.key(tb[1], 'End');
  say(w.sandbox.document.activeElement === tb[tb.length - 1], 'End goes to its last button');
  w.q('#tab-history').focus();
  w.key(w.q('#tab-history'), 'ArrowRight');
  say(w.sandbox.document.activeElement === w.q('#tab-settings') && !w.q('#settings').hidden
      && w.q('#tab-settings').getAttribute('tabindex') === '0' && w.q('#tab-history').getAttribute('tabindex') === '-1',
      'the tabs follow the ARIA pattern: Right moves to and opens the next tab, one Tab stop');
  say(w.location.hash === '#settings', 'and the address names the open tab, so a reload keeps it');

  // ---- Add section, a menu
  w.q('.cv-part[data-part="s1"]').focus();
  w.click(w.q('#tb-add'));
  const items = w.qa('#add-menu [role="menuitem"]');
  say(!w.q('#add-menu').hidden && w.q('#tb-add').getAttribute('aria-expanded') === 'true'
      && w.sandbox.document.activeElement === items[0]
      && /Add after/.test(text(w.q('#add-after'))),
      'Add section opens a menu after the section in focus, focus on its first item');
  w.key(items[0], 'ArrowDown');
  say(w.sandbox.document.activeElement === items[1], 'arrows move through it');
  w.key(items[1], 'Escape');
  say(w.q('#add-menu').hidden && w.sandbox.document.activeElement === w.q('#tb-add'), 'Escape closes it, back on its button');
  w.click(w.q('#tb-add'));
  const count = w.api.sections().length;
  w.click(w.qa('#add-menu [role="menuitem"]')[2]);
  say(w.api.sections().length === count + 1 && w.api.sections()[2].title === 'Languages'
      && w.sandbox.document.activeElement && w.sandbox.document.activeElement.tagName === 'TEXTAREA',
      'choosing Languages adds it after the section in focus and opens it with focus on its first line');
  w.api.undo();

  // ---- Rearrange, a keyboard dialog
  w.click(w.q('#tb-rearrange'));
  const dlg = w.q('#rearrange-dlg');
  let row = w.sandbox.document.activeElement;
  say(dlg.open && row && row.classList.contains('rrow') && /position 1 of/.test(row.getAttribute('aria-label')),
      'Rearrange opens a dialog with focus on the first section');
  w.key(row, ' ');
  row = w.sandbox.document.activeElement;
  say(/picked up/.test(text(w.q('#rearrange-live'))) && row.classList.contains('is-picked'),
      'Space picks a section up and says so');
  w.key(row, 'ArrowDown');
  row = w.sandbox.document.activeElement;
  w.key(row, 'Escape');
  say(dlg.open && /put back at position 1/.test(text(w.q('#rearrange-live'))),
      'Escape while one is picked up puts it back, and leaves the dialog open');
  row = w.sandbox.document.activeElement;
  w.key(row, ' ');
  w.key(w.sandbox.document.activeElement, 'ArrowDown');
  w.key(w.sandbox.document.activeElement, ' ');
  say(/put down at position 2/.test(text(w.q('#rearrange-live'))), 'Up and Down move it, Space puts it down');
  w.click(w.q('#rearrange-done'));
  say(!dlg.open && w.api.sections()[1].title === C.expect.sections[0]
      && w.sandbox.document.activeElement === w.q('#tb-rearrange'),
      'Done applies the order to the CV and returns focus to Rearrange');
  say(w.api.state().edited === true, 'and the new order is an unsaved change');
  w.api.undo();
  w.click(w.q('#tb-rearrange'));
  w.click(w.qa('#rearrange-list [data-move="down"]')[0]);
  w.click(w.q('#rearrange-cancel'));
  say(titles() === was0, 'Cancel leaves the order as it was, after Move down');

  // ---- Design
  w.click(w.q('#tb-design'));
  const dd = w.q('#design-dlg');
  const size = () => w.q('#design-opts [data-group="size"] [aria-checked="true"]');
  say(dd.open && text(size()) === '10.5 pt' && /2 pages|1 page|pages/.test(text(w.q('#design-pages'))),
      'Design opens on the design in use, ticked, and says how many pages it gives');
  w.key(size(), 'ArrowRight');
  say(text(size()) === '11 pt' && w.sandbox.document.activeElement === size(),
      'arrows choose within a group, the choice ticked as well as tinted');
  w.click(w.q('#design-apply'));
  await settle();
  const dcall = w.calls[w.calls.length - 1];
  say(dcall.url === '/cv/design' && JSON.parse(dcall.opts.body).size === 11, 'Apply saves the choice');
  w.answer({ ok: true, design: { font: 'Arial', size: 11, spacing: 'standard', margins: 20 } });
  await settle(); await settle();
  say(!dd.open && w.q('#cv-sheet').style['--cv-scale'] === String(11 / 10.5),
      'and the sheet takes it at once');
  w.click(w.q('#tb-design'));
  w.click(w.q('#design-numbers'));
  say(w.q('#cv-sheet').classList.contains('nums-off') && w.store['radar-cv-numbers'] === 'off',
      'the line numbers switch hides the numbers on the sheet and is remembered');
  w.click(w.q('#design-cancel'));

  // ---- the earlier-CV notice and Rescore affected
  const sum = C.expect.stale;
  const rb = w.q('[data-rescore="stale"]');
  say(rb && text(rb) === `Rescore ${sum.count} roles, ${sum.cost}` && rb.getAttribute('aria-expanded') === 'false'
      && rb.getAttribute('aria-controls') === 'rescore-confirm',
      `the earlier-CV notice's button carries the count and the cost: "${text(rb)}"`);
  const sent = w.calls.length;
  w.click(rb);
  const conf = w.q('#rescore-confirm');
  say(!conf.hidden && w.sandbox.document.activeElement === conf && rb.getAttribute('aria-expanded') === 'true'
      && text(w.q('#rescore-confirm-text')).includes(`That is ${sum.count} paid calls, ${sum.cost}, in one run.`)
      && /keep their scores and are not counted/.test(text(w.q('#rescore-confirm-text')))
      && w.calls.length === sent,
      'pressing it opens a confirm under the notice, with focus, that says the count and the cost again and what is left out, and sends nothing');
  w.click(w.q('#rescore-cancel'));
  say(conf.hidden && w.sandbox.document.activeElement === rb && w.calls.length === sent,
      'Cancel closes it, back on the button, and nothing was sent');
  w.click(rb);
  w.click(w.q('#rescore-go'));
  await settle();
  const rc = w.calls[w.calls.length - 1];
  say(w.calls.length === sent + 1 && rc.url === '/cv/rescore'
      && JSON.stringify(JSON.parse(rc.opts.body)) === JSON.stringify({ confirm: true, scope: 'stale', expect: sum.count, ids_hash: sum.ids_hash }),
      'only the confirm sends the run, with the count it stated and the hash of the roles it named');
  w.answer({ ok: true, outcome: 'started', state: 'running', count: sum.count, done: 0, failed: 0, left: 0,
             unstored: 0, cost_cents: 0, mode: 'live' });
  await settle(); await settle();
  say(!w.q('#rescore-run').hidden && new RegExp(`Rescoring 1 of ${sum.count}. Under a cent so far. `).test(text(w.q('#rescore-run-text'))),
      'the run shows in the same place: "' + text(w.q('#rescore-run-text')).slice(0, 40) + '"');
  say(text(w.q('#rescore-live')) === `Rescore started: ${sum.count} roles.`,
      'and its first words are written into the status region that was there all along, so they are read out');
  const poll = w.calls[w.calls.length - 1];
  say(poll.url === '/cv/rescore/status', 'and the page asks how far it has got');
  w.answer({ state: 'running', count: sum.count, done: 1, failed: 0, left: 0, unstored: 0, cost_cents: 16.2, mode: 'live' });
  await settle(); await settle();
  say(new RegExp(`^Rescoring 2 of ${sum.count}. 16 cents so far. It carries on if you leave this page`).test(text(w.q('#rescore-run-text'))),
      'the running line carries its cost so far, as the states frame draws it: "' + text(w.q('#rescore-run-text')).slice(0, 44) + '"');
  w.flush();
  await settle();
  w.answer({ state: 'running', count: sum.count, done: 1, failed: 0, left: 0, unstored: 0, cost_cents: 0, mode: 'mock' });
  await settle(); await settle();
  say(/^Rescoring 2 of \d+. On the test mocks, nothing paid so far./.test(text(w.q('#rescore-run-text'))),
      'on the mocks it says nothing is paid');
  w.flush();
  await settle();
  w.answer({ state: 'done', count: sum.count, cost_cents: 41.6,
             result: { ok: true, count: sum.count, rescored: sum.count - 2, failed: 1, unstored: 1, left: 0, rose: 3, fell: 1, mode: 'live' } });
  await settle(); await settle();
  const doneWords = text(w.q('#rescore-run-text'));
  say(new RegExp(`Rescored ${sum.count - 2} of ${sum.count}, ${sum.count} paid calls, 42 cents. 4 roles moved tier: 3 rose, 1 fell. 1 role was not rescored`)
        .test(doneWords) && w.q('#rescore-run').classList.contains('notice-ok'),
      'done, it says what it did and what it cost, as the runs table logged it: ' + doneWords.slice(0, 80));
  say(/1 role was scored and paid for but not stored: the database stayed busy, so it keeps its earlier score and is still flagged./.test(doneWords)
      && text(w.q('#rescore-live')) === doneWords,
      'it says what was paid for and not stored, and the status region says it all too');

  // ---- the Preferences tab
  w.click(w.q('#tab-settings'));
  const pass = w.q('#pref-cv_pass_score');
  w.input(pass, '75');
  say(pass.classList.contains('changed') && /Changed from 70/.test(text(pass.parentNode)),
      'a changed preference is marked in words and with a bar, "Changed from 70"');
  w.flush();
  await settle();
  const dry = w.calls[w.calls.length - 1];
  say(dry.url === '/cv/preferences' && JSON.stringify(JSON.parse(dry.opts.body)) === JSON.stringify({ values: { cv_pass_score: '75' }, dry_run: true }),
      'a moment after typing it asks, as a dry run, how many roles saving would move');
  w.answer({ ok: true, dry_run: true, note: 'Saving would move 3 roles: 3 from Top to Reading.' });
  await settle(); await settle();
  say(text(w.q('#prefs-say')) === 'Saving would move 3 roles: 3 from Top to Reading.', 'and says it under the form');
  w.input(pass, 'seventy');
  const nCalls = w.calls.length;
  w.fire(w.q('#prefs-form'), 'submit');
  say(w.calls.length === nCalls && pass.getAttribute('aria-invalid') === 'true'
      && w.sandbox.document.activeElement === pass && /plain number/.test(text(w.q('#prefs-say'))),
      'a value that is not a number is marked and focused, and nothing is sent');
  w.input(pass, '75');
  w.fire(w.q('#prefs-form'), 'submit');
  await settle();
  w.answer({ ok: true, saved: true, resorted: false, values: { cv_pass_score: '75' },
             note: 'Saved, but the roles could not be re-sorted, the database is busy (database is locked). No role moved. Re-sort now tries again.' });
  await settle(); await settle();
  const resortBtn = w.q('#prefs-say #prefs-resort');
  say(resortBtn && text(resortBtn) === 'Re-sort now' && pass.getAttribute('data-was') === '70'
      && pass.classList.contains('changed') && /No role moved/.test(text(w.q('#prefs-say'))),
      'a save the database was too busy to re-sort keeps a Re-sort now action and leaves the field marked as changed from 70');
  const beforeResort = w.calls.length;
  w.click(resortBtn);
  await settle();
  say(w.calls.length === beforeResort + 1 && w.calls[w.calls.length - 1].url === '/cv/preferences'
      && JSON.stringify(JSON.parse(w.calls[w.calls.length - 1].opts.body)) === JSON.stringify({ values: { cv_pass_score: '75' } }),
      'and Re-sort now sends the same values again, not as a dry run, which saves and re-sorts');
  const saveCall = w.calls[w.calls.length - 1];
  say(saveCall.url === '/cv/preferences' && JSON.stringify(JSON.parse(saveCall.opts.body)) === JSON.stringify({ values: { cv_pass_score: '75' } }),
      'Save and re-sort sends only what changed');
  w.answer({ ok: true, saved: true, resorted: true, values: { cv_pass_score: '75' }, note: 'Saved. 3 roles moved: 3 from Top to Reading.' });
  await settle(); await settle();
  say(text(w.q('#prefs-say')) === 'Saved. 3 roles moved: 3 from Top to Reading.' && !pass.classList.contains('changed'),
      'and says how many moved, the field no longer marked as changed');
  const loc = w.q('#pref-location');
  w.input(loc, 'Remote in the UK.');
  w.click(w.q('[data-section-save="location"]'));
  await settle();
  const secCall = w.calls[w.calls.length - 1];
  say(secCall.url === '/cv/preferences/section' && JSON.parse(secCall.opts.body).key === 'location', 'Save location sends the words');
  const sentNow = w.calls.length;
  w.answer({ ok: true, key: 'location', text: 'Remote in the UK.', note: 'Saved.',
             offer: { scope: 'open', count: 9, spec: 0, applied: 1, cost: { words: 'about 9 to 17 cents' } } });
  await settle(); await settle();
  const offerBtn = w.q('#section-say [data-rescore="open"]');
  say(offerBtn && text(offerBtn) === 'Rescore all 9 open roles, about 9 to 17 cents' && w.q('#section-say [data-leave]')
      && w.calls.length === sentNow,
      'a location change offers a paid rescore with its count and cost, beside Leave them, and runs nothing');
  w.click(offerBtn);
  say(!w.q('#rescore-confirm').hidden && /Rescore all 9 open roles/.test(text(w.q('#rescore-confirm-text')))
      && /1 role you have applied for keeps its score/.test(text(w.q('#rescore-confirm-text')))
      && w.calls.length === sentNow,
      'and pressing it opens the same confirm, still sending nothing');
  w.click(w.q('#rescore-cancel'));

  // ---- the Word import
  w.click(w.q('#tab-import'));
  say(w.q('#cv-file').getAttribute('tabindex') === '-1' && w.q('#import-choose'),
      'Word import is reached by a real button, the file field behind it');

  // ---- /cv#settings opens Preferences
  const w2 = world(page, { hash: '#settings' });
  await settle();
  say(w2.q('#tab-settings').getAttribute('aria-selected') === 'true' && !w2.q('#settings').hidden,
      '/cv#settings, Home\'s link, opens the Preferences tab');

  // ---- an older version, viewed read only
  const w3 = world(fs.readFileSync(C.pages.viewing, 'utf8'));
  await settle();
  say(!w3.q('#cv-editor textarea') && !w3.q('.cv-part[tabindex]') && w3.q('#tb-save').hidden
      && text(w3.q('#save-state')) === 'Read only' && w3.q('#tb-add').getAttribute('aria-disabled') === 'true',
      'an older version opened from History is read only, with nothing to edit and nothing to save');
  w3.click(w3.q('#view-restore'));
  await settle();
  say(w3.calls.length === 1 && w3.calls[0].url === '/cv/master/save'
      && JSON.parse(w3.calls[0].opts.body).base === C.expect.version
      && JSON.parse(w3.calls[0].opts.body).text === C.expect.viewText && w3.confirms.length === 1,
      'Start a new version from this one asks first, then saves that text as the next version');

  // ---- the phone's Edit button
  const w4 = world(page);
  await settle();
  w4.click(w4.q('[data-part-toggle="s1"]'));
  say(w4.q('.cv-part[data-part="s1"] textarea') && text(w4.q('[data-part-toggle="s1"]')) === 'Done',
      'Edit beside a section, the phone\'s way in, opens its lines; it then reads Done');
  w4.click(w4.q('[data-part-toggle="s1"]'));
  say(!w4.q('#cv-editor textarea'), 'and Done closes it');

  // ---- over two pages
  const w5 = world(fs.readFileSync(C.pages.over, 'utf8'));
  await settle();
  const limit = w5.q('#cv-editor .cv-break.is-limit');
  const overNote = w5.q('#over-note');
  say(limit && limit.getAttribute('id') === 'page-3' && limit.getAttribute('tabindex') === '-1'
      && !overNote.hidden && new RegExp('Page 3 starts at line ' + C.expect.overFirst).test(text(overNote))
      && /Saving is still allowed/.test(text(overNote)),
      'a CV past two pages says so, names where page 3 starts, and marks the limit on the sheet');
  say(w5.qa('#cv-editor .cvl.cv-over').length > 0
      && w5.qa('#cv-editor .cvl.cv-over').every((r) => Number(r.getAttribute('data-n')) >= C.expect.overFirst),
      'every line past the limit is marked, and none before it');
  w5.click(w5.q('#go-page-3'));
  say(w5.sandbox.document.activeElement === w5.q('#page-3'), 'Go to page 3 moves focus to the page itself');
  say(w5.q('#tb-save').getAttribute('data-cv-act') === 'master', 'and Save version is still there to use');

  console.log(failed ? `${failed} FAILED` : 'harness done');
  process.exit(failed ? 1 : 0);
})().catch((err) => { console.log('FAIL  the harness stopped: ' + (err && err.stack)); process.exit(1); });
