"""The frame every Radar page sits in: the top bar, its navigation, the colour
tokens, the type scale and the spacing, as plain CSS and a few lines of script.

From the round-2 design (design/round-1 branch, design/jobs/round-2, 14 Nov
2026), dark only for now. The Jobs page is built on these tokens throughout.
The other pages keep their bodies as they were and get the top bar over them;
the names their own stylesheet uses (--paper, --ink-1 and the rest) are
pointed at the same dark palette here, so a page reads as one product while
its body waits for its own redesign.

Nothing here reads the database. The inbox line in the bar is drawn from the
verdict radar_common.job_health gives, which the caller passes in, the same
verdict /health and Home use, so the bar cannot say on schedule while /health
says stalled. No external fonts, scripts or images: the mark is inline SVG and
the font stack ends in the system's own sans.
"""

from __future__ import annotations

import html
from datetime import datetime

# The pages in the bar, in order. The Watchlist is in the design and stays out
# of the bar until the page exists.
NAV = (("home", "/", "Home"), ("jobs", "/jobs", "Jobs"),
       ("insights", "/insights", "Insights"), ("cv", "/cv", "CV"))

FRAME_CSS = """
:root{
  color-scheme:dark;
  --canvas:#0e1215; --surface:#151a1e; --surface-sunken:#1c2226;
  --row-hover:#192024; --row-selected:#0f3237;
  --text-1:#e6ebee; --text-2:#b3bec6; --text-3:#93a0a9;
  --border-subtle:#262e34; --border-strong:#6f7c86;
  --accent:#5cc3cf; --accent-hover:#82d3dc; --accent-tint:#0f3237; --on-accent:#071114;
  --positive:#74d39b; --positive-tint:#12291c; --caution:#f0c46a; --caution-tint:#2c2311;
  --negative:#ff9b8f; --negative-tint:#33191a; --neutral:#b3bec6; --neutral-tint:#1f262b;
  --focus:#ffd447; --shadow-overlay:0 8px 24px rgba(0,0,0,.5);
  --font-sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --font-mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --radius-2:2px; --radius-4:4px; --dur:120ms;
  /* Type scale: size and line height in pairs, on a 4px grid. */
  --fs-12:12px; --lh-12:16px; --fs-14:14px; --lh-14:20px; --fs-15:15px; --lh-15:24px;
  --fs-16:16px; --lh-16:24px; --fs-18:18px; --lh-18:24px; --fs-20:20px; --lh-20:28px;
  /* Spacing, a 4px grid. */
  --sp-2:2px; --sp-4:4px; --sp-8:8px; --sp-12:12px; --sp-16:16px; --sp-20:20px;
  --sp-24:24px; --sp-32:32px; --sp-48:48px;
  /* The older pages' own names, pointed at the same palette. */
  --paper:#0e1215; --surface-sunk:#1c2226;
  --ink-1:#e6ebee; --ink-2:#b3bec6; --ink-3:#93a0a9;
  --hairline:#262e34; --hairline-strong:#3d474e;
  --accent-quiet:#2f6e76; --accent-wash:#0f3237;
  --fail:#ff9b8f; --settled:#74d39b;
  --tint-blue:#131b20; --tint-sand:#1a1a17; --tint-sage:#141b17;
  --tint-clay:#1c1716; --tint-stone:#161b1f;
  --panel-rule:rgba(230,235,238,.10);
}
@media (prefers-reduced-motion:reduce){:root{--dur:0ms}}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;box-shadow:0 0 0 2px var(--focus-gap,var(--surface))}
@media (forced-colors:active){:focus-visible{outline-color:Highlight}}
.visually-hidden{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);clip-path:inset(50%);white-space:nowrap;border:0}
.skip-link{position:absolute;left:8px;top:-60px;z-index:50;padding:8px 12px;background:var(--surface);color:var(--text-1);border:1px solid var(--border-strong);border-radius:var(--radius-2);font:600 14px/20px var(--font-sans)}
.skip-link:focus{top:8px}
.appbar{position:relative;z-index:20;display:flex;align-items:center;gap:24px;min-width:0;height:48px;padding:0 16px;background:var(--surface);border-bottom:1px solid var(--border-subtle);color:var(--text-1);font:400 14px/20px var(--font-sans);letter-spacing:0}
.appbar .brand{display:flex;align-items:center;gap:12px;flex:none;color:var(--text-1);text-decoration:none}
.appbar .brand-mark{display:block;width:51px;height:24px;flex:none}
.appbar .brand-name{font-size:14px;line-height:20px;font-weight:600;white-space:nowrap}
.appbar .appbar-page{display:none;font-size:16px;line-height:24px;font-weight:600}
.appbar .nav{display:flex;align-self:stretch;gap:12px;margin:0 0 0 -4px;padding:0;list-style:none}
.appbar .nav a{display:flex;align-items:center;height:100%;padding:2px 6px 0;border-bottom:2px solid transparent;color:var(--text-2);text-decoration:none;white-space:nowrap;transition:color var(--dur) ease-out}
.appbar .nav a:hover{color:var(--text-1)}
.appbar .nav a[aria-current="page"]{color:var(--text-1);border-bottom-color:var(--accent)}
.appbar .nav a:focus-visible{outline-offset:-4px;box-shadow:none}
.appbar .appbar-tools{display:flex;align-items:center;gap:16px;min-width:0;margin-left:auto}
.appbar .inbox-status{display:inline-flex;align-items:center;gap:6px;margin:0;font-size:12px;line-height:16px;color:var(--text-3);white-space:nowrap}
.appbar .inbox-status svg{flex:none}
.appbar .inbox-status.is-warn{color:var(--caution)}
.appbar .inbox-status.is-bad{color:var(--negative)}
.appbar .bar-btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:32px;min-width:32px;padding:0 8px;border:1px solid transparent;border-radius:var(--radius-2);background:transparent;color:var(--text-2);font:500 14px/20px var(--font-sans);cursor:pointer;text-decoration:none}
.appbar .bar-btn:hover{color:var(--text-1);background:var(--row-hover)}
.appbar .menu-btn{display:none}
.appbar .menu-panel{display:none}
@media (max-width:767.98px){
  .appbar{gap:16px}
  .appbar .nav,.appbar .appbar-tools,.appbar .brand-name{display:none}
  .appbar .appbar-page{display:block}
  .appbar .menu-btn{display:inline-flex;width:44px;height:44px;margin:0 -8px 0 auto}
  .appbar .menu-btn:focus-visible{outline-offset:-2px;box-shadow:none}
  .appbar.menu-open .menu-panel{display:block;position:absolute;top:48px;left:0;right:0;padding:8px 16px 16px;background:var(--surface);border-bottom:1px solid var(--border-subtle);box-shadow:var(--shadow-overlay)}
  .appbar .menu-panel ul{margin:0;padding:0;list-style:none}
  .appbar .menu-panel a{display:flex;align-items:center;min-height:44px;color:var(--text-1);text-decoration:none;font-size:16px;line-height:24px}
  .appbar .menu-panel a[aria-current="page"]{font-weight:600;text-decoration:underline;text-decoration-color:var(--accent);text-decoration-thickness:2px;text-underline-offset:6px}
  .appbar .menu-panel .inbox-status{display:flex;margin-top:8px}
}
@media (pointer:coarse){.appbar .bar-btn{min-width:44px;height:44px}}
/* A page whose body is still the older one: its own heading row keeps what it
   says and loses the controls the bar now holds. */
.page > .masthead{padding-top:0}
"""

FRAME_SCRIPT = """
(function () {
  var bar = document.querySelector('.appbar');
  if (!bar) return;
  var menu = bar.querySelector('.menu-btn');
  function setMenu(open) {
    bar.classList.toggle('menu-open', open);
    if (menu) menu.setAttribute('aria-expanded', String(open));
  }
  if (menu) menu.addEventListener('click', function () {
    setMenu(menu.getAttribute('aria-expanded') !== 'true');
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && bar.classList.contains('menu-open')) {
      setMenu(false);
      if (menu) menu.focus();
    }
  });
  document.addEventListener('click', function (e) {
    if (bar.classList.contains('menu-open') && !bar.contains(e.target)) setMenu(false);
  });
  // The inbox time is written in UTC by the server and shown in the
  // reader's own time here, today's as a time and older ones with the day.
  bar.querySelectorAll('time[data-local]').forEach(function (t) {
    var when = new Date(t.getAttribute('datetime'));
    if (isNaN(when)) return;
    var now = new Date();
    var hm = ('0' + when.getHours()).slice(-2) + ':' + ('0' + when.getMinutes()).slice(-2);
    var sameDay = when.toDateString() === now.toDateString();
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    t.textContent = sameDay ? hm : when.getDate() + ' ' + months[when.getMonth()] + ' ' + hm;
  });
})();
"""

# The radar mark, as the older pages draw it, at the bar's size. Inline, so
# nothing is fetched, and in the text tokens, so it sits in the dark palette.
MARK_SVG = ('<svg class="brand-mark" viewBox="0 0 36 17" width="51" height="24" '
            'fill="none" aria-hidden="true" focusable="false">'
            '<path d="M4 15a14 14 0 0 1 28 0" stroke="var(--text-3)" stroke-width="1.2"/>'
            '<path d="M8.5 15a9.5 9.5 0 0 1 19 0" stroke="var(--text-2)" stroke-width="1.2"/>'
            '<path d="M13 15a5 5 0 0 1 10 0Z" fill="var(--text-1)"/>'
            '<path d="M0 15h36" stroke="var(--text-1)" stroke-width="1.5"/></svg>')

ICON_HELP = ('<svg viewBox="0 0 20 20" width="20" height="20" fill="none" '
             'aria-hidden="true" focusable="false"><circle cx="10" cy="10" r="7.5" '
             'stroke="currentColor" stroke-width="1.4"/><path d="M7.9 7.9a2.1 2.1 0 1 1 '
             '3 1.9c-.6.3-.9.8-.9 1.4v.5" stroke="currentColor" stroke-width="1.4" '
             'stroke-linecap="round"/><circle cx="10" cy="13.9" r=".9" '
             'fill="currentColor"/></svg>')
ICON_WARN = ('<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" '
             'focusable="false"><circle cx="8" cy="8" r="5.6" fill="none" '
             'stroke="currentColor" stroke-width="1.6"/><path d="M8 2.4a5.6 5.6 0 0 0 '
             '0 11.2z" fill="currentColor"/></svg>')
ICON_BAD = ('<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" '
            'focusable="false"><path d="M4.2 4.2l7.6 7.6m0-7.6l-7.6 7.6" '
            'stroke="currentColor" stroke-width="1.8"/></svg>')

# The inbox verdict in the bar's words. The states are job_health's.
INBOX_WORDS = {"ok": "on schedule", "partial": "ran, with failures",
               "late": "running late", "failing": "failing",
               "stale": "stalled", "never": "never run"}


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _when(iso) -> tuple[str, str] | None:
    """(the datetime attribute, the server's own words for it), or None."""
    try:
        moment = datetime.strptime(str(iso)[:16], "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return None
    return (moment.strftime("%Y-%m-%dT%H:%M:00Z"),
            f"{moment.day} {moment:%b} {moment:%H:%M} UTC")


def inbox_status_html(jobs: dict | None) -> str:
    """One line on the inbox from its job_health verdict, or nothing.

    "Inbox on schedule · ran 09:02". A state that is not on schedule carries
    an icon as well as its words and a colour, so it never rests on colour.
    The time is UTC as the server writes it, turned into the reader's own
    time by FRAME_SCRIPT.
    """
    verdict = (jobs or {}).get("inbox")
    if not verdict:
        return ""
    state = verdict.get("state") or "never"
    words = INBOX_WORDS.get(state, state)
    cls, icon = "", ""
    if state in ("late", "partial", "failing"):
        cls, icon = " is-warn", ICON_WARN
    elif state in ("stale", "never"):
        cls, icon = " is-bad", ICON_BAD
    ran = _when(verdict.get("last_run"))
    tail = ""
    if ran:
        tail = (f' · ran <time datetime="{ran[0]}" data-local>'
                f'{esc(ran[1])}</time>')
    return (f'<p class="inbox-status{cls}" data-state="{esc(state)}">{icon}'
            f'<span>Inbox {esc(words)}{tail}</span></p>')


def appbar_html(current: str, jobs: dict | None = None, page_name: str = "",
                help_button: bool = False) -> str:
    """The top bar: the mark and name, the pages, the inbox line and, on the
    Jobs page, the shortcuts button. Under 768px the bar is the mark, the
    page's name and a Menu button that opens the pages and the inbox line."""
    links = "".join(
        f'<a href="{href}"{" aria-current=" + chr(34) + "page" + chr(34) if key == current else ""}>'
        f'{label}</a>' for key, href, label in NAV)
    items = "".join(
        f'<li><a href="{href}"{" aria-current=" + chr(34) + "page" + chr(34) if key == current else ""}>'
        f'{label}</a></li>' for key, href, label in NAV)
    status = inbox_status_html(jobs)
    name = page_name or dict((k, label) for k, _h, label in NAV).get(current, "")
    help_html = ('<button type="button" class="bar-btn help" id="keys-open" '
                 'aria-label="Keyboard shortcuts" aria-keyshortcuts="Shift+?" '
                 f'aria-haspopup="dialog">{ICON_HELP}</button>'
                 if help_button else "")
    return (
        '<a class="skip-link" href="#main">Skip to the page</a>'
        '<header class="appbar">'
        f'<a class="brand" href="/" aria-label="MedTech Radar, Home">{MARK_SVG}'
        '<span class="brand-name">MedTech Radar</span>'
        f'<span class="appbar-page" aria-hidden="true">{esc(name)}</span></a>'
        f'<nav class="nav" aria-label="Pages">{links}</nav>'
        f'<div class="appbar-tools">{status}{help_html}</div>'
        '<button type="button" class="bar-btn menu-btn" aria-expanded="false" '
        'aria-controls="menu-panel">Menu</button>'
        f'<div class="menu-panel" id="menu-panel"><nav aria-label="Pages, menu">'
        f'<ul>{items}</ul></nav>{status}</div>'
        '</header>')
