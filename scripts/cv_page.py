#!/usr/bin/env python
"""The CV editor, as it is drawn on the CV page and the Prepare application page.

The page draws the CV as numbered lines first, from the stored text, so it
reads without its script. The script then takes the CV as cv_doc lays it out,
checks that its own numbering of it is the server's to the line, and turns the
lines into an editor: a field per line, the four standard sections always
there, entries with their bullets, and buttons to add, move and remove.

Every number on the page is the number of a line of the text the server would
store, and so of the text the scorer would number. CV_MODEL_JS is cv_doc's
layout written again for the browser: the same cleaning, the same skipped
lines, the same blank lines, the same way of finding a cited line again.
test/run_cv_test.py runs the two on the same inputs under node and holds them
to the same answers, and the script refuses to edit if they ever disagree on
the CV it was given.

On the Prepare page the editor also ties the CV to the asks: hovering an ask
lights the lines it cites, hovering a line lights the asks that cite it, and
an ask that was met or partly loses that mark the moment a line it cited is
edited away. All of it is drawn from the cited lines the working stores. No
model is asked anything for it.

The script posts only from a button a person presses, through the fetch
wrapper that adds the guard header, one request at a time. The one post it
makes on its own is the one that makes the role's copy the first time its page
opens, which costs nothing.
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import cv_doc  # noqa: E402
import cv_store  # noqa: E402


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def line_kind(line: str) -> tuple[str, str]:
    """A line of the text as the page draws it: its kind and its words."""
    for mark, kind in (("### ", "h3"), ("## ", "h2"), ("# ", "h1"),
                       ("- ", "bullet")):
        if line.startswith(mark):
            return kind, line[len(mark):]
    return ("blank", "") if line == "" else ("plain", line)


def rows_html(text: str) -> str:
    """The CV as numbered lines, read only. Line n here is line n of the text."""
    out = []
    for n, line in enumerate(cv_store.cv_lines(text), 1):
        kind, words = line_kind(line)
        mark = "&bull;" if kind == "bullet" else ""
        out.append(f'<div class="cvl cvl-{kind}" data-n="{n}">'
                   f'<span class="cvl-n" aria-hidden="true">{n}</span>'
                   f'<span class="cvl-mark">{mark}</span>'
                   f'<span class="cvl-t">{esc(words)}</span></div>')
    return "".join(out)


def editor_html(text: str, data: dict) -> str:
    """The numbered CV and what the script needs to make it an editor.

    The script's data is JSON in an attribute, escaped like any other
    attribute, so nothing in a CV can end an element or start a script.
    """
    data = {**data, "doc": cv_doc.load_doc(text),
            "lines": cv_store.cv_lines(text)}
    return (f'<div id="cv-editor" class="cv-lines" data-mode="{esc(data["mode"])}"'
            f' data-cv="{esc(json.dumps(data, ensure_ascii=False))}">'
            f'{rows_html(text)}</div>')


CV_CSS = """
.cv-lines{margin:var(--space-8) 0 var(--space-16)}
.cvl{display:grid;grid-template-columns:2.4em 1em minmax(0,1fr) auto;gap:var(--space-4) var(--space-6);align-items:start;padding:1px 2px;border-radius:var(--radius-sm);transition:background var(--dur-fast) var(--ease)}
.cvl-n{font:400 var(--text-2xs)/2.05 var(--font-mono);color:var(--ink-3);text-align:right;user-select:none}
.cvl-mark{font:400 var(--text-sm)/1.6 var(--font-sans);color:var(--ink-3);text-align:center}
.cvl-t{display:block;font:400 var(--text-sm)/1.6 var(--font-sans);color:var(--ink-1);overflow-wrap:anywhere;min-width:0}
textarea.cvl-t{width:100%;box-sizing:border-box;resize:none;overflow:hidden;border:1px solid transparent;border-radius:var(--radius-sm);background:transparent;padding:0 4px;margin:0;min-height:1.7em}
textarea.cvl-t:hover{border-color:var(--hairline)}
textarea.cvl-t:focus{border-color:var(--accent-quiet);background:var(--surface);outline:none}
textarea.cvl-t:disabled{color:var(--ink-2)}
.cvl-h1 .cvl-t{font:var(--weight-strong) var(--text-lg)/1.35 var(--font-sans)}
.cvl-h2 .cvl-t{font:var(--weight-strong) var(--text-xs)/2 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;color:var(--ink-2)}
.cvl-h3 .cvl-t{font-weight:var(--weight-strong)}
.cvl-blank{min-height:.7em}
.cvl-blank .cvl-n{line-height:1.2}
.cvl.cv-hl{background:var(--accent-wash);box-shadow:inset 3px 0 0 var(--accent)}
.cvl-ops{display:flex;gap:2px;opacity:0;transition:opacity var(--dur-fast) var(--ease)}
.cvl:hover .cvl-ops,.cvl:focus-within .cvl-ops{opacity:1}
.cvl-ops button,.cv-add button{font:400 var(--text-2xs)/1.4 var(--font-sans);border:1px solid var(--hairline-strong);border-radius:var(--radius-sm);background:var(--surface);color:var(--ink-2);padding:1px 6px;cursor:pointer}
.cvl-ops button:hover,.cv-add button:hover{color:var(--accent);border-color:var(--accent-quiet)}
.cv-add{display:flex;flex-wrap:wrap;gap:var(--space-6);margin:2px 0 var(--space-6) calc(3.4em + var(--space-12))}
.cv-actions{display:flex;flex-wrap:wrap;gap:var(--space-8);align-items:center;position:sticky;top:0;z-index:2;background:var(--paper);padding:var(--space-10) 0}
.cv-actions [hidden]{display:none !important}
.cv-note{font:400 var(--text-sm)/1.55 var(--font-sans);color:var(--ink-2);margin:var(--space-8) 0;min-height:1.2em}
.cv-note.bad{color:var(--fail)}
.cv-hint{font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--ink-3);margin:var(--space-4) 0}
.cv-banner{border-left:3px solid #c08a1d;background:var(--tint-sand);border-radius:var(--radius-sm);padding:var(--space-10) var(--space-12);margin:var(--space-12) 0;font:400 var(--text-sm)/1.55 var(--font-sans);color:var(--ink-2)}
@media (hover:none){.cvl-ops{opacity:1}}
@media (max-width:600px){.cvl{grid-template-columns:2em 1em minmax(0,1fr)}.cvl-ops{grid-column:3;justify-content:flex-end;opacity:1}.cv-add{margin-left:calc(3em + var(--space-8))}}
"""

# The layout, written again for the browser. Every function here has its
# twin in cv_doc.py, and test/run_cv_test.py holds each pair to the same
# answers on the same inputs.
CV_MODEL_JS = r"""
var RadarCv = (function () {
  var STANDARD = ['Summary', 'Experience', 'Skills', 'Education and certifications'];
  // cv_doc._ANY_BULLET: a bullet mark, the editor's or one a paste brings.
  var BULLET = /^[-*•+·–—▪◦‣●○■□➢➤✓✔♦◆»]\s+/;
  var HEADING_MARK = /^#{1,6}\s+/;
  var PLAIN_HEADING = /^#{1,3} /;
  // Code points, not escapes, so that the tool chain cannot turn one into the
  // character it names inside a regular expression.
  function dropped(c) {
    return (c >= 0xd800 && c <= 0xdfff) || c === 0xfeff || c === 0xfffe || c === 0xffff;
  }
  function spaced(c) {
    return c < 0x20 || (c >= 0x7f && c <= 0x9f) || c === 0x2028 || c === 0x2029;
  }
  // cv_doc.clean, in its order: drop, make spaces, compose, run together.
  function clean(value) {
    var s = String(value === null || value === undefined ? '' : value);
    s = Array.from(s).map(function (ch) {
      var c = ch.codePointAt(0);
      return dropped(c) ? '' : spaced(c) ? ' ' : ch;
    }).join('');
    if (s.normalize) s = s.normalize('NFC');
    return s.split(/\s+/).filter(Boolean).join(' ');
  }
  function stripMarks(t) {
    for (;;) {
      var m = BULLET.exec(t);
      if (!m) return t;
      t = clean(t.slice(m[0].length));
    }
  }
  function cleanPlain(value, allowBullet) {
    var t = clean(value);
    for (;;) {
      var m = PLAIN_HEADING.exec(t);
      if (m) { t = clean(t.slice(m[0].length)); continue; }
      if (!allowBullet && BULLET.test(t)) { t = stripMarks(t); continue; }
      return t;
    }
  }
  function line(item) {
    if (item && item.b) return { b: true, t: stripMarks(clean(item.t)) };
    var t = cleanPlain(item ? item.t : '', true);
    if (BULLET.test(t)) return { b: true, t: stripMarks(t) };
    return { b: false, t: t };
  }
  // The lines of the text the doc stands for, made tidy the way the server
  // makes it tidy, and the number of the line each place in the doc is on.
  function layout(doc) {
    var out = [];
    var at = {};
    function push(place, text) {
      out.push(text);
      if (place) at[place] = out.length;
    }
    push('name', '# ' + clean(doc.name));
    (doc.headline || []).forEach(function (h, i) {
      var t = cleanPlain(h, false);
      if (t) push('h.' + i, t);
    });
    (doc.sections || []).forEach(function (sec, s) {
      var lines = [];
      var entries = [];
      (sec.lines || []).forEach(function (it, j) {
        var c = line(it);
        if (c.t) lines.push([j, c]);
      });
      (sec.entries || []).forEach(function (e, k) {
        var heading = clean(e.heading);
        var el = [];
        (e.lines || []).forEach(function (it, j) {
          var c = line(it);
          if (c.t) el.push([j, c]);
        });
        if (heading || el.length) entries.push([k, heading, el]);
      });
      if (!lines.length && !entries.length) return;
      push('s' + s + '.g', '');
      push('s' + s + '.t', '## ' + clean(sec.title));
      lines.forEach(function (p) {
        push('s' + s + '.l' + p[0], (p[1].b ? '- ' : '') + p[1].t);
      });
      entries.forEach(function (e) {
        push('s' + s + '.e' + e[0] + '.g', '');
        push('s' + s + '.e' + e[0] + '.h', '### ' + e[1]);
        e[2].forEach(function (p) {
          push('s' + s + '.e' + e[0] + '.l' + p[0], (p[1].b ? '- ' : '') + p[1].t);
        });
      });
    });
    return { lines: out, at: at };
  }
  function text(doc) { return layout(doc).lines.join('\n') + '\n'; }
  // cv_doc.line_key: the whole line, without one heading mark and without
  // the bullet marks in front of it.
  function key(value) {
    return stripMarks(clean(clean(value).replace(HEADING_MARK, '')));
  }
  function findLine(cited, keys) {
    var k = key(cited.text);
    if (!k) return null;
    var n = cited.n;
    if (typeof n === 'number' && n >= 1 && n <= keys.length && keys[n - 1] === k) return n;
    var anchor = typeof n === 'number' ? n : 0;
    var best = null;
    keys.forEach(function (other, i) {
      if (other !== k) return;
      if (best === null || Math.abs(i + 1 - anchor) < Math.abs(best - anchor)) best = i + 1;
    });
    return best;
  }
  return { STANDARD: STANDARD, clean: clean, stripMarks: stripMarks,
           cleanPlain: cleanPlain, line: line, layout: layout, text: text,
           key: key, findLine: findLine };
})();
"""

CV_EDITOR_JS = r"""
(function () {
  var host = document.getElementById('cv-editor');
  if (!host || !window.RadarCv) return;
  var C = window.RadarCv;
  var data;
  try { data = JSON.parse(host.getAttribute('data-cv') || ''); } catch (err) { return; }
  var doc = data.doc;
  var editable = !!data.editable;
  var busy = false;
  var dirty = false;
  var current = C.layout(doc);
  var note = document.getElementById('cv-say');
  var WORD = { met: '✓ Met', partly: '◐ Partly', missing: '✗ Missing' };
  var URL = { open: '/prepare/cv/open', rescore: '/prepare/cv/rescore',
              save: '/prepare/cv/save', reset: '/prepare/cv/reset',
              apply: '/apply', master: '/cv/master/save' };
  var DOING = { open: 'Making the tailored copy from the master CV.',
                rescore: 'Scoring the copy on screen against the full description, once. This takes a few seconds.',
                save: 'Saving the copy.', reset: 'Putting the master CV back.',
                apply: 'Marking the role applied.', master: 'Saving the master CV as a new version.' };
  var BAD = { score_failed: 1, cooling: 1, applied: 1, not_open: 1, db_busy: 1,
              busy: 1, no_description: 1, no_master: 1, score_held: 1,
              copy_moved: 1, master_not_laid_out: 1 };

  function say(words, bad) {
    if (!note) return;
    note.textContent = words || '';
    note.className = 'cv-note' + (bad ? ' bad' : '');
  }
  function all(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  // The script's numbers and the server's must be the same for the CV it was
  // given, or a number on the page could name another line than the scorer's.
  // If they are not, the rows the server drew stay as they are, numbered by
  // the server, and the asks are found in the server's lines.
  var frozen = false;
  if (JSON.stringify(current.lines) !== JSON.stringify(data.lines || [])) {
    editable = false;
    frozen = true;
    current = { lines: (data.lines || []).slice(), at: {} };
    say('The editor could not line up its line numbers with the stored CV, so '
        + 'the CV is shown as stored and cannot be edited on this page.', true);
  }

  // ------------------------------------------------------------ the model
  function slot(place) {
    var m;
    if (place === 'name') return { obj: doc, key: 'name' };
    if ((m = /^h\.(\d+)$/.exec(place))) return { list: doc.headline, i: +m[1] };
    if ((m = /^s(\d+)\.t$/.exec(place))) return { obj: doc.sections[+m[1]], key: 'title' };
    if ((m = /^s(\d+)\.l(\d+)$/.exec(place))) {
      return { list: doc.sections[+m[1]].lines, i: +m[2], item: true };
    }
    if ((m = /^s(\d+)\.e(\d+)\.h$/.exec(place))) {
      return { obj: doc.sections[+m[1]].entries[+m[2]], key: 'heading' };
    }
    if ((m = /^s(\d+)\.e(\d+)\.l(\d+)$/.exec(place))) {
      return { list: doc.sections[+m[1]].entries[+m[2]].lines, i: +m[3], item: true };
    }
    return null;
  }
  function read(place) {
    var r = slot(place);
    if (!r) return '';
    if (r.obj) return r.obj[r.key] || '';
    var v = r.list[r.i];
    return r.item ? (v ? v.t : '') : (v || '');
  }
  function write(place, words) {
    var r = slot(place);
    if (!r) return;
    if (r.obj) r.obj[r.key] = words;
    else if (r.item) r.list[r.i].t = words;
    else r.list[r.i] = words;
  }
  function owner(place) {
    var m = /^(.*)\.l(\d+)$/.exec(place);
    if (m) {
      var parts = /^s(\d+)(?:\.e(\d+))?$/.exec(m[1]);
      var sec = doc.sections[+parts[1]];
      var list = parts[2] === undefined ? sec.lines : sec.entries[+parts[2]].lines;
      return { list: list, i: +m[2], base: m[1], item: true };
    }
    m = /^h\.(\d+)$/.exec(place);
    if (m) return { list: doc.headline, i: +m[1], base: 'h', item: false };
    return null;
  }
  function placeOf(base, i) { return base === 'h' ? 'h.' + i : base + '.l' + i; }

  // ------------------------------------------------------------ drawing
  function row(place, kind, words, opts) {
    var div = document.createElement('div');
    div.className = 'cvl cvl-' + kind;
    div.setAttribute('data-place', place);
    var n = document.createElement('span');
    n.className = 'cvl-n';
    n.setAttribute('aria-hidden', 'true');
    div.appendChild(n);
    var mark = document.createElement('span');
    mark.className = 'cvl-mark';
    mark.textContent = kind === 'bullet' ? '•' : '';
    div.appendChild(mark);
    var t;
    if (editable && opts.edit) {
      t = document.createElement('textarea');
      t.rows = 1;
      t.value = words;
      t.setAttribute('data-edit', place);
      t.setAttribute('aria-label', opts.label);
    } else {
      t = document.createElement('span');
      t.textContent = words;
    }
    t.className = 'cvl-t';
    div.appendChild(t);
    if (editable && opts.ops) {
      var ops = document.createElement('span');
      ops.className = 'cvl-ops';
      opts.ops.forEach(function (op) {
        var b = document.createElement('button');
        b.type = 'button';
        b.setAttribute('data-op', op[0]);
        b.setAttribute('data-at', place);
        b.setAttribute('aria-label', op[2]);
        b.title = op[2];
        b.textContent = op[1];
        ops.appendChild(b);
      });
      div.appendChild(ops);
    }
    host.appendChild(div);
  }
  function adders(list) {
    if (!editable) return;
    var div = document.createElement('div');
    div.className = 'cv-add';
    list.forEach(function (a) {
      var b = document.createElement('button');
      b.type = 'button';
      b.setAttribute('data-add', a[0]);
      b.setAttribute('data-at', a[1]);
      b.textContent = a[2];
      div.appendChild(b);
    });
    host.appendChild(div);
  }
  var LINE_OPS = [['up', '↑', 'Move up'], ['down', '↓', 'Move down'],
                  ['kind', '•', 'Bullet or paragraph'], ['del', '×', 'Delete this line']];
  function draw() {
    host.textContent = '';
    row('name', 'h1', doc.name, { edit: true, label: 'Name' });
    doc.headline.forEach(function (h, i) {
      row('h.' + i, 'plain', h, { edit: true, label: 'Headline line',
          ops: LINE_OPS.filter(function (o) { return o[0] !== 'kind'; }) });
    });
    adders([['line', 'h', '+ Headline line']]);
    doc.sections.forEach(function (sec, s) {
      var standard = C.STANDARD.indexOf(sec.title) >= 0;
      row('s' + s + '.g', 'blank', '', {});
      row('s' + s + '.t', 'h2', sec.title, { edit: !standard, label: 'Section title',
          ops: standard ? null : [['delsec', '×', 'Delete this section']] });
      sec.lines.forEach(function (it, j) {
        row('s' + s + '.l' + j, it.b ? 'bullet' : 'plain', it.t,
            { edit: true, label: it.b ? 'Bullet' : 'Line', ops: LINE_OPS });
      });
      adders([['line', 's' + s, '+ Line'], ['bullet', 's' + s, '+ Bullet']]);
      sec.entries.forEach(function (e, k) {
        var at = 's' + s + '.e' + k;
        row(at + '.g', 'blank', '', {});
        row(at + '.h', 'h3', e.heading, { edit: true, label: 'Entry heading',
            ops: [['eup', '↑', 'Move this entry up'], ['edown', '↓', 'Move this entry down'],
                  ['edel', '×', 'Delete this entry']] });
        e.lines.forEach(function (it, j) {
          row(at + '.l' + j, it.b ? 'bullet' : 'plain', it.t,
              { edit: true, label: it.b ? 'Bullet' : 'Line', ops: LINE_OPS });
        });
        adders([['bullet', at, '+ Bullet'], ['line', at, '+ Line']]);
      });
      adders([['entry', 's' + s, '+ Entry']]);
    });
    all('textarea.cvl-t', host).forEach(grow);
    renumber();
  }
  function grow(t) {
    if (!t.style) return;
    t.style.height = 'auto';
    t.style.height = (t.scrollHeight || 0) + 2 + 'px';
  }
  // A field's height depends on the width it has, which a page opened in a
  // background tab may not have yet, so the fields are sized again whenever
  // the editor's width changes.
  var lastWidth = -1;
  function regrow() {
    var width = host.clientWidth || 0;
    if (width === lastWidth) return;
    lastWidth = width;
    all('textarea.cvl-t', host).forEach(grow);
  }
  if (window.ResizeObserver) new window.ResizeObserver(regrow).observe(host);
  window.addEventListener('load', function () { lastWidth = -1; regrow(); });
  function renumber() {
    current = C.layout(doc);
    all('.cvl', host).forEach(function (div) {
      var n = current.at[div.getAttribute('data-place')];
      var label = div.querySelector('.cvl-n');
      if (label) label.textContent = n ? String(n) : '';
      if (n) div.setAttribute('data-n', String(n));
      else div.removeAttribute('data-n');
    });
    mark();
  }

  // -------------------------------------------- the asks beside the lines
  function asks() { return all('#prepare-body [data-ask]'); }
  function mark() {
    var keys = current.lines.map(C.key);
    asks().forEach(function (ask) {
      var status = ask.getAttribute('data-ask-status');
      var gone = false;
      all('[data-cv-text]', ask).forEach(function (li) {
        var n = Number(li.getAttribute('data-cv-n'));
        var now = C.findLine({ n: n, text: li.getAttribute('data-cv-text') }, keys);
        li.setAttribute('data-now', now ? String(now) : '');
        var b = li.querySelector('b');
        if (b) {
          b.textContent = now ? 'CV line ' + now + (now !== n ? ' (' + n + ' when scored)' : '')
            : 'CV line ' + n + ' when scored, since changed';
        }
        if (li.classList) li.classList.toggle('cv-gone', !now);
        if (!now) gone = true;
      });
      var stale = (status === 'met' || status === 'partly') && gone;
      if (ask.classList) ask.classList.toggle('ask-stale', stale);
      var chip = ask.querySelector('.ask-status');
      if (chip && WORD[status]) {
        chip.textContent = stale ? '! Stale, was ' + status : WORD[status];
      }
    });
  }
  function lit(on) {
    all('.cv-hl').forEach(function (el) { el.classList.remove('cv-hl'); });
    all('.ask-hl').forEach(function (el) { el.classList.remove('ask-hl'); });
    if (!on) return;
    var ask = on.closest ? on.closest('#prepare-body [data-ask]') : null;
    var line = on.closest ? on.closest('#cv-editor .cvl[data-n]') : null;
    if (ask) {
      ask.classList.add('ask-hl');
      var nums = all('[data-now]', ask).map(function (li) { return li.getAttribute('data-now'); })
        .filter(Boolean);
      all('#cv-editor .cvl[data-n]').forEach(function (div) {
        if (nums.indexOf(div.getAttribute('data-n')) >= 0) div.classList.add('cv-hl');
      });
    } else if (line) {
      line.classList.add('cv-hl');
      var n = line.getAttribute('data-n');
      asks().forEach(function (a) {
        if (all('[data-now]', a).some(function (li) { return li.getAttribute('data-now') === n; })) {
          a.classList.add('ask-hl');
        }
      });
    }
  }
  document.addEventListener('mouseover', function (e) { lit(e.target); });
  document.addEventListener('focusin', function (e) { lit(e.target); });
  document.addEventListener('click', function (e) {
    var li = e.target.closest ? e.target.closest('#prepare-body [data-cv-text]') : null;
    if (!li) return;
    var n = li.getAttribute('data-now');
    var div = n ? host.querySelector('.cvl[data-n="' + n + '"]') : null;
    if (div && div.scrollIntoView) div.scrollIntoView({ block: 'center' });
  });
  var body = document.getElementById('prepare-body');
  if (body && window.MutationObserver) {
    new window.MutationObserver(function () { mark(); }).observe(body, { childList: true });
  }

  // ------------------------------------------------------------ editing
  function focus(place, at) {
    var t = host.querySelector('textarea[data-edit="' + place + '"]');
    if (!t) return;
    t.focus();
    if (typeof at === 'number' && t.setSelectionRange) t.setSelectionRange(at, at);
  }
  function changed() { dirty = true; shown(false); }
  host.addEventListener('input', function (e) {
    var t = e.target;
    var place = t.getAttribute ? t.getAttribute('data-edit') : null;
    if (!place) return;
    var parts = String(t.value).split(/\r\n|\r|\n/);
    if (parts.length > 1 && owner(place)) {
      var o = owner(place);
      write(place, parts[0]);
      parts.slice(1).forEach(function (p, i) {
        o.list.splice(o.i + 1 + i, 0, o.item ? { b: !!o.list[o.i].b, t: p } : p);
      });
      changed();
      draw();
      focus(placeOf(o.base, o.i + parts.length - 1));
      return;
    }
    write(place, parts.join(' '));
    if (parts.length > 1) t.value = parts.join(' ');
    grow(t);
    changed();
    renumber();
  });
  host.addEventListener('keydown', function (e) {
    var t = e.target;
    var place = t.getAttribute ? t.getAttribute('data-edit') : null;
    if (!place) return;
    var o = owner(place);
    if (e.key === 'Enter') {
      e.preventDefault();
      if (!o) return;
      var v = String(t.value);
      var cut = typeof t.selectionStart === 'number' ? t.selectionStart : v.length;
      var end = typeof t.selectionEnd === 'number' ? t.selectionEnd : cut;
      write(place, v.slice(0, cut));
      o.list.splice(o.i + 1, 0, o.item ? { b: !!o.list[o.i].b, t: v.slice(end) } : v.slice(end));
      changed();
      draw();
      focus(placeOf(o.base, o.i + 1), 0);
    } else if (e.key === 'Backspace' && t.value === '' && o) {
      e.preventDefault();
      o.list.splice(o.i, 1);
      changed();
      draw();
      if (o.i > 0) focus(placeOf(o.base, o.i - 1), read(placeOf(o.base, o.i - 1)).length);
    }
  });
  host.addEventListener('focusout', function (e) {
    var t = e.target;
    var place = t.getAttribute ? t.getAttribute('data-edit') : null;
    if (!place) return;
    // A field that a redraw has just taken away, after Enter or Backspace,
    // names a place that may now be another line or none: leave it.
    if (typeof host.contains === 'function' && !host.contains(t)) return;
    var o = owner(place);
    if (o && o.i >= o.list.length) return;
    if (o && o.item) {
      var tidy = C.line(o.list[o.i]);
      var redraw = tidy.b !== !!o.list[o.i].b;
      o.list[o.i] = tidy;
      if (redraw) { draw(); return; }
      t.value = tidy.t;
    } else if (o) {
      o.list[o.i] = C.cleanPlain(o.list[o.i], false);
      t.value = o.list[o.i];
    } else {
      write(place, C.clean(read(place)));
      t.value = read(place);
    }
    grow(t);
    renumber();
  });
  function op(kind, place) {
    var m;
    var o = owner(place);
    if (o && (kind === 'up' || kind === 'down')) {
      var j = kind === 'up' ? o.i - 1 : o.i + 1;
      if (j < 0 || j >= o.list.length) return;
      var held = o.list[o.i];
      o.list[o.i] = o.list[j];
      o.list[j] = held;
      changed(); draw(); focus(placeOf(o.base, j));
    } else if (o && kind === 'kind' && o.item) {
      o.list[o.i] = { b: !o.list[o.i].b, t: o.list[o.i].t };
      changed(); draw(); focus(place);
    } else if (o && kind === 'del') {
      o.list.splice(o.i, 1);
      changed(); draw();
    } else if ((m = /^s(\d+)\.e(\d+)\.h$/.exec(place))) {
      var list = doc.sections[+m[1]].entries;
      var k = +m[2];
      if (kind === 'edel') {
        var lines = list[k].lines.filter(function (it) { return C.clean(it.t); });
        if (lines.length && !window.confirm('Delete this entry and its '
            + lines.length + ' line' + (lines.length === 1 ? '' : 's') + '?')) return;
        list.splice(k, 1);
        changed(); draw();
      } else {
        var to = kind === 'eup' ? k - 1 : k + 1;
        if (to < 0 || to >= list.length) return;
        var e = list[k];
        list[k] = list[to];
        list[to] = e;
        changed(); draw(); focus('s' + m[1] + '.e' + to + '.h');
      }
    } else if ((m = /^s(\d+)\.t$/.exec(place)) && kind === 'delsec') {
      if (!window.confirm('Delete this section and everything in it?')) return;
      doc.sections.splice(+m[1], 1);
      changed(); draw();
    }
  }
  function add(kind, at) {
    var m;
    if (at === 'h') {
      doc.headline.push('');
      changed(); draw(); focus('h.' + (doc.headline.length - 1));
      return;
    }
    if ((m = /^s(\d+)$/.exec(at))) {
      var sec = doc.sections[+m[1]];
      if (kind === 'entry') {
        sec.entries.push({ heading: '', lines: [{ b: true, t: '' }] });
        changed(); draw(); focus(at + '.e' + (sec.entries.length - 1) + '.h');
        return;
      }
      sec.lines.push({ b: kind === 'bullet', t: '' });
      changed(); draw(); focus(at + '.l' + (sec.lines.length - 1));
      return;
    }
    if ((m = /^s(\d+)\.e(\d+)$/.exec(at))) {
      var entry = doc.sections[+m[1]].entries[+m[2]];
      entry.lines.push({ b: kind === 'bullet', t: '' });
      changed(); draw(); focus(at + '.l' + (entry.lines.length - 1));
    }
  }
  host.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('[data-op],[data-add]') : null;
    if (!b || busy) return;
    if (b.hasAttribute('data-op')) op(b.getAttribute('data-op'), b.getAttribute('data-at'));
    else add(b.getAttribute('data-add'), b.getAttribute('data-at'));
  });

  // -------------------------------------------------------- the buttons
  function lock(on) {
    all('[data-cv-act]').forEach(function (b) { b.disabled = on || !editable; });
    all('textarea', host).forEach(function (t) { t.disabled = on; });
  }
  // The saved line, the Download link and the hint under it. The line keeps
  // the words the server drew, its date with them, until the copy is taken
  // again from the server (full).
  function shown(full) {
    var saved = document.getElementById('cv-saved');
    var link = document.getElementById('cv-download');
    var hint = document.getElementById('cv-download-hint');
    if (saved && full) {
      saved.textContent = data.saved ? 'Saved as ' + data.saved.name + '.'
        : 'Not saved yet. Save CV stores this copy for this role only.';
    }
    if (link) link.hidden = !data.saved;
    if (hint) {
      hint.textContent = data.saved && (dirty || !data.saved.same)
        ? 'Download gives the copy as it was saved. Save CV again to include what is on screen.' : '';
    }
  }
  function take(copy) {
    doc = copy.doc;
    data.lines = copy.lines;
    data.saved = copy.saved;
    data.hash = copy.hash || null;
    data.needsCopy = false;
    dirty = false;
    var status = document.getElementById('cv-status');
    if (status && copy.master_version) {
      status.textContent = 'Made from the master CV ' + copy.master_version
        + '. Nothing done to the copy changes the master.';
    }
    draw();
    shown(true);
  }
  function told(j) {
    return (j.notices || []).map(function (n) { return n.text; }).join(' ');
  }
  function moved(t) {
    var words = 'Rescored. CV match ';
    if (t.before_cv_match !== null && t.before_cv_match !== undefined) {
      words += t.before_cv_match + ' to ';
    }
    words += t.cv_match + '.';
    if (t.changes) words += ' ' + t.changes + ' ask' + (t.changes === 1 ? '' : 's') + ' changed.';
    return words;
  }
  function send(act, btn) {
    if (busy) return;
    busy = true;
    lock(true);
    var was = btn ? btn.textContent : '';
    if (btn) btn.textContent = 'Working';
    say(DOING[act], false);
    var text = C.text(doc);
    // base is the version the page took: of the master on the CV page, of
    // the copy on a Prepare page, so that a change made in another tab since
    // is not written over without a word.
    var body = act === 'master' ? { text: text, base: data.version || null }
      : (act === 'rescore' || act === 'save') ? { id: data.id, text: text, base: data.hash || null }
      : { id: data.id };
    fetch(URL[act], {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        busy = false;
        lock(false);
        if (btn) btn.textContent = was;
        if (!j || j.ok === false) {
          say((j && j.note) || 'That did not run.', true);
          return;
        }
        if (act === 'apply' || act === 'master') {
          dirty = false;
          window.location.reload();
          return;
        }
        if (j.copy) take(j.copy);
        if (typeof j.html === 'string' && document.getElementById('prepare-body')) {
          document.getElementById('prepare-body').innerHTML = j.html;
        }
        mark();
        var words = told(j);
        if (act === 'rescore' && j.outcome === 'scored' && j.tailored) words = moved(j.tailored);
        if (act === 'save' && j.saved_name) words = 'Saved as ' + j.saved_name + '.';
        say(words || (act === 'open' ? '' : 'Done.'), !!BAD[j.outcome]);
      })
      .catch(function () {
        busy = false;
        lock(false);
        if (btn) btn.textContent = was;
        say('No usable answer came back. Reload the page to see what was stored.', true);
      });
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('[data-cv-act]') : null;
    if (!b || busy || b.disabled) return;
    var act = b.getAttribute('data-cv-act');
    if (act === 'reset' && !window.confirm('Start again from the master CV? The copy on '
        + 'screen is replaced by the master as it is now. The saved copy and the '
        + 'scores are kept.')) return;
    if (act === 'apply' && !data.saved && !window.confirm('No CV has been saved for '
        + 'this role, so none will be recorded with the application. Mark it applied '
        + 'anyway?')) return;
    send(act, b);
  });
  window.addEventListener('beforeunload', function (e) {
    if (!dirty) return;
    e.preventDefault();
    e.returnValue = '';
  });

  if (frozen) mark();
  else draw();
  shown(false);
  lock(false);
  if (data.mode === 'tailored' && editable && data.needsCopy) send('open', null);
})();
"""
