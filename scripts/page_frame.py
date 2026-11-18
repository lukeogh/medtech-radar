"""The frame every Radar page sits in: the top bar, its navigation, the colour
tokens, the type scale and the spacing, as plain CSS and a few lines of script.

From the round-2 design (design/round-1 branch, design/jobs/round-2, 14 Nov
2026), dark only, with one approved exception: the CV sheet is light paper
(the --sheet-* tokens and the .sheet-paper wrapper). The Jobs page is built on these tokens throughout.
The other pages keep their bodies as they were and get the top bar over them;
the names their own stylesheet uses (--paper, --ink-1 and the rest) are
pointed at the same dark palette here, so a page reads as one product while
its body waits for its own redesign.

Nothing here reads the database. The status light in the bar is drawn from
the verdicts radar_common.job_health gives, which the caller passes in, read
by the same rules as Home's five lights (job_light below, which Home uses
too), so the bar cannot say All running while Home shows a job late. No
external fonts, scripts or images: the mark is inline SVG and the font stack
ends in the system's own sans.
"""

from __future__ import annotations

import html
from datetime import datetime

# The pages in the bar, in order (owner's order, 17 Nov). Settings is a page
# of its own since 18 Nov, /settings, underlined in the accent.
NAV = (("home", "/", "Home"), ("jobs", "/jobs", "Jobs"),
       ("watchlist", "/watchlist", "Watchlist"), ("insights", "/insights", "Insights"),
       ("cv", "/cv", "CV"), ("settings", "/settings", "Settings"))
# Each page's group, for its menu underline (owner, 17 Nov). Set here once.
NAV_GROUP = {"jobs": "jobs", "watchlist": "apps", "insights": "story", "cv": "cv"}

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
  /* The three groups (owner, 17 Nov): stories (Insights), jobs, and
     applications and follow-ups. One colour each, for a group's icon chip and
     the glow on its cards, and as r g b triplets for those tints. Home uses
     them; other pages may later. */
  --group-story:#e0a94a; --group-jobs:#a79cf0; --group-apps:#5cc3cf;
  --group-story-rgb:224 169 74; --group-jobs-rgb:167 156 240; --group-apps-rgb:92 195 207;
  /* The CV's own colour, rose (owner, 18 Nov): the CV page and the Tailored
     CV on Prepare application. */
  --group-cv:#e88fb4; --group-cv-rgb:232 143 180;
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
  /* The CV sheet's light paper: the one approved exception to dark only
     (design/README.md, prepare-application/final and cv/final, decisions of
     15 Nov). Ink on paper: 15.0, 7.8 and 5.5 to 1; accent 6.3, the focus
     ring 5.7 and the strong rule 3.4 to 1, all on --sheet-paper. */
  --sheet-paper:#f8f6f1; --sheet-surface:#ffffff; --sheet-sunken:#efece5;
  --sheet-ink-1:#1c2125; --sheet-ink-2:#454f56; --sheet-ink-3:#5b666e;
  --sheet-rule:#ddd8ce; --sheet-rule-strong:#7d8388;
  --sheet-accent:#0a6570; --sheet-accent-quiet:#5d8f95; --sheet-accent-wash:#e2f0f1;
  --sheet-negative:#a8281c; --sheet-positive:#1d6b3f; --sheet-caution:#7a5300;
  --sheet-caution-wash:#f6ecd6; --sheet-focus:#9a4d00;
  --sheet-shadow:0 1px 2px rgba(0,0,0,.35),0 8px 24px rgba(0,0,0,.35);
}
@media (prefers-reduced-motion:reduce){:root{--dur:0ms}}
/* One content box for every page (owner, 17 Nov): the window's width up to
   2560px, centred beyond that, 16px in on a phone, 24px from 768px and 32px
   from 1024px. Every
   page's content and the top bar's contents (.appbar-in) read these two, and
   no page sets its own, so the menu never moves when the page changes. */
/* The scroll bar's room is kept on every page, so a page that does not
   scroll (Jobs) puts the box where one that does (Home) puts it. */
html{scrollbar-gutter:stable}
:root{--frame-max:2560px;--frame-pad:16px}
@media (min-width:768px){:root{--frame-pad:24px}}
@media (min-width:1024px){:root{--frame-pad:32px}}
/* The top bar's height, for the pages whose grid or offsets hold a row for it:
   48px on a phone, 64px from 700px (owner, 17 Nov). */
:root{--bar-h:48px}
@media (min-width:700px){:root{--bar-h:64px}}
/* Wrap a CV sheet in .sheet-paper and everything inside it, the editor
   included, reads the paper's colours through the names it already uses. */
.sheet-paper{
  color-scheme:light;background:var(--sheet-paper);color:var(--sheet-ink-1);box-shadow:var(--sheet-shadow);
  --paper:var(--sheet-paper); --surface:var(--sheet-surface); --surface-sunk:var(--sheet-sunken);
  --surface-sunken:var(--sheet-sunken); --canvas:var(--sheet-paper); --row-hover:var(--sheet-sunken);
  --ink-1:var(--sheet-ink-1); --ink-2:var(--sheet-ink-2); --ink-3:var(--sheet-ink-3);
  --text-1:var(--sheet-ink-1); --text-2:var(--sheet-ink-2); --text-3:var(--sheet-ink-3);
  --hairline:var(--sheet-rule); --hairline-strong:var(--sheet-rule-strong);
  --border-subtle:var(--sheet-rule); --border-strong:var(--sheet-rule-strong);
  --accent:var(--sheet-accent); --accent-hover:var(--sheet-accent); --accent-quiet:var(--sheet-accent-quiet);
  --accent-wash:var(--sheet-accent-wash); --accent-tint:var(--sheet-accent-wash); --row-selected:var(--sheet-accent-wash);
  --fail:var(--sheet-negative); --negative:var(--sheet-negative); --settled:var(--sheet-positive);
  --positive:var(--sheet-positive); --caution:var(--sheet-caution); --tint-sand:var(--sheet-caution-wash);
  --caution-tint:var(--sheet-caution-wash); --focus:var(--sheet-focus); --focus-gap:var(--sheet-paper);
}
@media (forced-colors:active){.sheet-paper{box-shadow:none;border:1px solid CanvasText}}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;box-shadow:0 0 0 2px var(--focus-gap,var(--surface))}
@media (forced-colors:active){:focus-visible{outline-color:Highlight}}
.visually-hidden{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);clip-path:inset(50%);white-space:nowrap;border:0}
/* ---- the divider between facts (owner, 18 Nov), one for every page. Each
   fact draws a hairline before it, and the line is pulled left by a hairline
   and its space and clipped, so a fact that starts a wrapped line has its
   hairline cut off and no line ends on one. The 6px of padding keeps a focus
   ring inside the clip. --dv-space is the space either side. ---- */
.dv{display:block;min-width:0;overflow:hidden;padding:6px;margin:-6px}
.dv-in{display:flex;flex-wrap:wrap;align-items:center;row-gap:var(--dv-row,4px);margin-left:calc(-2 * var(--dv-space,10px) - 1px)}
.dv-i{min-width:0}
.dv-i::before{content:"";display:inline-block;width:0;height:var(--dv-h,12px);margin:0 var(--dv-space,10px);border-left:1px solid var(--text-3);vertical-align:-1px}
.skip-link{position:absolute;left:8px;top:-60px;z-index:50;padding:8px 12px;background:var(--surface);color:var(--text-1);border:1px solid var(--border-strong);border-radius:var(--radius-2);font:600 14px/20px var(--font-sans)}
.skip-link:focus{top:8px}
/* The bar's ground and rule run the full width; its contents sit in
   .appbar-in, in the shared content box above. */
.appbar{position:relative;z-index:20;min-width:0;height:var(--bar-h);background:var(--surface);border-bottom:1px solid var(--border-subtle);color:var(--text-1);font:400 14px/20px var(--font-sans);letter-spacing:0}
.appbar-in{box-sizing:border-box;display:flex;align-items:center;gap:24px;width:100%;height:100%;min-width:0;max-width:var(--frame-max,none);margin:0 auto;padding:0 var(--frame-pad,16px)}
.appbar .brand{display:flex;align-items:center;gap:12px;flex:none;color:var(--text-1);text-decoration:none}
.appbar .brand-mark{display:block;width:51px;height:24px;flex:none}
.appbar .brand-name{font-size:14px;line-height:20px;font-weight:600;white-space:nowrap}
.appbar .appbar-page{display:none;font-size:16px;line-height:24px;font-weight:600}
/* The pages at 18px, 32px apart, each link the bar's full height; the page
   you are on in 600 with a 3px rule under it, in the accent as before. */
.appbar .nav{display:flex;align-self:stretch;gap:24px;margin:0 0 0 -4px;padding:0;list-style:none}
.appbar .nav a{display:flex;align-items:center;height:100%;min-height:44px;padding:3px 4px 0;border-bottom:3px solid transparent;color:var(--text-2);font-size:18px;line-height:24px;font-weight:500;text-decoration:none;white-space:nowrap;transition:color var(--dur) ease-out}
.appbar .nav a:hover{color:var(--text-1)}
.appbar .nav a[aria-current="page"]{color:var(--text-1);font-weight:600;border-bottom-color:var(--nav-line,var(--accent))}
/* The page you are on is underlined in its group's colour: Jobs purple, the
   Watchlist teal, Insights amber, the CV rose (NAV_GROUP); Home and Settings
   keep the accent. */
""" + "".join(f'.appbar a[data-group="{k}"]{{--nav-line:var(--group-{g})}}' for k, g in NAV_GROUP.items()) + """
.appbar .nav a:focus-visible{outline-offset:-4px;box-shadow:none}
.appbar .appbar-tools{display:flex;align-items:center;gap:16px;min-width:0;margin-left:auto}
.appbar .radar-status{display:inline-flex;align-items:center;gap:8px;min-height:32px;margin:0;font-size:12px;line-height:16px;font-weight:500;color:var(--text-2);text-decoration:none;white-space:nowrap}
.appbar .radar-status:hover{color:var(--text-1);text-decoration:underline;text-underline-offset:2px}
.appbar .radar-dot{flex:none;width:8px;height:8px;border-radius:50%;background:var(--positive)}
.appbar .radar-status.is-check{color:var(--caution)}
.appbar .radar-status.is-check .radar-dot{background:var(--caution)}
@media (forced-colors:active){.appbar .radar-dot{border:1px solid CanvasText}}
/* Six pages at 16px: between 768 and 1023 the name gives way to them. */
@media (max-width:1023.98px){.appbar .brand-name{display:none}.appbar-in{gap:16px}}
/* From 700px the mark and the name grow with the bar. */
@media (min-width:700px){.appbar .brand-mark{width:68px;height:32px}.appbar .brand-name{font-size:17px;line-height:24px}}
.appbar .bar-btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:32px;min-width:32px;padding:0 8px;border:1px solid transparent;border-radius:var(--radius-2);background:transparent;color:var(--text-2);font:500 14px/20px var(--font-sans);cursor:pointer;text-decoration:none}
.appbar .bar-btn:hover{color:var(--text-1);background:var(--row-hover)}
.appbar .menu-btn{display:none}
.appbar .menu-panel{display:none}
@media (max-width:767.98px){
  .appbar-in{gap:16px}
  .appbar .nav,.appbar .appbar-tools,.appbar .brand-name{display:none}
  .appbar .appbar-page{display:block}
  .appbar .menu-btn{display:inline-flex;width:44px;height:44px;margin:0 -8px 0 auto}
  .appbar .menu-btn:focus-visible{outline-offset:-2px;box-shadow:none}
  .appbar.menu-open .menu-panel{display:block;position:absolute;top:var(--bar-h);left:0;right:0;padding:8px 16px 16px;background:var(--surface);border-bottom:1px solid var(--border-subtle);box-shadow:var(--shadow-overlay)}
  .appbar .menu-panel ul{margin:0;padding:0;list-style:none}
  .appbar .menu-panel a{display:flex;align-items:center;min-height:44px;color:var(--text-1);text-decoration:none;font-size:16px;line-height:24px}
  .appbar .menu-panel a[aria-current="page"]{font-weight:600;text-decoration:underline;text-decoration-color:var(--nav-line,var(--accent));text-decoration-thickness:2px;text-underline-offset:6px}
  .appbar .menu-panel .radar-status{display:flex;min-height:44px;margin-top:8px;font-size:16px;line-height:24px}
  .appbar .menu-panel .radar-dot{width:10px;height:10px}
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
# Radar's five jobs, as Home's strip and the bar's light read them.
JOB_NAMES = ("inbox", "signals", "check", "digest", "push")
# A light that is a fault. Not sending (a dry run) is the box as it is set up,
# and Quiet is the inbox's normal gap between alert emails.
FAULTS = ("Late", "Stale", "Off")
# Only these say Not sending (decision 5, 15 Nov; the words of 17 Nov): the
# digest is built and not sent, the push is written to a file. The signal
# watcher and the advert checker in dry run still read and store, so their
# lights are their state.
DRY_JOBS = ("digest", "push")


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


def _dry(name: str, verdict: dict) -> bool:
    mode = str(verdict.get("mode") or "")
    if name == "digest":
        return mode not in ("", "build", "commit")
    return mode in ("dry-run", "mock")


def job_light(name: str, verdict: dict | None) -> dict:
    """One job's light from its job_health verdict: never is Off, stale is
    Stale, late, failing or partial (a run that finished with failures) is
    Late, except the inbox's late, which is Quiet; and the digest on time
    whose last build was a dry run says Not sending."""
    v = dict(verdict or {"state": "never"})
    state = v.get("state") or "never"
    if state == "never":
        word = "Off"
    elif state == "stale":
        word = "Stale"
    elif state == "late" and name == "inbox":
        word = "Quiet"
    elif state in ("late", "failing", "partial"):
        word = "Late"
    else:
        word = "Not sending" if name in DRY_JOBS and _dry(name, v) else "On time"
    return {"name": name, "word": word, "verdict": v, "state": state}


def radar_running(jobs: dict | None) -> bool:
    """True when every job that reads or sends is on time, by Home's lights:
    no light says Late, Stale or Off. The phone push has no run of its own and
    rides on the signal watcher, so the watcher's light stands for it. A job
    /health is told not to watch never turns the light."""
    jobs = jobs or {}
    for name in JOB_NAMES[:4]:
        verdict = jobs.get(name)
        if verdict is None or verdict.get("watched", True) is False:
            continue
        if job_light(name, verdict)["word"] in FAULTS:
            return False
    return True


def radar_status_html(jobs: dict | None) -> str:
    """The bar's light: All running, or Check Radar, a link to Radar's health.
    Nothing when the caller has no verdicts at all. The two states differ in
    words as well as colour."""
    if not jobs:
        return ""
    ok = radar_running(jobs)
    words = "All running" if ok else "Check Radar"
    return (f'<a class="radar-status{"" if ok else " is-check"}" href="/radar" '
            f'data-state="{"ok" if ok else "check"}"><span class="radar-dot" aria-hidden="true"></span>'
            f'{words}<span class="visually-hidden">, open Radar&#39;s health</span></a>')


def appbar_html(current: str, jobs: dict | None = None, page_name: str = "",
                help_button: bool = False) -> str:
    """The top bar: the mark and name, the pages, the status light and, on
    the Jobs page, the shortcuts button. Under 768px the bar is the mark, the
    page's name and a Menu button that opens the pages and the light."""
    def attrs(key: str) -> str:
        group = f' data-group="{key}"' if key in NAV_GROUP else ""
        return group + (' aria-current="page"' if key == current else "")
    links = "".join(f'<a href="{href}"{attrs(key)}>{label}</a>' for key, href, label in NAV)
    items = "".join(f'<li><a href="{href}"{attrs(key)}>{label}</a></li>'
                    for key, href, label in NAV)
    status = radar_status_html(jobs)
    name = page_name or dict((k, label) for k, _h, label in NAV).get(current, "")
    help_html = ('<button type="button" class="bar-btn help" id="keys-open" '
                 'aria-label="Keyboard shortcuts" aria-keyshortcuts="Shift+?" '
                 f'aria-haspopup="dialog">{ICON_HELP}</button>'
                 if help_button else "")
    return (
        '<a class="skip-link" href="#main">Skip to the page</a>'
        '<header class="appbar"><div class="appbar-in">'
        f'<a class="brand" href="/" aria-label="MedTech Radar, Home">{MARK_SVG}'
        '<span class="brand-name">MedTech Radar</span>'
        f'<span class="appbar-page" aria-hidden="true">{esc(name)}</span></a>'
        f'<nav class="nav" aria-label="Pages">{links}</nav>'
        f'<div class="appbar-tools">{status}{help_html}</div>'
        '<button type="button" class="bar-btn menu-btn" aria-expanded="false" '
        'aria-controls="menu-panel">Menu</button>'
        f'<div class="menu-panel" id="menu-panel"><nav aria-label="Pages, menu">'
        f'<ul>{items}</ul></nav>{status}</div>'
        '</div></header>')


# ---------------------------------------------------- the groups, shared
# Home's group chips, glows and section titles, shared with Jobs (owner,
# 17 Nov). Not in FRAME_CSS: Insights and the Watchlist have a .sec-head
# of their own, and these rules would reach it. A page that draws the
# groups includes GROUPS_CSS and TITLES_CSS.

# The groups' line icons (owner, 17 Nov): drawn on a 20 by 20 grid, no fill,
# in the colour of their chip. Each group has one colour, page_frame's
# --group-story, --group-jobs and --group-apps; Radar's own items and the
# Open actions title take a neutral chip.
ICONS = {
    # The experience bank's three sections: To review, Backed, Parked.
    "review": '<circle cx="9" cy="9" r="5"/><line x1="12.6" y1="12.6" x2="16.5" y2="16.5"/>',
    "backed": '<path d="M10 3.5 15.5 5.5v4.2c0 3.3-2.3 5.8-5.5 6.8-3.2-1-5.5-3.5-5.5-6.8V5.5z"/>',
    "parked": '<rect x="3.5" y="3.5" width="13" height="13" rx="3"/><line x1="8.3" y1="7.3" x2="8.3" y2="12.7"/>'
              '<line x1="11.7" y1="7.3" x2="11.7" y2="12.7"/>',
    "story": '<rect x="3.5" y="3.5" width="13" height="13" rx="2"/><line x1="6.5" y1="7.5" x2="13.5" y2="7.5"/>'
             '<line x1="6.5" y1="10.5" x2="13.5" y2="10.5"/><line x1="6.5" y1="13.5" x2="10.5" y2="13.5"/>',
    "top": '<circle cx="10" cy="10" r="6.5"/><circle cx="10" cy="10" r="2.5"/>',
    "question": '<circle cx="10" cy="10" r="6.5"/><path d="M8 8.3a2 2 0 1 1 2.9 1.8c-.6.3-.9.7-.9 1.3"/>'
                '<circle cx="10" cy="13.7" r="0.5" fill="currentColor"/>',
    # A company: a building, so it never reads as the story's page. Two rows
    # of windows; three ran together at 18px.
    "threads": '<path d="M4.5 16.5V5.5A1.5 1.5 0 0 1 6 4h5a1.5 1.5 0 0 1 1.5 1.5v11"/>'
               '<path d="M12.5 9H14a1.5 1.5 0 0 1 1.5 1.5v6"/><line x1="3" y1="16.5" x2="17" y2="16.5"/>'
               '<line x1="7.2" y1="8" x2="9.8" y2="8"/><line x1="7.2" y1="12" x2="9.8" y2="12"/>',
    "apps": '<rect x="3" y="5" width="14" height="10" rx="2"/><polyline points="3.5,6.5 10,11 16.5,6.5"/>',
    "actions": '<rect x="3.5" y="3.5" width="13" height="13" rx="3"/><polyline points="6.8,10.2 9,12.4 13.4,7.8"/>',
    "jobs": '<rect x="3" y="6.5" width="14" height="9.5" rx="2"/>'
            '<path d="M7.5 6.5V5A1.5 1.5 0 0 1 9 3.5h2A1.5 1.5 0 0 1 12.5 5v1.5"/>',
    "applications": '<path d="M17 3 3 9l5.5 2.5L11 17z"/><line x1="8.5" y1="11.5" x2="17" y2="3"/>',
    # Radar's own item, a job that needs a look: a clock, as the Late mark is.
    "radar": '<circle cx="10" cy="10" r="6.5"/><polyline points="10,6.5 10,10 12.5,11.5"/>',
    # Reasons given for a score: a quote mark.
    "quote": '<path d="M5.5 14.5c0-3.4 1.2-5.6 3.8-7"/><path d="M11.5 14.5c0-3.4 1.2-5.6 3.8-7"/>'
             '<circle cx="6.5" cy="14" r="1.6"/><circle cx="12.5" cy="14" r="1.6"/>',
    # Settings: a cog.
    "settings": '<circle cx="10" cy="10" r="2.6"/><path d="M10 3.5v2M10 14.5v2M3.5 10h2M14.5 10h2'
                'M5.4 5.4l1.4 1.4M13.2 13.2l1.4 1.4M5.4 14.6l1.4-1.4M13.2 6.8l1.4-1.4"/>',
    # A CV: a sheet with its lines.
    "target": '<circle cx="10" cy="10" r="6.5"/><circle cx="10" cy="10" r="3.5"/>'
              '<circle cx="10" cy="10" r="0.8" fill="currentColor"/>',
    "pin": '<path d="M10 17s-5-4.6-5-8.6a5 5 0 0 1 10 0c0 4-5 8.6-5 8.6z"/><circle cx="10" cy="8.4" r="1.8"/>',
    "tag": '<path d="M3.5 4.5a1 1 0 0 1 1-1h5l7 7-6 6-7-7z"/><circle cx="7" cy="7" r="1.2"/>',
    "person": '<circle cx="10" cy="7" r="3"/><path d="M4 16.5c.8-3 3.2-4.5 6-4.5s5.2 1.5 6 4.5"/>',
    "cv": '<path d="M5.5 3.5h6l3 3v10h-9z"/><polyline points="11.5,3.5 11.5,6.5 14.5,6.5"/>'
          '<line x1="7.5" y1="10" x2="12.5" y2="10"/><line x1="7.5" y1="13" x2="12.5" y2="13"/>',
    # A section of details: a list.
    "list": '<line x1="8" y1="6" x2="16" y2="6"/><line x1="8" y1="10" x2="16" y2="10"/>'
            '<line x1="8" y1="14" x2="16" y2="14"/><line x1="4" y1="6" x2="4.5" y2="6"/>'
            '<line x1="4" y1="10" x2="4.5" y2="10"/><line x1="4" y1="14" x2="4.5" y2="14"/>',
}


def divided(parts, cls: str = "") -> str:
    """Facts set apart by the shared divider, each already escaped, empties
    dropped, with a comma between them for a screen reader. The divider never
    starts or ends a wrapped line (FRAME_CSS, .dv)."""
    items = [p for p in parts if p]
    inner = "".join(f'<span class="dv-i">{"" if i == 0 else SR_COMMA}{p}</span>'
                    for i, p in enumerate(items))
    return f'<span class="dv{" " + cls if cls else ""}"><span class="dv-in">{inner}</span></span>'


SR_COMMA = '<span class="visually-hidden">, </span>'


def chip(icon: str, group: str) -> str:
    """A group's icon chip: 32px square, the icon in the group's colour on a
    tint of it. group is story, jobs, apps, cv or neutral."""
    return (f'<span class="gchip g-{group}" aria-hidden="true"><svg viewBox="0 0 20 20" '
            'width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
            f'{ICONS[icon]}</svg></span>')

# The CV group's two pages (owner, 18 Nov): the CV and the experience bank,
# a two-link switch under each one's title row, the page you are on
# underlined in rose. The menu marks CV on both.
CV_SWITCH = (("cv", "/cv", "CV"), ("experience", "/cv/experience", "Experience"))


def cv_switch(current: str) -> str:
    mark = ' aria-current="page"'
    links = "".join(f'<a href="{href}"{mark if key == current else ""}>{label}</a>'
                    for key, href, label in CV_SWITCH)
    return f'<nav class="cv-switch" aria-label="CV pages">{links}</nav>'


SWITCH_CSS = """/* ---- the CV group's switch, CV and Experience ---- */
nav.cv-switch{display:flex;flex-wrap:wrap;gap:4px 24px;margin:0 0 20px}
nav.cv-switch a{display:inline-flex;align-items:center;min-height:36px;padding:0 2px;border-bottom:2px solid transparent;color:var(--text-2);font:500 15px/24px var(--font-sans);text-decoration:none}
nav.cv-switch a:hover{color:var(--text-1)}
nav.cv-switch a[aria-current="page"]{color:var(--text-1);font-weight:600;border-bottom-color:var(--group-cv)}
@media (pointer:coarse){nav.cv-switch a{min-height:44px}}
"""

GROUPS_CSS = """/* ---- the groups (owner, 17 Nov): each has one colour, page_frame's
   --group-*, for its icon chip and a soft glow from the top left of its
   cards. The glow is background only, over the card's own ground. ---- */
.g-story{--g:var(--group-story-rgb);--gc:var(--group-story)}
.g-jobs{--g:var(--group-jobs-rgb);--gc:var(--group-jobs)}
.g-apps{--g:var(--group-apps-rgb);--gc:var(--group-apps)}
.g-cv{--g:var(--group-cv-rgb);--gc:var(--group-cv)}
.g-neutral{--g:232 237 240;--gc:var(--text-1)}
.gchip{display:inline-flex;align-items:center;justify-content:center;flex:none;width:32px;height:32px;border-radius:8px;background:rgb(var(--g) / .16);color:var(--gc)}
.gchip.g-neutral{background:rgb(var(--g) / .10)}
.gchip svg{display:block}
.glow-tile{background-image:radial-gradient(150% 170% at 0% 0%,rgb(var(--g) / .30) 0%,rgb(var(--g) / .11) 45%,rgb(var(--g) / 0) 100%)}
.glow-tile.is-zero{background-image:radial-gradient(150% 170% at 0% 0%,rgb(var(--g) / .16) 0%,rgb(var(--g) / .06) 45%,rgb(var(--g) / 0) 100%)}
.glow-wide{background-image:radial-gradient(115% 160% at 0% 0%,rgb(var(--g) / .20) 0%,rgb(var(--g) / .08) 45%,rgb(var(--g) / 0) 100%)}
.glow-short{background-image:radial-gradient(80% 260% at 0% 0%,rgb(var(--g) / .22) 0%,rgb(var(--g) / .08) 45%,rgb(var(--g) / 0) 100%)}
/* A wide card in a list, glowing from its left end, more strongly when it
   needs you (the Watchlist's companies, owner, 17 Nov). */
.glow-row{background-image:radial-gradient(60% 300% at 0% 0%,rgb(var(--g) / .22) 0%,rgb(var(--g) / .08) 45%,rgb(var(--g) / 0) 100%)}
.glow-row.is-due{background-image:radial-gradient(60% 300% at 0% 0%,rgb(var(--g) / .32) 0%,rgb(var(--g) / .12) 45%,rgb(var(--g) / 0) 100%)}
/* A page's header card (a role on Jobs, a company's record). */
.glow-head{background-image:radial-gradient(900px 300px at 0% 0%,rgb(var(--g) / .22) 0%,rgb(var(--g) / .08) 45%,rgb(var(--g) / 0) 100%)}
/* A section's card: a faint glow of its chip's colour (owner, 17 Nov). */
.glow-faint{background-image:radial-gradient(700px 220px at 0% 0%,rgb(var(--g) / .10) 0%,rgb(var(--g) / .04) 45%,rgb(var(--g) / 0) 100%)}
@media (forced-colors:active){.gchip{border:1px solid CanvasText}}
"""

TITLES_CSS = """/* ---- a section's title, outside its card: the chip, the title, a count
   pill, and a link or the tools at the right; 12px to the card ---- */
.sec-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;min-width:0;margin-bottom:12px}
.sec-title{display:flex;align-items:center;gap:10px;min-width:0;font-size:17px;line-height:24px;font-weight:600;color:var(--text-1)}
.sec-count{display:inline-flex;align-items:center;height:22px;padding:0 8px;border:1px solid #2a3238;border-radius:11px;background:#1a2025;font-size:13px;line-height:18px;font-weight:500;color:var(--text-2);font-variant-numeric:tabular-nums}
.sec-link{margin-left:auto}
.sec-head > .gchip{margin-right:2px}
"""
