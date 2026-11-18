"""GET /insights/sources: the news watcher's sources, built-in and Luke's, with
what each brought in the last thirty days, and Add for a new one.

Read only, like every page. The writes are four POSTs, each through the write
guard: look (Add, which fetches the address once), add (Add source), pause
(Pause or Resume) and remove (Remove, on Luke's own sources only). See
news_sources.py.
"""

from __future__ import annotations

import html
from datetime import datetime

import insights_page
import news_sources as ns
import page_frame

esc = html.escape

STATUS = {"answering": ("Answering", "st-pos"), "not answering": ("Not answering", "st-neg"),
          "paused": ("Paused", "st-neu")}


def _day(iso) -> str:
    try:
        moment = datetime.strptime(str(iso)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return "Not known"
    return f"{moment.day} {moment:%b}"


def row_html(s: dict, hidden: bool) -> str:
    word, cls = STATUS[s["status"]]
    added = '<span class="src-mine">Added by you</span>' if s["added"] else ""
    acts = [f'<button type="button" class="src-act" data-src-pause="{esc(s["id"])}" '
            f'data-paused="{"1" if s["paused"] else "0"}">{"Resume" if s["paused"] else "Pause"}'
            f'<span class="visually-hidden"> {esc(s["name"])}</span></button>']
    if s["added"]:
        acts.append(f'<button type="button" class="src-act" data-src-remove="{esc(s["id"])}">Remove'
                    f'<span class="visually-hidden"> {esc(s["name"])}</span></button>')
    return (f'<li class="src-row"{" hidden data-more" if hidden else ""}>'
            f'<span class="src-name"><span class="src-n">{esc(s["name"])}</span>{added}</span>'
            f'<span class="src-last"><span class="src-k">Last answered </span>{esc(_day(s["last"]))}</span>'
            f'<span class="src-num"><span class="src-k">Stories 30 days </span>{s["stories"]}</span>'
            f'<span class="src-num"><span class="src-k">Strong 30 days </span>{s["strong"]}</span>'
            f'<span class="src-st"><span class="stchip {cls}">{word}</span></span>'
            f'<span class="src-acts">{"".join(acts)}</span></li>')


def body_html(model: dict) -> str:
    sources = model["sources"]
    rows = "".join(row_html(s, i >= ns.SHOWN) for i, s in enumerate(sources))
    more = len(sources) - ns.SHOWN
    show = (f'<button type="button" class="src-more" id="src-more">Show the other {more}</button>'
            if more > 0 else "")
    head = ('<li class="src-row src-headrow" aria-hidden="true"><span>Source</span><span>Last answered</span>'
            '<span>Stories 30 days</span><span>Strong 30 days</span><span>Status</span><span></span></li>')
    return (
        '<a class="plain src-back" href="/insights">Back to Insights</a>'
        f'<header class="ins-head src-head"><div class="ins-title">{page_frame.chip("feed", "story")}'
        f'<h1>Sources</h1><span class="sec-count" id="src-pill">{model["answering"]} of {model["total"]} answering</span></div>'
        '<form class="src-add" id="src-add" novalidate>'
        '<label class="visually-hidden" for="src-url">A site or feed address</label>'
        '<input class="src-in" id="src-url" type="text" inputmode="url" autocomplete="off" maxlength="2000" '
        'placeholder="Paste a site or feed address">'
        '<button type="submit" class="btn" id="src-go">Add</button></form></header>'
        '<div class="src-look" id="src-look" aria-live="polite"></div>'
        '<section class="src-sec" aria-labelledby="src-h"><header class="sec-head src-sechead">'
        f'<h2 class="sec-title" id="src-h">Your sources</h2><span class="sec-count">{len(sources)}</span>'
        '<span class="src-order">Strongest first</span></header>'
        f'<div class="card src-card"><ul class="src-list" id="src-list">{head}{rows}</ul>{show}</div></section>')


def feed_icon() -> str:
    return page_frame.ICONS["feed"]


CSS = """
.src-back{display:inline-block;margin-top:24px;font-size:14px;line-height:20px}
.src-head{padding-top:8px}
.src-add{display:flex;align-items:center;gap:8px;margin-left:auto;flex:1 1 360px;max-width:520px}
.src-in{flex:1 1 auto;min-width:0;height:36px;padding:0 12px;border:1px solid var(--border-strong);border-radius:8px;background:var(--surface-sunken,var(--canvas));color:var(--text-1);font:400 15px/24px var(--font-sans)}
.src-look:empty{display:none}
.src-look{margin:0 0 16px}
.look{padding:20px;border:1px solid var(--border-subtle);border-radius:14px;background:var(--surface)}
.look-top{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}
.look-url{font:400 14px/20px var(--font-mono);color:var(--text-2);overflow-wrap:anywhere}
.look-line{margin-top:12px;font-size:14px;line-height:20px;color:var(--text-1)}
.look-l{display:block;margin-top:16px;margin-bottom:6px;font-size:14px;line-height:20px;font-weight:600}
.look-name{width:100%;max-width:420px}
.look-h{margin-top:16px;font-size:14px;line-height:20px;font-weight:600}
.look-list{margin-top:8px;display:flex;flex-direction:column;gap:6px}
.look-list li{display:flex;justify-content:space-between;gap:16px;font-size:14px;line-height:20px}
.look-list .d{flex:none;color:var(--text-2)}
.look-acts{display:flex;justify-content:flex-end;align-items:center;gap:16px;margin-top:20px}
.look-cancel{min-height:36px;padding:0;border:0;background:none;color:var(--text-1);font:500 14px/20px var(--font-sans);cursor:pointer}
.look-cancel:hover{text-decoration:underline}
.btn-amber{background:#e0a94a;border-color:#e0a94a;color:#2a1d05;font-weight:600}
.btn-amber:hover{background:#e8b961;border-color:#e8b961;color:#2a1d05}
.stchip{display:inline-flex;align-items:center;height:24px;padding:0 10px;border-radius:12px;border:1px solid;font-size:13px;line-height:20px;font-weight:500;white-space:nowrap}
.stchip.st-pos{border-color:rgba(116,211,155,.45);background:var(--positive-tint);color:var(--positive)}
.stchip.st-neg{border-color:rgba(240,128,128,.45);background:rgba(240,128,128,.1);color:var(--negative)}
.stchip.st-cau{border-color:rgba(240,196,106,.5);background:var(--caution-tint);color:var(--caution)}
.stchip.st-neu{border-color:#2a3238;background:var(--neutral-tint,#1a2025);color:var(--text-2)}
.src-sechead{display:flex;align-items:center;gap:8px}
.src-order{margin-left:auto;font-size:13px;line-height:20px;color:var(--text-2)}
.src-card{padding:4px 0}
.src-row{display:grid;grid-template-columns:minmax(0,2.2fr) minmax(0,1fr) minmax(0,.9fr) minmax(0,.9fr) minmax(0,1.1fr) minmax(0,1fr);gap:12px;align-items:center;padding:12px 20px;border-top:1px solid var(--border-subtle);font-size:14px;line-height:20px}
.src-row[hidden]{display:none}
.src-headrow{border-top:0;padding-top:10px;padding-bottom:10px;font-size:13px;line-height:20px;color:var(--text-2)}
.src-name{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;min-width:0}
.src-n{font:600 15px/22px var(--font-sans);overflow-wrap:anywhere}
.src-mine{display:inline-flex;align-items:center;height:22px;padding:0 8px;border:1px solid rgb(var(--group-story-rgb) / .45);border-radius:11px;background:rgb(var(--group-story-rgb) / .12);color:var(--group-story);font-size:13px;line-height:18px;font-weight:500}
.src-k{display:none}
.src-last,.src-num{color:var(--text-1)}
.src-acts{display:flex;flex-wrap:wrap;gap:12px;justify-content:flex-end}
.src-act,.src-more{min-height:28px;padding:0;border:0;background:none;color:var(--text-1);font:500 14px/20px var(--font-sans);text-decoration:underline;text-decoration-color:#5b6770;text-underline-offset:3px;cursor:pointer}
.src-act:hover,.src-more:hover{text-decoration-color:var(--text-1)}
.src-more{margin:8px 20px 12px}
@media (max-width:767.98px){
  .src-add{flex-basis:100%;max-width:none;margin-left:0}
  .src-headrow{display:none}
  .src-row{grid-template-columns:minmax(0,1fr) auto;gap:6px 12px;padding:14px 16px}
  .src-name{grid-column:1}
  .src-st{grid-column:2;grid-row:1;justify-self:end}
  .src-last,.src-num{grid-column:1 / -1;color:var(--text-2)}
  .src-k{display:inline}
  .src-acts{grid-column:1 / -1;justify-content:flex-start}
}
@media (pointer:coarse){.src-act,.src-more,.look-cancel{min-height:44px}}
"""


SCRIPT = r"""
(function () {
  var form = document.getElementById('src-add');
  if (!form) return;
  var input = document.getElementById('src-url'), go = document.getElementById('src-go');
  var look = document.getElementById('src-look');
  function post(path, body) {
    return fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                         body: JSON.stringify(body) })
      .then(function (r) { return r.json().catch(function () { return { ok: false }; }); });
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function line(text) { look.textContent = ''; var c = el('div', 'look'); c.appendChild(el('p', 'look-line', text)); look.appendChild(c); }
  function top(url, chip, cls) {
    var t = el('div', 'look-top');
    t.appendChild(el('span', 'look-url', url));
    t.appendChild(el('span', 'stchip ' + cls, chip));
    return t;
  }
  function close() { look.textContent = ''; input.focus(); }
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var url = input.value.trim();
    if (!url) { input.focus(); return; }
    go.disabled = true;
    post('/insights/sources/look', { url: url }).then(function (j) {
      go.disabled = false;
      look.textContent = '';
      if (!j || !j.ok) { line((j && j.note) || 'That did not go through.'); return; }
      if (j.already) { line(j.note); return; }
      var card = el('div', 'look');
      if (!j.found) {
        card.appendChild(top(j.url, 'No feed found', 'st-cau'));
        card.appendChild(el('p', 'look-line', j.note));
        var acts = el('div', 'look-acts');
        var x = el('button', 'look-cancel', 'Close'); x.type = 'button'; x.addEventListener('click', close);
        acts.appendChild(x); card.appendChild(acts); look.appendChild(card); x.focus();
        return;
      }
      card.appendChild(top(j.url, 'Feed found', 'st-pos'));
      var lab = el('label', 'look-l', 'Name'); lab.setAttribute('for', 'src-name');
      var name = el('input', 'src-in look-name'); name.id = 'src-name'; name.type = 'text'; name.maxLength = 120; name.value = j.name || '';
      card.appendChild(lab); card.appendChild(name);
      card.appendChild(el('h3', 'look-h', 'Latest from this source'));
      var ul = el('ul', 'look-list');
      (j.latest || []).forEach(function (it) {
        var li = el('li'); li.appendChild(el('span', 'h', it.headline)); li.appendChild(el('span', 'd', it.date)); ul.appendChild(li);
      });
      card.appendChild(ul);
      var say = el('p', 'look-line'); say.setAttribute('aria-live', 'polite');
      var acts2 = el('div', 'look-acts');
      var cancel = el('button', 'look-cancel', 'Cancel'); cancel.type = 'button'; cancel.addEventListener('click', close);
      var add = el('button', 'btn btn-amber', 'Add source'); add.type = 'button';
      add.addEventListener('click', function () {
        add.disabled = true;
        post('/insights/sources/add', { token: j.token, name: name.value }).then(function (r) {
          add.disabled = false;
          if (r && r.ok) { location.reload(); return; }
          say.textContent = (r && r.note) || 'That did not go through.';
        });
      });
      acts2.appendChild(cancel); acts2.appendChild(add);
      card.appendChild(say); card.appendChild(acts2);
      look.appendChild(card);
      name.focus();
    }).catch(function () { go.disabled = false; line('The server did not answer.'); });
  });
  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('[data-src-pause],[data-src-remove],#src-more') : null;
    if (!b) return;
    if (b.id === 'src-more') {
      Array.prototype.forEach.call(document.querySelectorAll('.src-row[data-more]'), function (r) { r.hidden = false; });
      b.hidden = true;
      return;
    }
    b.disabled = true;
    var req = b.hasAttribute('data-src-pause')
      ? post('/insights/sources/pause', { source_id: b.getAttribute('data-src-pause'), paused: b.getAttribute('data-paused') !== '1' })
      : post('/insights/sources/remove', { source_id: b.getAttribute('data-src-remove') });
    req.then(function (r) { if (r && r.ok) location.reload(); else b.disabled = false; });
  });
})();
"""


def render(conn, config: dict | None = None, now: str | None = None) -> str:
    """The page, as text. Read only."""
    model = ns.page_model(conn, now)
    import radar_common as rc
    jobs = rc.job_health(conn, now or rc.now_iso())
    page = insights_page.page("Sources, Insights, MedTech Radar", body_html(model), jobs)
    return page.replace("</style>", CSS + "</style>", 1).replace("</script>", SCRIPT + "</script>", 1)
