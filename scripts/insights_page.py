"""The Insights page as a news front page, variation B (Broadsheet).

From the design on the design/round-1 branch, design/insights/news/b and its
notes, with the decisions of 15 Nov 2026 recorded in design/README.md. On the
shared frame in page_frame.py, dark only.

The page is the last seven days of what the signal watcher found, laid out
like a newspaper: Top stories (the lead and four smaller), Most relevant this
week (1 to 10), three sections filed by story type, everything under the
floor folded under More stories, and the stories hidden this week folded
under that. The page ranks the week by relevance whatever the numbers: 40 is
the mark of a buying window, which the digest and the phone pushes use, and
most weeks nothing reaches it, so the news page does not wait for it. A story is one event: the reports that share an event_key (see
radar_common.event_key_for), led by the highest-scoring one.

Three pages, all read only:
  /insights                    the front page
  /insights/story/<id>         one story: summary, why it matters, every
                               report, the company, its roles, drafts and a
                               link to log a contact on the Watchlist (<id> is
                               any report's signal id)
  /insights/section/<key>      every open story in one section

The page's script writes through the guarded POST routes serve_dashboard.py
adds for it: /story/hide, /story/show, /story/split, /watchlist/add (with
via insights and the story's signal id, so the Watchlist can say where the
company came from) and /watchlist/remove (Undo, with undo_add, which puts the
company back as it was). Contacts are logged on the Watchlist, at
/watchlist/<id>, which is also where On your Watchlist leads. Nothing
here fetches, scores or writes.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import radar_common
import build_dashboard as bd
import page_frame

esc = bd.esc

FLOOR = 10             # under this a story is folded under More stories
BUYING = 40            # the mark of a buying window, as the scorer's bands put it
TOP_COUNT = 5
RANKED = 10
OTHER_COUNT = 5        # Other stories this week
SECTION_SHOWN = 3      # rows shown in a section before Show the other N
WEEK_DAYS = 7          # a rolling week, as "fresh" has always worked
EARLIER_SHOWN = 3
SECTIONS = radar_common.STORY_SECTIONS
SECTION_OF = {t: key for key, _n, types in SECTIONS for t in types}
SECTION_NAME = {key: name for key, name, _t in SECTIONS}
CODE_TYPES = {"job-advert": "buying-window", "qa-tripwire": "first-hire"}
TYPE_WORDS = {"buying-window": "Buying window", "first-hire": "First hire", "funding": "Funding round",
              "spin-off": "Spin-off launch", "accelerator": "Accelerator entry",
              "regulation": "Regulation", "market": "Market news", "other": "Other news"}
CHANNELS = (("comment", "Comment"), ("connection-note", "Connection note"),
            ("engagement", "Engagement"), ("artefact", "Article sent"), ("other", "Other"))
WATCHER_WORDS = {
    "stale": "The watcher has stopped",
    "failing": "The watcher's runs are failing",
    "late": "The watcher is running late",
    "never": "The watcher has not run yet",
}
FONT_FILES = ("IBMPlexSerif-Regular.woff2", "IBMPlexSerif-Medium.woff2", "IBMPlexSerif-SemiBold.woff2")


# ================================================================ model

def _parse(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _day(iso) -> str:
    when = _parse(iso)
    return f"{when.day} {when:%b}" if when else ""


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def source_names() -> dict:
    names = {s.get("id"): s.get("name") or s.get("id") for s in bd.load_watchlist()}
    names["job-advert"] = "a job advert"
    names["qa-tripwire"] = "a job advert"
    return names


def source_name(names: dict, source_id) -> str:
    """A source's name from the watchlist, or its id read as words."""
    if source_id in names:
        return names[source_id]
    words = str(source_id or "a source").replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else "A source"


def _story(reports: list, names: dict, companies: dict) -> dict:
    """One event from its reports. The highest-scoring report leads."""
    ranked = sorted(reports, key=lambda r: (-(r["relevance"] or 0), r["first_seen"] or "", r["id"]))
    lead = ranked[0]
    kind = lead.get("story_type") or CODE_TYPES.get(lead.get("source_id")) or "other"
    company = companies.get(lead.get("company_id")) if lead.get("company_id") else None
    known = bool(company) and not radar_common.is_placeholder_company(lead.get("company"))
    found = min(r["first_seen"] or "" for r in reports)
    return {
        "id": lead["id"], "key": lead.get("event_key") or f"e{lead['id']}",
        "headline": radar_common.clean_headline(lead.get("headline") or "Untitled"),
        "summary": lead.get("card_summary") or lead.get("why") or "",
        "why": lead.get("why") or "",
        "company": (lead.get("company") or "Unknown company") if known else TYPE_WORDS.get(kind, "Other news"),
        "company_id": lead.get("company_id") if known else None,
        "company_rec": company if known else None,
        "watched": bool(company and company.get("watched_at")) if known else False,
        "type": kind, "section": SECTION_OF.get(kind, "market"),
        "relevance": lead["relevance"] or 0,
        "found": found, "source": source_name(names, lead.get("source_id")),
        "url": bd.safe_url(lead.get("source_url")),
        "reports": sorted(reports, key=lambda r: (r["first_seen"] or "", r["id"])),
        "count": len(reports),
        "hidden": all(r.get("acknowledged_at") for r in reports),
        "drafts": next((r for r in ranked if r.get("draft_comment") or r.get("draft_note")), None),
        "move": (lead.get("playbook_step") or "").strip(),
    }


def build_model(conn, config: dict | None = None, now: str | None = None) -> dict:
    """Everything the three pages show, from one read of the database."""
    moment = _parse(now) if now else datetime.now(timezone.utc)
    week_from = _iso(moment - timedelta(days=WEEK_DAYS))
    names = source_names()
    have = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    watched_col = "watched_at" if "watched_at" in have else "NULL AS watched_at"
    companies = {r["id"]: dict(r) for r in conn.execute(
        f"SELECT id, display_name, what_they_build, software_content, stage, city, country,"
        f" region, people, {watched_col} FROM companies")}
    rows = [dict(r) for r in conn.execute("SELECT * FROM signals WHERE relevance IS NOT NULL")]
    unscored = conn.execute("SELECT COUNT(*) FROM signals WHERE relevance IS NULL").fetchone()[0]
    events: dict = {}
    for r in rows:
        events.setdefault(r.get("event_key") or f"e{r['id']}", []).append(r)
    # Hiring is the Jobs page's, so a story led by a hiring report is not shown
    # anywhere on Insights: not in a list, not in the hidden fold, not as a page.
    stories = [s for s in (_story(reps, names, companies) for reps in events.values())
               if s["type"] not in radar_common.HIRING_TYPES]
    for s in stories:
        s["this_week"] = s["found"] >= week_from or any(
            (r["first_seen"] or "") >= week_from for r in s["reports"])
    order = lambda s: (-s["relevance"], "".join(chr(0x7e - ord(c)) for c in s["found"]))  # noqa: E731
    stories.sort(key=order)
    shown = [s for s in stories if not s["hidden"]]
    week = [s for s in shown if s["this_week"]]
    earlier = [s for s in shown if not s["this_week"]]
    above = [s for s in week if s["relevance"] >= FLOOR]
    below = [s for s in week if s["relevance"] < FLOOR]
    # Top stories: every open story at the mark of a buying window, whatever
    # its age (owner, 18 Nov). Other stories this week: the week's five most
    # relevant that are not in Top stories.
    top = [s for s in shown if s["relevance"] >= BUYING]
    in_top = {s["key"] for s in top}
    other = [s for s in week if s["key"] not in in_top][:OTHER_COUNT]
    sections = []
    for key, name, _types in SECTIONS:
        sections.append({
            "key": key, "name": name,
            "stories": [s for s in above if s["section"] == key and s["key"] not in in_top],
            "in_top": [s for s in top if s["section"] == key],
            "earlier": [s for s in earlier if s["section"] == key and s["relevance"] >= FLOOR
                        and s["key"] not in in_top],
        })
    for sec in sections:
        sec["count"] = len(sec["stories"])
    jobs = radar_common.job_health(conn, now)
    watcher = jobs.get("signals") or {}
    # Counted as /insights/sources counts them: built-in and added, none paused.
    import news_sources
    counted = news_sources.page_model(conn, _iso(moment))
    answering = counted["answering"]
    return {
        "now": _iso(moment), "stories": stories, "week": week, "earlier": earlier,
        "above": above, "below": below, "top": top, "other": other, "sections": sections,
        "ranked": above[:RANKED], "buying": sum(1 for s in week if s["relevance"] >= BUYING), "hidden": [s for s in stories if s["hidden"] and s["this_week"]],
        "watcher": watcher, "jobs": jobs, "sources": counted["total"], "answering": answering,
        "unscored": unscored, "companies": companies,
    }


def find_story(model: dict, signal_id: int) -> dict | None:
    for s in model["stories"]:
        if any(r["id"] == signal_id for r in s["reports"]):
            return s
    return None


# ================================================================ CSS

FONT_CSS = "".join(
    f'@font-face{{font-family:"IBM Plex Serif";src:url(/static/fonts/{name}) format("woff2");'
    f'font-weight:{weight};font-style:normal;font-display:swap}}'
    for name, weight in zip(FONT_FILES, (400, 500, 600)))

PAGE_CSS = """
/* The serif is retired here (owner, 18 Nov): the story and section pages
   read the shared sans through the old name. */
:root{--font-serif:var(--font-sans);--gutter:var(--frame-pad)}
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--canvas)}
body{margin:0;background:var(--canvas);color:var(--text-1);font:400 15px/24px var(--font-sans);
  -webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
a{color:var(--accent)}
a:hover{color:var(--accent-hover)}
h1,h2,h3,h4,p,ol,ul,dl,dd{margin:0}
ol,ul{padding:0;list-style:none}
button,select,textarea{font:inherit}
.page{margin:0 auto;padding:0 var(--frame-pad) 64px;max-width:var(--frame-max)}
/* Plain links: the text colour, underlined. */
a.plain{color:var(--text-1);text-decoration:underline;text-decoration-color:#5b6770;text-decoration-thickness:1px;
  text-underline-offset:3px}
a.plain:hover{color:var(--text-1);text-decoration-color:var(--text-1)}
.card{min-width:0;border:1px solid var(--border-subtle);border-radius:14px;background-color:var(--surface)}
.btn{display:inline-flex;align-items:center;gap:6px;height:36px;padding:0 14px;border:1px solid var(--border-strong);
  border-radius:8px;background:var(--surface);color:var(--text-1);font:500 14px/20px var(--font-sans);
  cursor:pointer;text-decoration:none;white-space:nowrap}
.btn:hover{background:var(--row-hover);color:var(--text-1)}
.btn.quiet{border-color:transparent;background:transparent;color:var(--text-2);padding:0 8px}
.btn.quiet:hover{color:var(--text-1)}
.btn[disabled],.btn[aria-disabled="true"]{opacity:.55;cursor:default}
/* On your Watchlist: a teal pill, companies' colour, linking to the record. */
.wpill,.on-list{display:inline-flex;align-items:center;height:26px;padding:0 10px;border:1px solid rgba(92,195,207,.45);
  border-radius:13px;background:rgba(92,195,207,.14);color:#9fe0e8;font:500 13px/20px var(--font-sans);
  text-decoration:none;white-space:nowrap}
.wpill:hover,.on-list:hover{color:var(--text-1);border-color:var(--group-apps)}
@media (pointer:coarse){.btn{height:44px}.wpill,.on-list{height:32px}}
/* ---- the front page ---- */
.ins-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;padding:24px 0 12px}
.ins-title{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;min-width:0}
.ins-title h1{font:600 24px/32px var(--font-sans)}
.ins-health{margin-left:auto;font-size:14px;line-height:20px}
.ins-toc{display:flex;flex-wrap:wrap;align-items:center;row-gap:8px;padding:0 0 8px;font-size:14px;line-height:20px}
.ins-toc a{white-space:nowrap}
.toc-n{margin-left:6px;color:var(--text-2);font-variant-numeric:tabular-nums}
.ins-notice{margin-top:16px;padding:16px;border:1px solid var(--caution);border-radius:14px;background:var(--caution-tint)}
.ins-notice.is-bad{border-color:var(--negative);background:var(--negative-tint)}
.ins-notice h2{font:600 16px/24px var(--font-sans)}
.ins-notice p{margin-top:4px;color:var(--text-2);font-size:14px;line-height:20px}
.ins-sec{margin-top:32px;min-width:0;scroll-margin-top:16px}
.ins-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,520px),1fr));gap:0 32px;align-items:start}
.tlist{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,440px),1fr));gap:16px}
.tlist>li{display:flex;min-width:0}
.tlist>li:first-child{grid-column:1 / -1}
.tcard{display:flex;flex-direction:column;gap:8px;width:100%;padding:20px}
.tline,.srow-m{display:flex;flex-wrap:wrap;align-items:center;row-gap:4px;color:var(--text-2);font-size:14px;
  line-height:20px;overflow-wrap:anywhere}
.th{font:600 16px/24px var(--font-sans);overflow-wrap:anywhere}
.tcard.is-first .th{font-size:20px;line-height:28px}
.th a,.srow-h{color:var(--text-1);text-decoration:none}
.th a:hover,.srow-h:hover{text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:4px}
.treason{color:var(--text-2);font-size:15px;line-height:24px;max-width:80ch}
.tmove{max-width:80ch;padding:12px 14px;background:#101417;border:1px solid #1f262b;border-radius:10px}
.tmove-l{color:var(--group-story);font-size:13px;line-height:20px;font-weight:600}
.tmove-t{color:var(--text-1);font-size:14px;line-height:20px}
.story-actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin-top:auto;padding-top:4px}
.scard{padding:4px 20px}
.rows>li{padding:12px 0;border-top:1px solid var(--border-subtle)}
.scard>.rows:first-child>li:first-child,.scard>div:first-child>.rows>li:first-child{border-top:0}
.srow-h{display:block;font:500 15px/22px var(--font-sans);overflow-wrap:anywhere}
.srow-m{margin-top:4px;column-gap:12px}
.srow-src{display:inline-flex;flex-wrap:wrap;align-items:center;row-gap:4px;min-width:0}
.none{padding:12px 0;color:var(--text-2);font-size:14px;line-height:20px}
.more-btn{display:flex;align-items:center;min-height:44px;width:100%;margin:0;padding:0;border:0;
  border-top:1px solid var(--border-subtle);background:none;color:var(--text-1);font:500 14px/20px var(--font-sans);
  text-decoration:underline;text-decoration-color:#5b6770;text-underline-offset:3px;cursor:pointer}
.scard>.more-btn:first-child,.scard>div[hidden]:first-child+.more-btn{border-top:0}
.more-btn:hover{text-decoration-color:var(--text-1)}
.hidden-slot{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;padding:16px;border:1px dashed var(--border-strong);
  border-radius:14px;color:var(--text-2);font-size:14px;line-height:20px;width:100%}
.hidden-slot .hl{color:var(--text-1)}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:40;display:flex;align-items:center;
  gap:16px;max-width:calc(100% - 32px);padding:12px 16px;border:1px solid var(--border-strong);
  border-radius:var(--radius-4);background:var(--surface-sunken);box-shadow:var(--shadow-overlay);
  color:var(--text-1);font-size:14px;line-height:20px}
.toast.is-bad{border-color:var(--negative)}
.toast:not(.show){width:1px;height:1px;padding:0;border:0;overflow:hidden;clip-path:inset(50%);box-shadow:none}
.toast:not(.show) .btn{display:none}
.watch-done{display:inline-flex;flex-wrap:wrap;align-items:center;gap:4px 8px}
.shown{color:var(--text-2);font-size:13px;line-height:20px}
@media (max-width:767.98px){.ins-health{margin-left:0;flex-basis:100%}.tcard{padding:16px}.scard{padding:4px 16px}}
/* ---- the story and section pages (unchanged in layout) ---- */
.story-line{color:var(--text-2);font-size:14px;line-height:20px;overflow-wrap:anywhere;display:flex;flex-wrap:wrap;align-items:center}
.empty-line{color:var(--text-3);font-size:14px;line-height:20px;padding:4px 0 8px}
.quiet-sub{margin:24px 0 12px;font:600 16px/24px var(--font-sans);color:var(--text-2)}
.plain-list li{padding:8px 0;border-top:1px solid var(--border-subtle)}
.plain-list li a{color:var(--text-1);text-decoration:none;font:500 15px/22px var(--font-sans)}
.plain-list li a:hover{text-decoration:underline;text-underline-offset:3px}
.plain-list .meta{display:flex;flex-wrap:wrap;align-items:center;color:var(--text-3);font-size:13px;line-height:20px}
/* The story page */
.crumbs{padding:16px 0 0;font-size:14px;line-height:20px;color:var(--text-3)}
.crumbs ol{display:flex;flex-wrap:wrap;gap:4px 8px}
.crumbs li+li::before{content:"/";margin-right:8px;color:var(--text-3)}
.crumbs [aria-current]{color:var(--text-2)}
.story-page{display:grid;grid-template-columns:minmax(0,1fr);gap:40px;max-width:820px;margin:8px auto 0}
.story-page>.crumbs{grid-column:1 / -1}
@media (min-width:1440px){.story-page{grid-template-columns:minmax(0,820px) 360px;max-width:none;justify-content:center;gap:24px 64px}
  .story-page .company{margin-top:32px;align-self:start}}
.article{min-width:0}
.article .story-line{font-size:14px;margin-top:24px}
.article h1{margin-top:8px;font:500 44px/52px var(--font-serif);letter-spacing:-.01em}
.article .facts-line{margin-top:12px;color:var(--text-3);font-size:14px;line-height:20px}
.article .facts-line .rel{color:var(--text-1);font-weight:600}
.article .story-actions{margin-top:16px;padding-top:0}
.article h2{margin-top:32px;font:600 18px/24px var(--font-sans)}
.article .s-body{margin-top:8px;font:400 18px/30px var(--font-serif);color:var(--text-1)}
.article .src-note{margin-top:8px;color:var(--text-3);font-size:13px;line-height:20px}
.article .why{margin-top:8px;font-size:17px;line-height:28px;color:var(--text-1)}
.reports li{padding:12px 0;border-top:1px solid var(--border-subtle)}
.reports li:last-child{border-bottom:1px solid var(--border-subtle)}
.reports .src{font-weight:600;color:var(--text-1);font-size:15px}
.reports .as{display:block;margin-top:2px;font:400 16px/24px var(--font-serif);color:var(--text-2)}
.reports .meta{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px;margin-top:4px;color:var(--text-3);font-size:13px;line-height:20px}
.drafts{margin-top:12px}
.drafts .draft{margin-top:12px}
.drafts h3{font:600 15px/20px var(--font-sans)}
.drafts p.text{margin-top:4px;padding:12px;border:1px solid var(--border-subtle);border-radius:var(--radius-2);
  background:var(--surface);color:var(--text-1);white-space:pre-wrap}
.drafts .btn{margin-top:8px}
/* Running text stays about 80 characters a line on a wide window. */
.why,.drafts p.text{max-width:80ch}
.log-line{margin-top:24px;max-width:72ch;color:var(--text-2);font-size:14px;line-height:20px}
.log-line a{color:var(--accent);text-underline-offset:2px}
.watch-done{display:inline-flex;flex-wrap:wrap;align-items:center;gap:4px 8px}
.shown{color:var(--text-2);font-size:13px;line-height:20px}
.plain-list a,.plain-list .meta,.article h1,.hidden-slot .hl,.company dd,.company h2,.reports .as,.reports .src{overflow-wrap:anywhere}
.company{min-width:0}
.company h2{font:600 18px/24px var(--font-sans);padding-top:12px;border-top:3px solid var(--text-1)}
.company dl{display:grid;grid-template-columns:96px 1fr;gap:8px 12px;margin-top:12px;font-size:14px;line-height:20px}
.company dt{color:var(--text-3)}
.company dd{color:var(--text-1)}
.company dd.nk{color:var(--text-3)}
.company h3{margin-top:24px;font:600 15px/20px var(--font-sans)}
.company ul li{padding:8px 0;border-top:1px solid var(--border-subtle);font-size:14px;line-height:20px}
.company .meta{display:flex;flex-wrap:wrap;align-items:center;color:var(--text-3);font-size:13px;line-height:20px}
.company .rec{display:block;margin-top:12px;font-size:14px;line-height:20px}
.back{display:inline-block;margin-top:32px;font-size:14px}
.section-page{max-width:900px;margin:0 auto}
.section-page h1{margin-top:24px;font:500 40px/48px var(--font-serif)}
.section-page .plain-list{margin-top:16px}
@media (max-width:767.98px){
  .article h1{font-size:28px;line-height:36px}
  .article .s-body{font-size:17px;line-height:28px}
}
"""

ICON_CHECK = ('<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">'
              '<path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>')
ICON_OUT = ('<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false">'
            '<path d="M9 3h4v4M13 3L7.5 8.5M11 9.5V13H3V5h3.5" fill="none" stroke="currentColor" '
            'stroke-width="1.5"/></svg>')
ICON_WARN = ('<svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true" focusable="false">'
             '<path d="M10 2.5l8 14H2z" fill="none" stroke="currentColor" stroke-width="1.6" '
             'stroke-linejoin="round"/><path d="M10 8v4" stroke="currentColor" stroke-width="1.8"/>'
             '<circle cx="10" cy="14.4" r="1" fill="currentColor"/></svg>')
ICON_PREV = ('<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false">'
             '<path d="M10 3L5 8l5 5" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>')
ICON_NEXT = ('<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false">'
             '<path d="M6 3l5 5-5 5" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>')
ICON_DOWN = ('<svg class="chev" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false">'
             '<path d="M3.5 6l4.5 4.5L12.5 6" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>')


# ================================================================ parts

def story_href(s: dict) -> str:
    return f"/insights/story/{s['id']}"




def _join(parts: list) -> str:
    """Items on one line with the shared divider between them."""
    return page_frame.divided(parts)


def company_link(s: dict) -> str:
    """The company's name, linked to its record when it has one."""
    if s["company_id"]:
        return f'<a class="plain" href="/watchlist/{esc(s["company_id"])}">{esc(s["company"])}</a>'
    return f'<span>{esc(s["company"])}</span>'


def sources_words(s: dict) -> str:
    more = ""
    if s["count"] > 1:
        n = s["count"] - 1
        more = f' and {n} more source{"s" if n > 1 else ""}'
    return f'{esc(s["source"][:1].upper() + s["source"][1:])}{more}'


def headline_link(s: dict, cls: str = "") -> str:
    """The headline, straight to the article; the story page when there is no link."""
    c = f' class="{cls}"' if cls else ""
    if s["url"]:
        return (f'<a{c} href="{esc(s["url"])}" target="_blank" rel="noopener">{esc(s["headline"])}'
                f'<span class="visually-hidden"> (opens in a new tab)</span></a>')
    return f'<a{c} href="{story_href(s)}">{esc(s["headline"])}</a>'


def watch_pill(s: dict) -> str:
    return (f'<a class="wpill" href="/watchlist/{esc(s["company_id"])}">On your Watchlist'
            f'<span class="visually-hidden">: {esc(s["company"])}</span></a>')


def watch_html(s: dict) -> str:
    """Add company to Watchlist, or the On your Watchlist pill. Nothing for an unnamed company."""
    if not s["company_id"]:
        return ""
    if s["watched"]:
        return watch_pill(s)
    return (f'<button type="button" class="btn" data-act="watch" data-cid="{esc(s["company_id"])}" '
            f'data-signal="{esc(s["id"])}" data-company="{esc(s["company"])}">Add company to Watchlist'
            f'<span class="visually-hidden">: {esc(s["company"])}</span></button>')


def actions_html(s: dict) -> str:
    return (f'<div class="story-actions">{watch_html(s)}<button type="button" class="btn" data-act="done" '
            f'data-id="{esc(s["id"])}">Done<span class="visually-hidden">: {esc(s["headline"])}</span>'
            f'</button></div>')


def source_line(s: dict) -> str:
    return f'<p class="story-line">{_join([company_link(s), sources_words(s)])}</p>'


def _list_item(s: dict) -> str:
    """One story as a row on the story and section pages."""
    return (f'<li><a href="{story_href(s)}">{esc(s["headline"])}</a><span class="meta">'
            f'{_join([esc(s["company"]), "Found " + esc(_day(s["found"]))])}</span></li>')


def top_card(s: dict, first: bool) -> str:
    hid = f"t-{s['id']}"
    why = f'<p class="treason">{esc(sentence_of(s["why"]))}</p>' if s["why"] else ""
    move = (f'<div class="tmove"><p class="tmove-l">Next move</p><p class="tmove-t">{esc(s["move"])}</p></div>'
            if s["move"] else "")
    return (f'<li><article class="card g-story glow-faint tcard{" is-first" if first else ""}" '
            f'aria-labelledby="{hid}" data-story="{s["id"]}"><p class="tline">'
            f'{_join([company_link(s), sources_words(s)])}</p>'
            f'<h3 class="th" id="{hid}">{headline_link(s)}</h3>{why}{move}{actions_html(s)}</article></li>')


def sentence_of(text: str) -> str:
    """The scorer's reason, its first sentence."""
    text = " ".join(str(text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return m.group(1) if m else text


def row(s: dict) -> str:
    pill = watch_pill(s) if s["company_id"] and s["watched"] else ""
    return (f'<li class="srow" data-story="{s["id"]}">{headline_link(s, "srow-h")}'
            f'<p class="srow-m"><span class="srow-src">{_join([company_link(s), sources_words(s)])}</span>'
            f'{pill}</p></li>')


def sec_head(sid: str, title: str, count: int, icon: str = "story") -> str:
    return (f'<header class="sec-head">{page_frame.chip(icon, "story")}'
            f'<h2 class="sec-title" id="{sid}-h">{esc(title)}</h2>'
            f'<span class="sec-count">{count}</span></header>')


def toggle(tid: str, words: str) -> str:
    return (f'<button type="button" class="more-btn" data-toggle aria-expanded="false" '
            f'aria-controls="{tid}">{esc(words)}</button>')


SECTION_IDS = {"funding": "funding", "spinoffs": "spin-offs", "market": "regulation-and-market"}


def head_html(model: dict) -> str:
    w = model["watcher"]
    when = page_frame._when(w["last_run"]) if w.get("last_run") else None
    # One inline run inside the pill, which is a flex box and would drop the space.
    upd = (f'<span class="sec-count upd"><span>Updated <time datetime="{when[0]}" data-hm>'
           f'{esc(when[1].split(" ")[2])}</time></span></span>' if when else
           '<span class="sec-count upd">Not run yet</span>')
    cover = f'{model["answering"]} of {model["sources"]} sources answering'
    return (f'<header class="ins-head"><div class="ins-title">{page_frame.chip("story", "story")}'
            f'<h1>Insights</h1>{upd}</div><a class="plain ins-health" href="/insights/sources">{cover}</a></header>')


def notice_html(model: dict) -> str:
    w = model["watcher"]
    state = w.get("state")
    if state not in WATCHER_WORDS:
        return ""
    ran = page_frame._when(w["last_run"]) if w.get("last_run") else None
    last = (f'Last finished run <time datetime="{ran[0]}" data-local>{esc(ran[1])}</time>. ' if ran else "")
    cls = "is-warn" if state == "late" else "is-bad"
    return (f'<section class="ins-notice {cls}" aria-labelledby="notice-h"><h2 id="notice-h">'
            f'{WATCHER_WORDS[state]}</h2><p>{last}<a class="plain" href="/health">What the watcher reported</a>'
            f'</p></section>')


def toc_html(model: dict) -> str:
    items = [("top-stories", "Top stories", len(model["top"])),
             ("other-stories", "Other stories this week", len(model["other"]))]
    items += [(SECTION_IDS[sec["key"]], sec["name"], sec["count"]) for sec in model["sections"]]
    items.append(("more-stories", "More stories", len(model["below"])))
    links = page_frame.divided(f'<a class="plain" href="#{i}">{esc(t)}<span class="visually-hidden">, </span>'
                               f'<span class="toc-n">{n}</span></a>' for i, t, n in items)
    return f'<nav class="ins-toc" aria-label="Sections on this page">{links}</nav>'


def top_html(model: dict) -> str:
    top = model["top"]
    body = (f'<ol class="tlist">{"".join(top_card(s, i == 0) for i, s in enumerate(top))}</ol>' if top
            else '<div class="card g-story glow-faint scard"><p class="none">No top stories.</p></div>')
    return (f'<section class="ins-sec" id="top-stories" aria-labelledby="top-stories-h">'
            f'{sec_head("top-stories", "Top stories", len(top))}{body}</section>')


def other_html(model: dict) -> str:
    other = model["other"]
    body = (f'<ul class="rows">{"".join(row(s) for s in other)}</ul>' if other
            else '<p class="none">No other stories this week.</p>')
    return (f'<section class="ins-sec" id="other-stories" aria-labelledby="other-stories-h">'
            f'{sec_head("other-stories", "Other stories this week", len(other))}'
            f'<div class="card g-story glow-faint scard">{body}</div></section>')


def section_html(sec: dict) -> str:
    sid = SECTION_IDS[sec["key"]]
    week, older = sec["stories"], sec["earlier"]
    parts = []
    if week:
        parts.append(f'<ul class="rows">{"".join(row(s) for s in week[:SECTION_SHOWN])}</ul>')
        rest = week[SECTION_SHOWN:]
        if rest:
            parts.append(f'<div id="{sid}-rest" hidden><ul class="rows">{"".join(row(s) for s in rest)}</ul></div>'
                         f'{toggle(sid + "-rest", f"Show the other {len(rest)}")}')
    if older:
        parts.append(f'<div id="{sid}-earlier" hidden><ul class="rows">{"".join(row(s) for s in older)}</ul></div>'
                     f'{toggle(sid + "-earlier", f"From earlier weeks {len(older)}")}')
    card_ = f'<div class="card g-story glow-faint scard">{"".join(parts)}</div>' if parts else ""
    return (f'<section class="ins-sec" id="{sid}" aria-labelledby="{sid}-h">'
            f'{sec_head(sid, sec["name"], sec["count"])}{card_}</section>')


def more_html(model: dict) -> str:
    below = model["below"]
    body = ""
    if below:
        body = (f'<div class="card g-story glow-faint scard"><div id="more-list" hidden><ul class="rows">'
                f'{"".join(row(s) for s in below)}</ul></div>'
                f'{toggle("more-list", f"Show all {len(below)} headlines")}</div>')
    return (f'<section class="ins-sec" id="more-stories" aria-labelledby="more-stories-h">'
            f'{sec_head("more-stories", "More stories", len(below))}{body}</section>')


def done_html(model: dict) -> str:
    """The stories marked Done this week, to put one back."""
    hidden = model["hidden"]
    if not hidden:
        return ""
    lis = "".join(f'<li class="srow" data-story="{s["id"]}">{headline_link(s, "srow-h")}<p class="srow-m">'
                  f'<span class="srow-src">{_join([company_link(s), sources_words(s)])}</span>'
                  f'<button type="button" class="btn" data-act="show" data-id="{s["id"]}">Put back'
                  f'<span class="visually-hidden">: {esc(s["headline"])}</span></button></p></li>' for s in hidden)
    return (f'<section class="ins-sec" id="done-stories" aria-labelledby="done-stories-h">'
            f'{sec_head("done-stories", "Done this week", len(hidden))}<div class="card g-story glow-faint scard">'
            f'<div id="done-list" hidden><ul class="rows">{lis}</ul></div>'
            f'{toggle("done-list", f"Show {len(hidden)}")}</div></section>')


def front_body(model: dict) -> str:
    grid = other_html(model) + "".join(section_html(sec) for sec in model["sections"])
    return (f'{head_html(model)}{toc_html(model)}{notice_html(model)}{top_html(model)}'
            f'<div class="ins-grid">{grid}</div>{more_html(model)}{done_html(model)}')


def _stage(stage) -> str:
    stage = str(stage or "").strip()
    if not stage:
        return ""
    if stage.lower().startswith("series "):
        return "Series " + stage.split(" ", 1)[1].upper()
    return stage[:1].upper() + stage[1:]


def _people(raw) -> str:
    try:
        items = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return str(raw or "")
    if isinstance(items, list):
        return ", ".join(str(p.get("name", p)) if isinstance(p, dict) else str(p) for p in items)
    return str(items)


def company_html(conn, s: dict, others: list | None = None) -> str:
    c = s["company_rec"]
    if not c:
        return ('<aside class="company" aria-labelledby="co-h"><h2 id="co-h">About the company</h2>'
                '<p class="empty-line">The source names no single company, so there is no record to show and '
                'nothing to add to the Watchlist.</p></aside>')
    where = ", ".join(x for x in (c.get("city"), c.get("country")) if x)
    rows = [("Builds", c.get("what_they_build")), ("Software", c.get("software_content")),
            ("Stage", _stage(c.get("stage"))), ("Where", where), ("People", _people(c.get("people")))]
    dl = "".join(f'<dt>{k}</dt><dd class="{"" if v else "nk"}">{esc(v or "Not known")}</dd>' for k, v in rows)
    parts = [f'<h2 id="co-h">About {esc(c["display_name"])}</h2><dl>{dl}</dl>']
    last = conn.execute("SELECT touched_at, channel FROM touches WHERE company_id = ? OR LOWER(company) = LOWER(?)"
                        " ORDER BY touched_at DESC, id DESC LIMIT 1", (c["id"], c["display_name"])).fetchone()
    contact = (f'Last contact {_day(last[0])} ({dict(CHANNELS).get(last[1], last[1] or "other").lower()}).'
               if last else "No contact yet.")
    cid = esc(c["id"])
    watch_line = (f'On your Watchlist since {_day(c["watched_at"])}.' if s["watched"]
                  else "Not on your Watchlist.")
    parts.append(f'<a class="rec" href="/watchlist/{cid}">{esc(c["display_name"])}’s record</a>'
                 f'<span class="meta"><span data-watch-line="{cid}">{esc(watch_line)}</span> '
                 f'<span data-contact-line="{cid}">{esc(contact)}</span></span>')
    roles = conn.execute(
        "SELECT id, title, tier, applied_at, location FROM opportunities WHERE company_id = ?"
        " AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL"
        " ORDER BY first_seen DESC LIMIT 6", (c["id"],)).fetchall()
    if roles:
        tiers = {"top": "Top prospect", "question": "One question away", "reading": "Reading", "filtered": "Filtered"}
        lis = "".join(f'<li><a href="/jobs/{r["id"]}">{esc(r["title"] or "A role")}</a><span class="meta">'
                      + page_frame.divided(["Applied" if r["applied_at"] else
                                           esc(tiers.get(r["tier"] or "", "Not scored")),
                                           esc(r["location"] or "")])
                      + '</span></li>' for r in roles)
        parts.append(f'<h3>Roles on Jobs</h3><ul>{lis}</ul>')
    else:
        parts.append(f'<h3>Roles on Jobs</h3><p class="empty-line">No open roles from {esc(c["display_name"])} on Jobs.</p>')
    if others:
        lis = "".join(_list_item(o) for o in others[:5])
        parts.append(f'<h3>Other stories about {esc(c["display_name"])}</h3><ul class="plain-list">{lis}</ul>')
    return f'<aside class="company" aria-labelledby="co-h">{"".join(parts)}</aside>'


def drafts_html(s: dict) -> str:
    d = s["drafts"]
    if not d:
        return ""
    out = []
    for field, title in (("draft_comment", "Comment"), ("draft_note", "Connection note")):
        if d.get(field):
            out.append(f'<div class="draft"><h3 id="d-{field}">{title}</h3><p class="text" id="t-{field}">'
                       f'{esc(d[field])}</p><button type="button" class="btn" data-act="copy" data-target="t-{field}" '
                       f'data-label="Copy the {title.lower()}">Copy the {title.lower()}</button></div>')
    return (f'<h2>Drafts to copy</h2><p class="src-note">Written when the story arrived. Copy one and make it yours '
            f'where you post it. Nothing is ever posted or sent by the machine.</p>'
            f'<div class="drafts">{"".join(out)}</div>')


def log_html(s: dict) -> str:
    """Contacts are logged on the Watchlist, with the next step and its due
    date, so the story page links there."""
    if not s["company_id"]:
        return ""
    return (f'<p class="log-line">Contacts with {esc(s["company"])} are logged on the Watchlist, with '
            f'the next step and its due date. <a href="/watchlist/{esc(s["company_id"])}">'
            f'Log a contact on the Watchlist<span class="visually-hidden">: {esc(s["company"])}</span></a></p>')


def story_body(conn, model: dict, s: dict) -> str:
    sec = s["section"]
    crumbs = (f'<nav class="crumbs" aria-label="You are here"><ol><li><a href="/insights">Insights</a></li>'
              f'<li><a href="/insights/section/{sec}">{esc(SECTION_NAME[sec])}</a></li>'
              f'<li><span aria-current="page">This story</span></li></ol></nav>')
    n = s["count"]
    facts = (f'<p class="facts-line">{_join([esc(TYPE_WORDS.get(s["type"], "Other news")), "Found " + esc(_day(s["found"])), f"Reported by {n} source" + ("s" if n > 1 else "")])}</p>')
    first = s["reports"][0]
    names = source_names()
    summary = ""
    words = (first.get("summary") or "").strip()
    if words:
        who = source_name(names, first.get("source_id"))
        who = "the job advert" if who == "a job advert" else who
        summary = (f'<h2>Summary</h2><p class="s-body">{esc(words)}</p><p class="src-note">In the words of '
                   f'{esc(who)}, the first report the watcher found.</p>')
    why = (f'<h2>Why it matters</h2><p class="why">{esc(s["why"])}</p><p class="src-note">The scoring model wrote '
           f'this from the reports below. Where a report does not say whether a company has software or quality '
           f'leadership, the model assumes it does not, so check before you act.</p>')
    reps = []
    for r in s["reports"]:
        src = source_name(names, r.get("source_id"))
        url = bd.safe_url(r.get("source_url"))
        link = (f'<a href="{esc(url)}" target="_blank" rel="noopener">Read the announcement<span '
                f'class="visually-hidden"> on {esc(src)} (opens in a new tab)</span>{ICON_OUT}</a>') if url else ""
        split = ""
        if n > 1 and r["id"] != s["id"]:
            split = (f'<button type="button" class="btn quiet" data-act="split" data-id="{r["id"]}">Not the same '
                     f'story<span class="visually-hidden">: the report from {esc(src)}</span></button>')
        reps.append(f'<li><span class="src">{esc(src[:1].upper() + src[1:])}</span><span class="as">'
                    f'{esc(radar_common.clean_headline(r.get("headline") or ""))}</span><span class="meta"><span>Found '
                    f'{esc(_day(r["first_seen"]))}</span>{link}{split}</span></li>')
    reports = f'<h2>Reported by {n} source{"s" if n > 1 else ""}</h2><ul class="reports">{"".join(reps)}</ul>'
    article = (f'<article class="article" aria-labelledby="story-h1" data-story="{s["id"]}">{source_line(s)}'
               f'<h1 id="story-h1">{esc(s["headline"])}</h1>{facts}{actions_html(s)}{summary}{why}{reports}'
               f'{drafts_html(s)}{log_html(s)}<a class="back" href="/insights">Back to Insights</a></article>')
    others = [o for o in model["stories"] if o["company_id"] and o["company_id"] == s["company_id"]
              and o["id"] != s["id"] and not o["hidden"]]
    return f'<div class="story-page">{crumbs}{article}{company_html(conn, s, others)}</div>'


def section_body(model: dict, key: str) -> str:
    name = SECTION_NAME[key]
    items = [s for s in model["stories"] if s["section"] == key and not s["hidden"]]
    week = [s for s in items if s["this_week"]]
    older = [s for s in items if not s["this_week"]]
    parts = [f'<nav class="crumbs" aria-label="You are here"><ol><li><a href="/insights">Insights</a></li>'
             f'<li><span aria-current="page">{esc(name)}</span></li></ol></nav><h1>{esc(name)}</h1>']
    for title, group in (("The last seven days", week), ("Earlier", older)):
        parts.append(f'<h2 class="quiet-sub">{title}</h2>')
        if group:
            parts.append(f'<ul class="plain-list">{"".join(_list_item(s) for s in group)}</ul>')
        else:
            parts.append('<p class="empty-line">Nothing here.</p>')
    return f'<div class="section-page">{"".join(parts)}</div>'


PAGE_SCRIPT = r"""
(function(){
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var toast = document.querySelector('.toast'), msg = toast && toast.querySelector('.toast-msg'),
      closeBtn = toast && toast.querySelector('.toast-close');
  var live = document.getElementById('live');
  // Polite words for a screen reader. The region is always rendered, so it is announced.
  function announce(text){ if(!live) return; live.textContent=''; window.requestAnimationFrame(function(){ live.textContent=text; }); }
  // A failure stays on screen until it is closed or the next action, and the page keeps
  // room under it so it never covers what has focus.
  function closeToast(){ if(!toast) return; toast.classList.remove('show'); document.documentElement.style.scrollPaddingBottom=''; }
  function fail(text, focusAfter){
    if(toast){ msg.textContent=text; toast.classList.add('show');
      document.documentElement.style.scrollPaddingBottom=(toast.offsetHeight+48)+'px'; }
    announce(text); if(focusAfter) focusAfter.focus();
  }
  if(closeBtn) closeBtn.addEventListener('click',closeToast);
  document.addEventListener('keydown',function(e){ if(e.key==='Escape'&&toast&&toast.classList.contains('show')) closeToast(); });
  function busy(b,on){ if(on){ b.setAttribute('aria-disabled','true'); b.dataset.busy='1'; }
    else { b.removeAttribute('aria-disabled'); delete b.dataset.busy; } }
  function post(path, body){
    return fetch(path,{method:'POST',headers:{'Content-Type':'application/json','__WRITE_HEADER__':'1'},body:JSON.stringify(body)})
      .then(function(r){ return r.json().catch(function(){
        return {ok:false,note:'No usable answer came back, so that may not have saved. Reload to check.'}; }); })
      .catch(function(){ return {ok:false,note:'The dashboard did not answer. Nothing was saved.'}; });
  }
  var since={};
  function act(b, path, body, done){
    if(b.dataset.busy) return; busy(b,true); closeToast();
    post(path, body).then(function(res){ busy(b,false);
      if(!res.ok){ fail(res.note||'That did not stick. Try again.', b); return; }
      done(res); });
  }
  function el(tag, cls, text){ var n=document.createElement(tag); if(cls) n.className=cls; if(text!=null) n.textContent=text; return n; }
  function vh(text){ return el('span','visually-hidden',text); }
  function setLine(sel, text){ document.querySelectorAll(sel).forEach(function(n){ n.textContent=text; }); }

  function watched(cid, name){
    var wrap=el('span','watch-done'), a=el('a','wpill','On your Watchlist'); a.href='/watchlist/'+cid;
    a.appendChild(vh(': '+name));
    var u=el('button','btn quiet','Undo'); u.type='button'; u.dataset.act='unwatch'; u.dataset.cid=cid; u.dataset.company=name;
    u.appendChild(vh(': take '+name+' off your Watchlist'));
    wrap.appendChild(a); wrap.appendChild(u); return wrap;
  }
  var restore = {};

  document.addEventListener('click',function(e){
    var b=e.target.closest('button[data-act]'); if(!b) return;
    var kind=b.dataset.act;
    if(b.dataset.busy){ e.preventDefault(); return; }
    if(kind==='copy'){
      var src=document.getElementById(b.dataset.target), label=b.dataset.label||b.textContent;
      (navigator.clipboard?navigator.clipboard.writeText(src.textContent):Promise.reject()).then(function(){
        b.textContent='Copied'; announce('Copied.'); setTimeout(function(){ b.textContent=label; },1600);
      },function(){ fail('The browser would not copy. Select the text and copy it by hand.', b); });
      return;
    }
    if(kind==='watch'){
      var cid=Number(b.dataset.cid), name=b.dataset.company, sid=Number(b.dataset.signal);
      var add={company_id:cid,via:'insights'}; if(sid) add.signal_id=sid;
      act(b,'/watchlist/add',add,function(res){
        since[cid]=res.already?null:res.watched_at;
        restore[cid]=[];
        document.querySelectorAll('button[data-act="watch"][data-cid="'+cid+'"]').forEach(function(x){
          var w=watched(cid,name); restore[cid].push({w:w,b:x}); x.replaceWith(w);
          if(x===b) w.querySelector('a').focus(); });
        setLine('[data-watch-line="'+cid+'"]','On your Watchlist from today.');
        announce(name+' is on your Watchlist.');
      });
      return;
    }
    if(kind==='unwatch'){
      var cid2=Number(b.dataset.cid), name2=b.dataset.company, mine=b.closest('.watch-done');
      act(b,'/watchlist/remove',{company_id:cid2,undo_add:true,since:since[cid2]||''},function(){
        (restore[cid2]||[]).forEach(function(p){ p.w.replaceWith(p.b); if(p.w===mine) p.b.focus(); });
        setLine('[data-watch-line="'+cid2+'"]','Not on your Watchlist.');
        announce(name2+' is off your Watchlist.');
      });
      return;
    }
    if(kind==='done'){
      var card=b.closest('[data-story]'); if(!card) return; var id=Number(b.dataset.id);
      act(b,'/story/hide',{id:id},function(){
        var link=card.querySelector('.th a, h1'), headline=link?link.firstChild.textContent:'This story';
        var target=card.classList.contains('article')?b.closest('.story-actions'):card;
        var ranked=document.querySelectorAll('.srow[data-story="'+id+'"]');
        var slot=el('div','hidden-slot'), t=el('span');
        t.appendChild(document.createTextNode('Done: ')); t.appendChild(el('span','hl',headline)); slot.appendChild(t);
        var u=el('button','btn','Undo'); u.type='button'; u.appendChild(vh(': '+headline)); slot.appendChild(u);
        u.addEventListener('click',function(){
          if(u.dataset.busy) return; busy(u,true); closeToast();
          post('/story/show',{id:id}).then(function(r2){ busy(u,false);
            if(!r2.ok){ fail(r2.note||'That did not stick. Try again.', u); return; }
            slot.remove(); target.hidden=false; ranked.forEach(function(n){ n.hidden=false; });
            var f=target.querySelector('.th a, button[data-act="done"]'); if(f) f.focus();
            announce('Story back on the page.');
          });
        });
        target.parentNode.insertBefore(slot,target); target.hidden=true;
        ranked.forEach(function(n){ n.hidden=true; });
        u.focus(); announce('Done. Undo is where it was.');
      });
      return;
    }
    if(kind==='show'){
      act(b,'/story/show',{id:Number(b.dataset.id)},function(){
        var s=el('span','shown','Put back'); s.tabIndex=-1;
        b.replaceWith(s); s.focus(); announce('Story put back.');
      });
      return;
    }
    if(kind==='split'){
      act(b,'/story/split',{id:Number(b.dataset.id)},function(){
        var a=el('a',null,'Now a story of its own'); a.href='/insights/story/'+b.dataset.id;
        b.replaceWith(a); a.focus(); announce('Split off into a story of its own.');
      });
    }
  });
  document.querySelectorAll('[data-toggle]').forEach(function(btn){
    btn.addEventListener('click',function(){
      var open=btn.getAttribute('aria-expanded')!=='true'; btn.setAttribute('aria-expanded',String(open));
      document.getElementById(btn.getAttribute('aria-controls')).hidden=!open;
    });
  });
  document.querySelectorAll('main time[data-local]').forEach(function(t){
    var when=new Date(t.getAttribute('datetime')); if(isNaN(when)) return;
    var m=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    t.textContent=when.getDate()+' '+m[when.getMonth()]+' '+('0'+when.getHours()).slice(-2)+':'+('0'+when.getMinutes()).slice(-2);
  });
  document.querySelectorAll('main time[data-hm]').forEach(function(t){
    var when=new Date(t.getAttribute('datetime')); if(isNaN(when)) return;
    t.textContent=('0'+when.getHours()).slice(-2)+':'+('0'+when.getMinutes()).slice(-2);
  });
})();
""".replace("__WRITE_HEADER__", bd.WRITE_HEADER)

TOAST = ('<p class="visually-hidden" id="live" role="status" aria-live="polite"></p>'
         '<div class="toast is-bad"><span class="toast-msg"></span>'
         '<button type="button" class="btn quiet toast-close" aria-label="Close message">Close</button></div>')


def page(title: str, body: str, jobs: dict | None) -> str:
    bar = page_frame.appbar_html("insights", jobs=jobs)
    css = page_frame.FRAME_CSS + page_frame.GROUPS_CSS + page_frame.TITLES_CSS + PAGE_CSS
    return ("<!doctype html>\n<html lang=\"en-GB\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{esc(title)}</title><style>{css}</style></head><body>{bar}"
            f"<main id=\"main\" class=\"page news\" tabindex=\"-1\">{body}</main>{TOAST}"
            f"<script>{page_frame.FRAME_SCRIPT}{PAGE_SCRIPT}</script></body></html>")


def render_front(conn, config: dict | None = None, now: str | None = None) -> str:
    model = build_model(conn, config, now)
    return page("Insights, MedTech Radar", front_body(model), model["jobs"])


def render_story(conn, signal_id: int, config: dict | None = None, now: str | None = None) -> str | None:
    model = build_model(conn, config, now)
    s = find_story(model, signal_id)
    if s is None:
        return None
    title = s["headline"] if len(s["headline"]) <= 70 else s["headline"][:70].rsplit(" ", 1)[0] + "…"
    return page(f"{title}, Insights, MedTech Radar", story_body(conn, model, s), model["jobs"])


def render_section(conn, key: str, config: dict | None = None, now: str | None = None) -> str | None:
    if key not in SECTION_NAME:
        return None
    model = build_model(conn, config, now)
    return page(f"{SECTION_NAME[key]}, Insights, MedTech Radar", section_body(model, key), model["jobs"])
