// Drives the Prepare application page's own script (prepare_page.PREPARE_SCRIPT)
// in node, on the page as the server draws it, with the editor's scripts beside
// it as the page runs them, and prints PASS or FAIL lines.
// run_prepare_test.py writes the pages to a JSON file and runs:
//   node prepare_script_harness.js <data file>
// No browser and no network: fetch is a stub that records what was sent and
// answers when the test says. The small DOM follows run_cv_test.py's editor
// harness, with a localStorage that keeps what is set and a history stub.
'use strict';
const vm = require('vm');
const fs = require('fs');
const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
let bad = 0;
const say = (ok, label) => {
  console.log((ok ? 'PASS  ' : 'FAIL  ') + label);
  if (!ok) bad += 1;
};
const settle = () => new Promise((resolve) => setImmediate(resolve));
const ENT = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0',
              rsquo: '’', ldquo: '“', rdquo: '”', bull: '•', mdash: '—' };
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
function world(html, hash) {
  const storage = {};
  const events = [];
  const calls = [];
  const pending = [];
  const timers = [];
  const confirms = [];
  let confirmAnswer = true;
  const observers = [];
  class Text { constructor(t) { this.nodeType = 3; this._t = t; this.parentNode = null; }
    get textContent() { return this._t; } set textContent(v) { this._t = String(v); } }
  class El {
    constructor(tag) {
      this.tagName = tag.toUpperCase(); this.nodeType = 1; this.attrs = {}; this.children = [];
      this.parentNode = null; this.listeners = {}; this.style = {}; this._text = '';
      this.value = ''; this.disabled = false; this.hidden = false; this.rows = 2;
      const me = this;
      this.classList = {
        contains: (k) => (me.attrs['class'] || '').split(/\s+/).includes(k),
        add: (k) => { if (!me.classList.contains(k)) me.attrs['class'] = ((me.attrs['class'] || '') + ' ' + k).trim(); },
        remove: (k) => { me.attrs['class'] = (me.attrs['class'] || '').split(/\s+/).filter((x) => x && x !== k).join(' '); },
        toggle: (k, on) => { const want = on === undefined ? !me.classList.contains(k) : !!on;
          if (want) me.classList.add(k); else me.classList.remove(k); return want; },
      };
    }
    setAttribute(n, v) { this.attrs[n] = String(v); if (n === 'hidden') this.hidden = true;
      if (n === 'disabled') this.disabled = true; }
    getAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null; }
    hasAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n); }
    removeAttribute(n) { delete this.attrs[n]; }
    get className() { return this.attrs['class'] || ''; }
    set className(v) { this.attrs['class'] = String(v); }
    get title() { return this.attrs.title || ''; } set title(v) { this.attrs.title = String(v); }
    get type() { return this.attrs.type || ''; } set type(v) { this.attrs.type = String(v); }
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
    insertBefore(c, ref) { c.parentNode = this; const i = this.children.indexOf(ref);
      if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; }
    get firstChild() { return this.children[0] || null; }
    get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join('') : this._text; }
    set textContent(v) {
      // As Chrome and Edge do: a focused field that is taken away is told it
      // lost focus first, while it is still in the page.
      const active = document.activeElement;
      if (active && active !== this && this.contains(active)) {
        document.activeElement = null;
        fire(active, 'focusout');
      }
      this.children.forEach((c) => { c.parentNode = null; }); this.children = []; this._text = String(v);
    }
    set innerHTML(h) { this.textContent = ''; parse(String(h), this);
      observers.forEach((o) => { if (o.target === this) o.fn([]); }); }
    walk(fn) { for (const c of this.children) { if (c.nodeType === 1) { fn(c); c.walk(fn); } } }
    querySelectorAll(sel) { const out = []; this.walk((el) => { if (matches(el, sel)) out.push(el); }); return out; }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    closest(sel) { let cur = this; while (cur && cur.nodeType === 1) { if (matches(cur, sel)) return cur; cur = cur.parentNode; } return null; }
    contains(node) { let cur = node; while (cur) { if (cur === this) return true; cur = cur.parentNode; } return false; }
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
    focus() { document.activeElement = this; }
    click() { return fire(this, 'click'); }
    setSelectionRange(a) { this.selectionStart = this.selectionEnd = a; }
    scrollIntoView() { events.push('scroll:' + (this.getAttribute('data-place') || this.getAttribute('data-n'))); }
    get scrollHeight() { return 20; }
    get clientWidth() { return 600; }
  }
  const document = new El('#document');
  document.documentElement = new El('html');
  document.createElement = (t) => new El(t);
  document.createTextNode = (t) => new Text(t);
  document.getElementById = (id) => document.querySelector('#' + id);
  const VOID = new Set(['br', 'img', 'input', 'meta', 'link', 'hr', 'col', 'source', 'wbr']);
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
      stack[stack.length - 1].appendChild(el);
      if (RAW.has(tag)) {
        const end = h.toLowerCase().indexOf('</' + tag, last);
        const inner = h.slice(last, end < 0 ? h.length : end);
        if (tag === 'textarea') el.value = decode(inner);
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
  document.body = body;
  parse(html, body);
  const windowListeners = {};
  const sandbox = {
    Headers, JSON, Array, Object, String, Number, Promise, Math, Error,
    document, localStorage: { getItem(k) { return Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null; },
                              setItem(k, v) { storage[k] = String(v); } },
    location: { pathname: '/prepare/7', hash: hash || '', hashSet: null, reload() { events.push('reload'); } },
    history: { replaceState(_s, _t, url) { sandbox.location.hashSet = String(url).replace(/^[^#]*/, ''); } },
    setTimeout() { timers.push('timeout'); }, setInterval() { timers.push('interval'); },
    confirm(q) { confirms.push(q); return confirmAnswer; },
    alert() { events.push('alert'); },
    MutationObserver: class { constructor(fn) { this.fn = fn; }
      observe(target) { observers.push({ target, fn: this.fn }); } },
    ResizeObserver: class { constructor(fn) { this.fn = fn; } observe() {} },
    addEventListener(t, fn) { (windowListeners[t] = windowListeners[t] || []).push(fn); },
  };
  sandbox.window = sandbox;
  sandbox.fetch = (url, opts) => {
    calls.push({ url, opts });
    return new Promise((resolve, reject) => { pending.push({ resolve, reject }); });
  };
  vm.createContext(sandbox);
  vm.runInContext(data.scripts, sandbox);
  function fire(target, type, extra) {
    const ev = Object.assign({ type, target, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; } }, extra || {});
    let cur = target;
    while (cur) { (cur.listeners && cur.listeners[type] || []).forEach((fn) => fn(ev)); cur = cur.parentNode; }
    (windowListeners[type] || []).forEach((fn) => fn(ev));
    return ev;
  }
  const q = (sel) => document.querySelector(sel);
  const qa = (sel) => document.querySelectorAll(sel);
  return { document, sandbox, events, calls, pending, timers, confirms, q, qa, fire, storage,
           get location() { return sandbox.location; },
           setConfirm(v) { confirmAnswer = v; },
           click(el) { return fire(el, 'click'); },
           answer(value) { pending.shift().resolve({ json: () => Promise.resolve(value) }); },
           answerAt(i, value) { pending.splice(i, 1)[0].resolve({ json: () => Promise.resolve(value) }); },
           fail() { pending.shift().reject(new Error('network')); },
           sayText() { const n = document.getElementById('cv-say'); return n ? n.textContent : null; },
           sayBad() { const n = document.getElementById('cv-say'); return n ? n.classList.contains('bad') : null; },
           RadarCv: sandbox.RadarCv };
}
const body = (w, i) => JSON.parse(w.calls[i].opts.body);
const txt = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
const key = (w, target, k, extra) => w.fire(target, 'keydown', Object.assign({ key: k }, extra || {}));

(async () => {
  // ------------------------------------------------ opening spends nothing
  let w = world(data.pages.before);
  await settle(); await settle();
  say(w.calls.length === 0 && w.timers.length === 0,
      'a page opened with a full description still to fetch sends nothing on opening and sets no timer');
  const go = w.q('[data-pa-fetch]');
  say(!!go && /^About 3 cents, 1 call: Fetch and score the full description$/.test(txt(go)),
      'its button states the cost before the verb: "' + txt(go) + '"');
  w = world(data.pages.fromJobs);
  await settle(); await settle();
  say(w.calls.length === 0, 'and so does the same page as the Jobs link opens it, ?go=1 and all');

  // ---------------------------------------------------- the one press
  w = world(data.pages.before);
  await settle();
  w.click(w.q('[data-pa-fetch]'));
  w.click(w.q('[data-pa-fetch]'));
  await settle();
  const sent = w.calls[0] && w.calls[0].opts;
  say(w.calls.length === 1 && w.calls[0].url === '/prepare' && sent.method === 'POST'
      && body(w, 0).id === data.ids.before && sent.headers instanceof Headers
      && sent.headers.get(data.header) === '1',
      'a press makes one POST of the role id to /prepare, through the fetch wrapper, with the guard header; a second press while it is out sends nothing');
  const held = w.q('[data-pa-fetch]');
  say(held.disabled && txt(held) === 'Fetching and scoring…'
      && txt(w.q('[data-fig="spec"] .num')) === 'Working' && !w.q('[data-pa-working]').hidden,
      'the button is held and says what it is doing, and the Full spec figure says Working');
  w.answer({ ok: false, note: 'The write guard refused this request.' });
  await settle(); await settle();
  const back = w.q('[data-pa-fetch]');
  say(!back.disabled && /Fetch and score/.test(txt(back)) && /refused/.test(txt(w.q('[data-pa-working]')))
      && txt(w.q('[data-fig="spec"] .num')) === 'Not fetched',
      'a refusal gives the button back with its words, says why, and puts the figure back');
  w.click(w.q('[data-pa-fetch]'));
  await settle();
  w.fail();
  await settle(); await settle();
  say(!w.q('[data-pa-fetch]').disabled && /No usable answer/.test(txt(w.q('[data-pa-working]'))),
      'a request that fails says so and gives the button back');
  w.click(w.q('[data-pa-fetch]'));
  await settle();
  w.answer({ ok: true, outcome: 'no_description', html: data.answers.failed });
  await settle(); await settle();
  say(/Nothing was charged/.test(txt(w.q('[data-slot="score"]')))
      && /About 3 cents, 1 call: Try again/.test(txt(w.q('[data-slot="score"] [data-pa-fetch]')))
      && w.qa('#prepare-body template').length === 0,
      'a fetch that failed is drawn in place, says nothing was charged, and offers Try again with its cost');
  w.click(w.q('[data-pa-fetch]'));
  await settle();
  w.answer({ ok: true, outcome: 'scored', html: data.answers.scored });
  await settle(); await settle();
  say(w.qa('[data-slot="notes"] [data-note]').length > 0 && !w.q('[data-pa-fetch]')
      && w.qa('#prepare-body template').length === 0 && w.calls.length === 4 && w.timers.length === 0,
      'the scored answer fills the score, the notes and the rest in place, offers no button, and sends nothing more');

  // ----------------------------- the status region and focus after a press
  w = world(data.pages.before);
  await settle();
  const live = w.q('#pa-live');
  say(!!live && txt(live) === '' && !live.closest('[data-slot]') && live.getAttribute('role') === 'status',
      'the status region is on the page from the start, empty, outside every part a press redraws');
  w.click(w.q('[data-pa-fetch]'));
  await settle();
  say(/^Working\. The advert is read from LinkedIn once/.test(txt(w.q('#pa-live'))),
      'a press writes its working message into that region, which was there before it');
  w.answer({ ok: true, outcome: 'scored', html: data.answers.scored });
  await settle(); await settle();
  say(w.document.activeElement === w.q('#score-h') && w.q('#score-h').getAttribute('tabindex') === '-1'
      && /^Scored once\. The full spec CV match is \d+\.$/.test(txt(w.q('#pa-live'))),
      'when the press finishes, focus moves to the Score heading and the region says the outcome: "' + txt(w.q('#pa-live')) + '"');
  w = world(data.pages.before);
  await settle();
  w.click(w.q('[data-pa-fetch]'));
  await settle();
  w.answer({ ok: false, note: 'The write guard refused this request.' });
  await settle(); await settle();
  say(/refused/.test(txt(w.q('#pa-live'))) && w.document.activeElement !== w.q('#score-h'),
      'a refusal is said in the region too, and focus stays where it was');

  // Rescore role, the other paid press, through the editor.
  w = world(data.pages.main);
  await settle(); await settle();
  const rescore = w.q('[data-cv-act="rescore"]');
  w.click(rescore);
  await settle();
  say(w.calls.length === 1 && w.calls[0].url === '/prepare/cv/rescore'
      && /^Rescoring\./.test(txt(w.q('#pa-live'))),
      'Rescore role sends one rescore and says it is working in the region');
  w.answer(data.answers.rescored);
  await settle(); await settle();
  say(w.document.activeElement === w.q('#score-h')
      && /^Rescored\. CV match .*This copy now scores \d+\.$/.test(txt(w.q('#pa-live'))),
      'and when it lands focus moves to the Score heading and the region says what moved: "' + txt(w.q('#pa-live')) + '"');

  // --------------------------------------------- the sheet's A4 pages
  w = world(data.pages.main);
  await settle(); await settle();
  const breaks = w.qa('#cv-editor .pa-break');
  const after = breaks.map((b) => {
    const kids = b.parentNode.children;
    const next = kids[kids.indexOf(b) + 1];
    return next ? Number(next.getAttribute('data-n')) : null;
  });
  say(breaks.length === data.pages_estimate.firsts.length - 1
      && JSON.stringify(after) === JSON.stringify(data.pages_estimate.firsts.slice(1))
      && txt(w.q('#pa-pages')) === data.pages_estimate.words,
      'the sheet breaks into the A4 pages the CV page estimates for its Design, at lines '
      + JSON.stringify(after) + ', and says how many under it');
  const firstLine = w.qa('#cv-editor .cvl-bullet textarea[data-edit], #cv-editor .cvl-plain textarea[data-edit]')[0];
  firstLine.value = firstLine.value + ' and a great deal more about the work of the role'.repeat(160);
  w.fire(firstLine, 'input');
  await settle(); await settle();
  const grown = w.qa('#cv-editor .pa-break');
  const limit = w.q('#cv-editor .pa-break.is-limit');
  const overFrom = limit ? Number(limit.parentNode.children[limit.parentNode.children.indexOf(limit) + 1].getAttribute('data-n')) : null;
  const over = w.qa('#cv-editor .cvl.pa-over').map((r) => Number(r.getAttribute('data-n')));
  say(grown.length >= 2 && !!limit && /^Over the two-page limit\. /.test(limit.getAttribute('aria-label'))
      && over.length > 0 && over.every((n) => n >= overFrom)
      && /^About \d+ pages of A4 in the Word file.*Over the two-page limit from line \d+/.test(txt(w.q('#pa-pages')))
      && w.q('#pa-pages').classList.contains('is-over'),
      'and drawn again as the copy is edited: a copy grown past two pages gets its breaks, the limit marked, the lines past it marked in the margin, and the count under it ('
      + grown.length + ' breaks, over from line ' + overFrom + ')');

  // ------------------------------- the add chips beside the edited line
  w = world(data.pages.main);
  await settle(); await settle();
  say(w.qa('#cv-editor .cv-add').length > 0 && w.qa('#cv-editor .cv-add.pa-add-on').length === 0,
      'no add chip is shown before a line is edited');
  const one = w.q('#cv-editor textarea[data-edit="h.0"]').closest('.cvl');
  w.fire(one.querySelector('textarea'), 'focusin');
  const on = w.qa('#cv-editor .cv-add.pa-add-on');
  const kids = one.parentNode.children;
  const between = (c) => {
    const i = kids.indexOf(one); const j = kids.indexOf(c);
    return j > i && kids.slice(i + 1, j).every((k) => k.classList.contains('cv-add')
      || k.classList.contains('cvl-bullet') || k.classList.contains('cvl-plain') || k.classList.contains('pa-break'));
  };
  say(on.length === 1 && on.every((c) => c.parentNode === one.parentNode && between(c))
      && txt(on[0]) === '+ Headline line' && one.classList.contains('pa-on'),
      'editing a line shows the add chips of its run, and only those, and marks the line for its buttons on touch');
  w.click(w.q('#cv-editor [data-add="line"][data-at="s0"]'));
  await settle();
  const added = w.q('#cv-editor textarea[data-edit="s0.l0"]');
  w.fire(added, 'focusin');
  const moved = w.qa('#cv-editor .cv-add.pa-add-on');
  say(!!added && added.closest('.cvl').classList.contains('pa-on')
      && w.qa('#cv-editor .cvl.pa-on').length === 1 && moved.length > 0
      && moved.every((c) => c.parentNode.getAttribute('data-part') === 's0'),
      'editing a line in another part moves them there, and the first line no longer shows its buttons');
  w.fire(w.q('[data-pa-apply]'), 'focusin');
  say(w.qa('#cv-editor .cv-add.pa-add-on').length === 0 && w.qa('#cv-editor .cvl.pa-on').length === 0,
      'and leaving the sheet hides them again');

  // ---------------------------------------------------------- the notes
  w = world(data.pages.main);
  await settle(); await settle();
  const groups = w.qa('[data-slot="notes"] ol.notes[data-pa-group]').map((g) => Number(g.getAttribute('data-pa-group')));
  say(groups.length > 1 && groups.every((n, i) => i === 0 || groups[i - 1] < n),
      'the notes sit in groups by the line they stand beside, in the order of the sheet');
  const met = w.qa('[data-slot="notes"] [data-note][data-status="met"]');
  const partly = w.qa('[data-slot="notes"] [data-note][data-status="partly"]');
  say(met.length > 0 && met.every((n) => n.classList.contains('is-closed')
      && n.querySelector('.note-toggle').getAttribute('aria-expanded') === 'false'
      && n.querySelector('.note-body').hidden)
      && partly.length > 0 && partly.every((n) => n.classList.contains('is-open') && !n.querySelector('.note-toggle')),
      'met notes fold to one line and partly notes stay open');
  const gapNotes = w.qa('[data-slot="gaps"] [data-note][data-status="missing"]');
  say(gapNotes.length === data.counts.missing && w.qa('[data-slot="notes"] [data-status="missing"]').length === 0,
      'the gaps are listed above the sheet, and none of them beside it');
  const first = met[0];
  w.click(first.querySelector('.note-toggle'));
  say(first.classList.contains('is-open') && !first.querySelector('.note-body').hidden
      && first.querySelector('.note-toggle').getAttribute('aria-expanded') === 'true'
      && first.classList.contains('is-selected') && w.qa('#cv-editor .cvl.pa-lit').length > 0,
      'a click opens a met note, selects it and lights the lines it cites');
  w.click(first.querySelector('.note-toggle'));
  say(first.classList.contains('is-closed'), 'and a second click folds it again');
  const second = met[1] || met[0];
  w.fire(second.querySelector('.note-toggle'), 'focusin');
  say(second.classList.contains('is-open'), 'focus opens a met note');
  w.fire(second.querySelector('.note-toggle'), 'focusout', { relatedTarget: null });
  say(second.classList.contains('is-closed'), 'and it folds again when focus leaves it');
  const show = w.q('[data-slot="gaps"] [data-pa-show]');
  w.click(show);
  say(w.q('[data-slot="advert"]').classList.contains('is-open')
      && w.q('#' + show.getAttribute('data-pa-show')).classList.contains('sel'),
      'Show it in the advert opens the folded description and marks the sentence');

  // ------------------------------------------------------------- the keys
  w = world(data.pages.main);
  await settle(); await settle();
  const field = w.q('#cv-editor textarea');
  if (field) key(w, field, 'm');
  key(w, w.document.body, 'm', { ctrlKey: true });
  key(w, w.document.body, 'M', { shiftKey: true });
  await settle();
  say(w.calls.length === 0 && !w.q('#pa-apply-dialog').hasAttribute('open'),
      'M does nothing while typing in a field, nor with a modifier');
  w.click(w.q('[data-pa-keys]'));
  key(w, w.document.body, 'm');
  await settle();
  say(w.calls.length === 0 && w.q('[data-pa-keys]').getAttribute('aria-pressed') === 'false'
      && txt(w.q('[data-pa-keys-word]')) === 'Off' && w.storage['radar-jobs-keys'] === 'off',
      'the single-key switch turns the keys off, says so, and is the one Jobs and the board share');
  w.click(w.q('[data-pa-keys]'));
  key(w, w.document.body, 'm');
  await settle();
  say(w.calls.length === 1 && w.calls[0].url === '/apply' && body(w, 0).id === data.ids.main,
      'with the keys on, M marks the role applied: the saved copy is the one on screen, so it asks nothing');
  w.answer({ ok: true, id: data.ids.main, applied: true });
  await settle(); await settle();
  say(w.events.includes('reload') && w.location.hashSet === '#marked',
      'and the page reloads to show it marked applied');

  // ---------------------------------- Mark applied with edits not saved
  w = world(data.pages.main);
  await settle(); await settle();
  const line = w.qa('#cv-editor .cvl-bullet textarea[data-edit], #cv-editor .cvl-plain textarea[data-edit]')[0];
  const lineN = line.closest('.cvl').getAttribute('data-n');
  line.value = line.value + ' and more';
  w.fire(line, 'input');
  key(w, w.document.body, 'm');
  await settle();
  const dlg = w.q('#pa-apply-dialog');
  say(w.calls.length === 0 && dlg.hasAttribute('open')
      && txt(w.q('#pa-dialog-h')) === 'The copy on screen is not the saved one'
      && new RegExp('^Line ' + lineN + ' has changed since you saved at ').test(txt(w.q('#pa-dialog-p')))
      && w.document.activeElement === w.q('[data-pa-dlg="save"]'),
      'with an edit not saved, M opens the dialog on the page, names the line, and starts on Save this copy, then mark applied');
  key(w, w.document.body, 'm');
  say(w.calls.length === 0, 'and while it is open the keys do nothing');
  w.click(w.q('[data-pa-dlg="cancel"]'));
  say(!dlg.hasAttribute('open') && w.document.activeElement === w.q('[data-pa-apply]') && w.calls.length === 0,
      'Cancel closes it, sends nothing and gives focus back to Mark applied');
  w.click(w.q('[data-pa-apply]'));
  w.click(w.q('[data-pa-dlg="save"]'));
  await settle();
  say(w.calls.length === 1 && w.calls[0].url === '/prepare/cv/save',
      'Save this copy, then mark applied sends Save CV first, and nothing else yet');
  w.answer(data.answers.saved);
  await settle(); await settle();
  say(w.calls.length === 2 && w.calls[1].url === '/apply',
      'and once the save is in, marks the role applied');

  // ------------------------------------------------------------- Undo
  w = world(data.pages.applied, '#marked');
  await settle(); await settle();
  say(!w.q('#pa-marked').hidden && w.q('[data-pa-apply]') === null,
      'after Mark applied the page says so, with its Undo, and offers no Mark applied');
  key(w, w.document.body, 'z');
  await settle();
  say(w.calls.length === 1 && w.calls[0].url === '/unapply' && body(w, 0).id === data.ids.applied,
      'Z is Undo: one POST to /unapply');
  say(w.timers.length === 0, 'no timer anywhere in it');
  process.exit(bad ? 1 : 0);
})();
