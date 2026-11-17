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
/watchlist?company=<id>, which is also where On your Watchlist leads. Nothing
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
    top, rest = above[:TOP_COUNT], above[TOP_COUNT:]
    sections = []
    for key, name, _types in SECTIONS:
        sections.append({
            "key": key, "name": name,
            "stories": [s for s in rest if s["section"] == key],
            "in_top": [s for s in top if s["section"] == key],
            "earlier": [s for s in earlier if s["section"] == key and s["relevance"] >= FLOOR],
        })
    for sec in sections:
        sec["count"] = len(sec["stories"]) + len(sec["in_top"])
    jobs = radar_common.job_health(conn, now)
    watcher = jobs.get("signals") or {}
    sources = [s for s in bd.load_watchlist() if (s.get("status") or "live") != "unverified"]
    state = {r["source_id"]: dict(r) for r in conn.execute("SELECT * FROM source_state")}
    answering = sum(1 for s in sources if str((state.get(s.get("id")) or {}).get("last_status") or "")
                    in ("200", "304"))
    return {
        "now": _iso(moment), "stories": stories, "week": week, "earlier": earlier,
        "above": above, "below": below, "top": top, "sections": sections,
        "ranked": above[:RANKED], "buying": sum(1 for s in week if s["relevance"] >= BUYING), "hidden": [s for s in stories if s["hidden"] and s["this_week"]],
        "watcher": watcher, "jobs": jobs, "sources": len(sources), "answering": answering,
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
:root{--font-serif:"IBM Plex Serif",Georgia,"Times New Roman",serif;--gutter:16px;--rail-w:320px}
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
/* The content box, set once and read by the page and by the top bar's
   contents (page_frame's .appbar-in): capped at 1280px, 2496px from 1920px,
   with the gutter either side. */
:root{--frame-max:1280px;--frame-pad:var(--gutter)}
.page{margin:0 auto;padding:0 var(--frame-pad) 64px;max-width:var(--frame-max)}
@media (min-width:768px){:root{--gutter:24px}}
@media (min-width:1920px){:root{--gutter:48px;--rail-w:400px;--frame-max:2496px}}
.masthead{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 24px;padding:24px 0 16px;
  border-bottom:1px solid var(--border-subtle)}
.masthead h1{font:500 40px/48px var(--font-serif);letter-spacing:-.01em}
.edition{color:var(--text-2);font-size:14px;line-height:20px}
.edition b{color:var(--text-1);font-weight:600}
.tally{flex-basis:100%;color:var(--text-3);font-size:14px;line-height:20px}
.tally a{color:var(--text-2)}
.notice{display:flex;gap:12px;margin:16px 0 0;padding:16px;border:1px solid var(--border-strong);
  border-left-width:4px;border-radius:var(--radius-2);background:var(--surface)}
.notice svg{flex:none;margin-top:2px}
.notice h2{font:600 16px/24px var(--font-sans)}
.notice p{color:var(--text-2);font-size:14px;line-height:20px;margin-top:4px;max-width:96ch}
.notice.is-bad{border-color:var(--negative);background:var(--negative-tint)}
.notice.is-bad svg{color:var(--negative)}
.notice.is-warn{border-color:var(--caution);background:var(--caution-tint)}
.notice.is-warn svg{color:var(--caution)}
.notice.is-quiet{border-left-color:var(--text-3)}
.sec-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 12px;padding-top:12px;
  border-top:3px solid var(--text-1);margin-bottom:16px}
.sec-head h2{font:600 20px/28px var(--font-sans)}
.sec-head .count{color:var(--text-3);font-size:14px;line-height:20px}
.story{position:relative;display:flex;flex-direction:column;gap:8px;min-width:0}
.kicker{color:var(--text-3);font-size:12px;line-height:16px}
.story-line{color:var(--text-3);font-size:13px;line-height:20px;overflow-wrap:anywhere}
.story-line .co{color:var(--text-2);font-weight:600}
.story-line .sep{padding:0 4px}
.story-h{font:500 20px/26px var(--font-serif);letter-spacing:-.005em;overflow-wrap:anywhere}
.story-h a{color:var(--text-1);text-decoration:none}
.story-h a:hover{text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:4px}
.story-h a::after{content:"";position:absolute;inset:0;z-index:0}
.story-sum{color:var(--text-2);font-size:15px;line-height:22px;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;overflow:hidden}
.story-meta{color:var(--text-3);font-size:13px;line-height:20px}
.story-meta .rel{color:var(--text-2)}
.story-meta a,.story-actions{position:relative;z-index:1}
.story-meta a{color:var(--accent);white-space:nowrap}
.story-meta a svg{vertical-align:-1px;margin-left:3px}
.story-actions{display:flex;flex-wrap:wrap;gap:8px 12px;margin-top:auto;padding-top:4px}
.story-actions>.btn.quiet:first-child{margin-left:-8px}
.btn{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 12px;border:1px solid var(--border-strong);
  border-radius:var(--radius-2);background:transparent;color:var(--text-1);font:500 13px/20px var(--font-sans);
  cursor:pointer;text-decoration:none;white-space:nowrap}
.btn:hover{background:var(--row-hover);color:var(--text-1)}
.btn.quiet{border-color:transparent;color:var(--text-2);padding:0 8px}
.btn.quiet:hover{color:var(--text-1)}
.btn[disabled]{opacity:.55;cursor:default}
.on-list{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0;color:var(--positive);
  font:500 13px/20px var(--font-sans);text-decoration:none;white-space:nowrap}
.on-list:hover{color:var(--positive);text-decoration:underline;text-underline-offset:3px}
@media (pointer:coarse){.btn,.on-list{height:44px}.story-meta a{display:inline-flex;align-items:center;min-height:44px}}
.story-lead .story-h{font-size:30px;line-height:38px}
.story-lead .story-sum{font-size:17px;line-height:26px;-webkit-line-clamp:3}
.ranked h2{font:600 18px/24px var(--font-sans);padding-top:12px;border-top:3px solid var(--text-1)}
.ranked .sub{color:var(--text-3);font-size:13px;line-height:20px;margin:4px 0 8px}
.ranked ol{counter-reset:rank}
.ranked li{counter-increment:rank;display:grid;grid-template-columns:40px 1fr;gap:0 8px;padding:12px 0;
  border-bottom:1px solid var(--border-subtle)}
.ranked li::before{content:counter(rank);grid-row:span 2;font:500 28px/32px var(--font-serif);color:var(--accent)}
.ranked a{color:var(--text-1);text-decoration:none;font:500 16px/22px var(--font-serif)}
.ranked a:hover{text-decoration:underline;text-underline-offset:3px}
.ranked .meta{color:var(--text-3);font-size:13px;line-height:20px}
.earlier{margin-top:20px}
.earlier h3{font:600 14px/20px var(--font-sans);color:var(--text-2);margin-bottom:4px}
.earlier li,.plain-list li{padding:8px 0;border-top:1px solid var(--border-subtle)}
.earlier li a,.plain-list li a{color:var(--text-1);text-decoration:none;font:500 15px/22px var(--font-serif)}
.earlier li a:hover,.plain-list li a:hover{text-decoration:underline;text-underline-offset:3px}
.earlier .meta,.plain-list .meta{display:block;color:var(--text-3);font-size:13px;line-height:20px}
.more-link{display:inline-block;margin-top:8px;font-size:14px;line-height:20px}
.empty-line{color:var(--text-3);font-size:14px;line-height:20px;padding:4px 0 8px}
.fold{margin-top:48px;border-top:1px solid var(--border-strong)}
.fold+.fold{margin-top:0}
.fold h2{font:inherit}
.fold-btn{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px;width:100%;min-height:56px;padding:12px 0;border:0;
  background:none;color:var(--text-1);text-align:left;cursor:pointer}
.fold-btn .t{font:600 20px/28px var(--font-sans);white-space:nowrap}
.fold-btn .s{color:var(--text-3);font-size:14px;line-height:20px}
.fold-btn .chev{margin-left:auto;transition:transform var(--dur)}
.fold-btn[aria-expanded="true"] .chev{transform:rotate(180deg)}
.fold-list{columns:var(--fold-cols,1);column-gap:32px;padding-bottom:8px}
.fold-list li{break-inside:avoid;padding:8px 0;border-top:1px solid var(--border-subtle)}
.fold-list a{color:var(--text-1);text-decoration:none;font:400 15px/22px var(--font-serif)}
.fold-list a:hover{text-decoration:underline}
.fold-list .meta{display:block;color:var(--text-3);font-size:13px;line-height:20px}
.fold-list .btn{margin-top:4px}
.fold-foot{padding:8px 0 0;color:var(--text-3);font-size:14px;line-height:20px}
@media (min-width:1024px){:root{--fold-cols:2}}
@media (min-width:1920px){:root{--fold-cols:3}}
.page-foot{margin-top:32px;padding-top:16px;border-top:1px solid var(--border-subtle);color:var(--text-3);
  font-size:13px;line-height:20px}
.page-foot p+p{margin-top:4px}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:40;display:flex;align-items:center;
  gap:16px;max-width:calc(100% - 32px);padding:12px 16px;border:1px solid var(--border-strong);
  border-radius:var(--radius-4);background:var(--surface-sunken);box-shadow:var(--shadow-overlay);
  color:var(--text-1);font-size:14px;line-height:20px}
.toast.is-bad{border-color:var(--negative)}
.toast:not(.show){width:1px;height:1px;padding:0;border:0;overflow:hidden;clip-path:inset(50%);box-shadow:none}
.toast:not(.show) .btn{display:none}
.hidden-slot{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;padding:16px;border:1px dashed var(--border-strong);
  border-radius:var(--radius-4);color:var(--text-2);font-size:14px;line-height:20px}
.hidden-slot .btn{flex:none}
.hidden-slot .hl{color:var(--text-1);font-family:var(--font-serif)}
.top-head{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.top-head h2{font:600 20px/28px var(--font-sans)}
.top-count{display:none;color:var(--text-2);font-size:14px;line-height:20px}
.top-nav{display:none;margin-left:auto;gap:8px}
.top-nav .btn{width:44px;height:44px;padding:0;justify-content:center}
.top-nav .btn[aria-disabled="true"]{opacity:.45;cursor:default}
.story{padding:0}
.front{display:grid;grid-template-columns:minmax(0,1fr);gap:40px;margin-top:24px}
.top-row{display:grid;gap:0}
.top-row>li{padding:16px 0;border-top:1px solid var(--border-subtle)}
.top-row>li:first-child{border-top:0;padding-top:0}
.col{min-width:0}
.col .stack>.story{padding:16px 0;border-top:1px solid var(--border-subtle)}
.col .stack>.story:first-child{border-top:0;padding-top:0}
.col .stack>.story:first-child .story-h{font-size:24px;line-height:30px}
.cols>.col+.col{margin-top:40px}
.quiet-sub{margin:24px 0 12px;font:600 16px/24px var(--font-sans);color:var(--text-2)}
.quiet-row{display:grid;gap:24px;grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}
.quiet-row .story{padding:16px 0;border-top:1px solid var(--border-subtle)}
@media (min-width:1024px){
  .front{grid-template-columns:minmax(0,1fr) var(--rail-w);grid-template-areas:"top top" "cols rail";gap:48px 0}
  .front>.top{grid-area:top}
  .front>.ranked{grid-area:rail;align-self:start;padding-left:24px;border-left:1px solid var(--border-subtle)}
  .front>.cols{grid-area:cols;padding-right:24px}
  .top-row{grid-template-columns:minmax(0,1.5fr) minmax(0,1fr) minmax(0,1fr)}
  .top-row>li{padding:0 0 0 24px;border-top:0;border-left:1px solid var(--border-subtle)}
  .top-row>li:first-child{grid-row:1 / span 2;padding:0 24px 0 0;border-left:0}
  .top-row>li:nth-child(2),.top-row>li:nth-child(3){padding-bottom:24px}
  .top-row>li:nth-child(4),.top-row>li:nth-child(5){padding-top:24px;border-top:1px solid var(--border-subtle)}
  .story-lead .story-h{font-size:40px;line-height:48px}
  .story-lead .story-sum{font-size:18px;line-height:28px;-webkit-line-clamp:4}
  .story-small .story-h{font-size:20px;line-height:26px}
  .cols{columns:2;column-gap:48px;column-rule:1px solid var(--border-subtle)}
  .cols>.col{break-inside:avoid;margin:0 0 40px}
  .cols>.col+.col{margin-top:0}
  .cols-none{columns:1}
}
@media (min-width:1920px){
  .front{grid-template-columns:minmax(0,1fr) 760px;grid-template-areas:"top rail" "cols cols"}
  .front>.top{padding-right:24px}
  .front>.cols{padding-right:0}
  .front>.ranked{align-self:stretch}
  .ranked ol{display:grid;grid-template-columns:1fr 1fr;grid-auto-flow:column;grid-template-rows:repeat(5,auto);gap:0 32px}
  .story-lead .story-h{font-size:48px;line-height:56px}
  .story-small .story-h{font-size:24px;line-height:30px}
  .cols{columns:auto;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin:0 -24px}
  .cols>.col{margin:0;padding:0 24px;border-left:1px solid var(--border-subtle)}
  .cols>.col:first-child{border-left:0}
  .cols-none{display:block;margin:0}
}
@media (max-width:767.98px){
  .masthead h1{font-size:30px;line-height:36px}
  .story-lead .story-h{font-size:26px;line-height:32px}
  .story-lead .story-sum{font-size:15px;line-height:22px}
  .top-count{display:block}
  .top-nav{display:flex}
  .top-row{display:flex!important;gap:12px;overflow-x:auto;scroll-snap-type:x mandatory;
    margin:0 calc(var(--gutter) * -1);padding:0 var(--gutter) 4px;scroll-padding:0 var(--gutter);scrollbar-width:none}
  .top-row::-webkit-scrollbar{display:none}
  .top-row>li{flex:0 0 calc(100% - 40px);scroll-snap-align:start;padding:16px;
    border:1px solid var(--border-subtle)!important;border-radius:var(--radius-4);background:var(--surface)}
  .top-row>li>.story{height:100%}
}
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
.log-line{margin-top:24px;max-width:72ch;color:var(--text-2);font-size:14px;line-height:20px}
.log-line a{color:var(--accent);text-underline-offset:2px}
.watch-done{display:inline-flex;flex-wrap:wrap;align-items:center;gap:4px 8px}
.shown{color:var(--text-2);font-size:13px;line-height:20px}
.ranked a,.ranked .meta,.fold-list a,.fold-list .meta,.earlier li a,.earlier .meta,.plain-list a,.plain-list .meta,
.article h1,.hidden-slot .hl,.company dd,.company h2,.reports .as,.reports .src,.masthead h1{overflow-wrap:anywhere}
.company{min-width:0}
.company h2{font:600 18px/24px var(--font-sans);padding-top:12px;border-top:3px solid var(--text-1)}
.company dl{display:grid;grid-template-columns:96px 1fr;gap:8px 12px;margin-top:12px;font-size:14px;line-height:20px}
.company dt{color:var(--text-3)}
.company dd{color:var(--text-1)}
.company dd.nk{color:var(--text-3)}
.company h3{margin-top:24px;font:600 15px/20px var(--font-sans)}
.company ul li{padding:8px 0;border-top:1px solid var(--border-subtle);font-size:14px;line-height:20px}
.company .meta{display:block;color:var(--text-3);font-size:13px;line-height:20px}
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


def watch_html(s: dict) -> str:
    """Add to Watchlist, or On your Watchlist once it is. Nothing for an unnamed company."""
    if not s["company_id"]:
        return ""
    if s["watched"]:
        return (f'<a class="on-list" href="/watchlist?company={esc(s["company_id"])}">{ICON_CHECK}'
                f'On your Watchlist<span class="visually-hidden">: {esc(s["company"])}, open it on the Watchlist'
                '</span></a>')
    return (f'<button type="button" class="btn" data-act="watch" data-cid="{esc(s["company_id"])}" '
            f'data-signal="{esc(s["id"])}" data-company="{esc(s["company"])}">Add to Watchlist'
            f'<span class="visually-hidden">: '
            f'{esc(s["company"])}</span></button>')


def actions_html(s: dict) -> str:
    return (f'<div class="story-actions">{watch_html(s)}<button type="button" class="btn quiet" data-act="hide" '
            f'data-id="{esc(s["id"])}">Hide<span class="visually-hidden"> this story: {esc(s["headline"])}</span>'
            f'</button></div>')


def source_line(s: dict) -> str:
    more = ""
    if s["count"] > 1:
        n = s["count"] - 1
        more = f' and {n} more source{"s" if n > 1 else ""}'
    return (f'<p class="story-line"><span class="co">{esc(s["company"])}</span><span class="sep" aria-hidden="true">·'
            f'</span><span class="visually-hidden">, </span>via {esc(s["source"])}{more}</p>')


def meta_line(s: dict) -> str:
    link = ""
    if s["url"]:
        link = (f' · <a href="{esc(s["url"])}" target="_blank" rel="noopener">Read the announcement'
                f'<span class="visually-hidden"> on {esc(s["source"])}: {esc(s["company"])} (opens in a new tab)'
                f'</span>{ICON_OUT}</a>')
    return (f'<p class="story-meta"><span class="rel">Relevance {esc(s["relevance"])}</span> · Found '
            f'{esc(_day(s["found"]))}{link}</p>')


def card(s: dict, size: str = "", tag: str = "h3", kicker: str = "") -> str:
    cls = f"story {('story-' + size) if size else ''}".strip()
    hid = f"h-{s['id']}-{size or 'c'}"
    kick = f'<p class="kicker">{esc(kicker)}</p>' if kicker else ""
    return (f'<article class="{cls}" aria-labelledby="{hid}" data-story="{s["id"]}">{kick}{source_line(s)}'
            f'<{tag} class="story-h" id="{hid}"><a href="{story_href(s)}">{esc(s["headline"])}</a></{tag}>'
            f'<p class="story-sum">{esc(s["summary"])}</p>{meta_line(s)}{actions_html(s)}</article>')


def _list_item(s: dict) -> str:
    return (f'<li><a href="{story_href(s)}">{esc(s["headline"])}</a><span class="meta">{esc(s["company"])} · '
            f'Relevance {s["relevance"]} · Found {esc(_day(s["found"]))}</span></li>')


def ranked_html(model: dict) -> str:
    items = "".join(f'<li data-rank-story="{esc(s["id"])}"><a href="{story_href(s)}">{esc(s["headline"])}</a>'
                    f'<span class="meta">{esc(s["company"])} · Relevance {s["relevance"]}</span></li>'
                    for s in model["ranked"])
    if not model["week"]:
        sub = "Nothing came in over the last seven days."
    elif not model["ranked"]:
        sub = f"Nothing scored {FLOOR} or more in the last seven days."
    elif not model["buying"]:
        sub = f"Ranked by relevance, over the last seven days. None reached {BUYING}, the mark of a buying window."
    else:
        sub = "Ranked by relevance, over the last seven days."
    body = f'<ol>{items}</ol>' if items else ""
    return (f'<aside class="ranked" aria-labelledby="ranked-h"><h2 id="ranked-h">Most relevant this week</h2>'
            f'<p class="sub">{sub}</p>{body}</aside>')


def section_link(sec: dict) -> str:
    return f'<a class="more-link" href="/insights/section/{sec["key"]}">All in {esc(sec["name"])}</a>'


def earlier_html(sec: dict) -> str:
    older = sec["earlier"]
    if not older:
        return section_link(sec) if sec["count"] else ""
    lis = "".join(_list_item(s) for s in older[:EARLIER_SHOWN])
    return f'<div class="earlier"><h3>From earlier weeks</h3><ul>{lis}</ul>{section_link(sec)}</div>'


def _count_words(sec: dict) -> str:
    n, t = len(sec["stories"]), len(sec["in_top"])
    if n + t == 0:
        return "None this week"
    return f"{n + t} this week" + (f", {t} in Top stories" if t else "")


def _empty_words(sec: dict) -> str:
    if sec["in_top"]:
        return f"This week's {'story is' if len(sec['in_top']) == 1 else 'stories are'} in Top stories."
    return f"Nothing in {sec['name']} scored {FLOOR} or more this week."


def section_html(sec: dict) -> str:
    cards = "".join(card(s, "col-first" if i == 0 else "") for i, s in enumerate(sec["stories"]))
    head = (f'<div class="sec-head"><h2 id="sec-{sec["key"]}">{esc(sec["name"])}</h2>'
            f'<span class="count">{_count_words(sec)}</span></div>')
    body = f'<div class="stack">{cards}</div>' if cards else f'<p class="empty-line">{_empty_words(sec)}</p>'
    return f'<section class="col" aria-labelledby="sec-{sec["key"]}">{head}{body}{earlier_html(sec)}</section>'


def masthead_html(model: dict, title: str = "Insights", tag: str = "h1") -> str:
    w = model["watcher"]
    ran = w.get("last_run")
    when = page_frame._when(ran) if ran else None
    if not ran:
        upd = "the watcher has not run yet"
    elif w.get("state") in ("stale", "failing", "never"):
        upd = (f'stories as of <b><time datetime="{when[0]}" data-local>{esc(when[1])}</time></b>'
               if when else "stories as of the last run")
    else:
        upd = (f'updated <b><time datetime="{when[0]}" data-local>{esc(when[1])}</time></b>'
               if when else "updated")
    week, buying = len(model["week"]), model["buying"]
    cover = f'{model["answering"]} of {model["sources"]} sources answering'
    return (f'<header class="masthead"><{tag}>{esc(title)}</{tag}><p class="edition"><b>The last seven days</b> · '
            f'{upd}</p><p class="tally">{week} {"story" if week == 1 else "stories"} this week, {buying} at '
            f'relevance {BUYING} or more · <a href="/health">{cover}</a></p></header>')


def notice_html(model: dict) -> str:
    w = model["watcher"]
    state = w.get("state")
    if state not in WATCHER_WORDS:
        return ""
    hours = w.get("hours_ago")
    ago = f", {int(hours)} hours ago" if isinstance(hours, (int, float)) else ""
    ran = page_frame._when(w["last_run"]) if w.get("last_run") else None
    last = (f'Its last finished run was <time datetime="{ran[0]}" data-local>{esc(ran[1])}</time>{ago}, and it '
            f'counts as late after {w.get("late_hours")} hours. ') if ran else ""
    cls = "is-warn" if state == "late" else "is-bad"
    note = "Anything reported since then is not on this page. " if state != "never" else ""
    return (f'<section class="notice {cls}" aria-labelledby="notice-h">{ICON_WARN}<div><h2 id="notice-h">'
            f'{WATCHER_WORDS[state]}</h2><p>{last}{note}<a href="/health">What the watcher reported</a></p></div>'
            f'</section>')


def top_html(model: dict) -> str:
    if not model["top"]:
        older = [s for s in model["earlier"] if s["relevance"] >= FLOOR][:3]
        week = len(model["week"])
        best = model["below"][0]["relevance"] if model["below"] else None
        if week:
            text = (f'Nothing has scored {FLOOR} or more in the last seven days. {week} '
                    f'{"story" if week == 1 else "stories"} came in and the highest scored {best}. '
                    f'{"It is" if week == 1 else "They are"} under More stories at the foot of the page.')
        else:
            text = "Nothing came in over the last seven days."
        cards = "".join(f"<li>{card(s, 'small', tag='h4')}</li>" for s in older)
        more = (f'<h3 class="quiet-sub">From earlier weeks</h3><ul class="quiet-row">{cards}</ul>' if older else "")
        return (f'<section class="top"><section class="notice is-quiet" aria-labelledby="quiet-h"><div>'
                f'<h2 id="quiet-h">A quiet week</h2><p>{text}</p></div></section>{more}</section>')
    lis = "".join(f'<li>{card(s, "lead" if i == 0 else "small")}</li>' for i, s in enumerate(model["top"]))
    n = len(model["top"])
    head = (f'<div class="top-head"><h2 id="top-h">Top stories</h2><p class="top-count" aria-live="polite">'
            f'<span class="visually-hidden">Showing story </span><span data-pos>1</span> of {n}</p>'
            f'<div class="top-nav"><button type="button" class="btn" data-dir="-1" aria-label="Previous top story" '
            f'aria-controls="top-row" aria-disabled="true">{ICON_PREV}</button><button type="button" class="btn" '
            f'data-dir="1" aria-label="Next top story" aria-controls="top-row">{ICON_NEXT}</button></div></div>')
    return (f'<section class="top" aria-labelledby="top-h">{head}<ol class="top-row" id="top-row" role="list" '
            f'aria-labelledby="top-h">{lis}</ol></section>')


def fold_html(fid: str, title: str, sub: str, items: str, foot: str = "") -> str:
    return (f'<section class="fold" aria-labelledby="{fid}-h"><h2 id="{fid}-h"><button type="button" class="fold-btn" '
            f'aria-expanded="false" aria-controls="{fid}-list"><span class="t">{esc(title)}</span>'
            f'<span class="visually-hidden">, </span><span class="s">{esc(sub)}</span>{ICON_DOWN}</button></h2>'
            f'<div id="{fid}-list" hidden><ul class="fold-list">{items}</ul>{foot}</div></section>')


def folds_html(model: dict) -> str:
    below = model["below"]
    out = ""
    if below:
        out = fold_html("more", "More stories", f"{len(below)} this week under relevance {FLOOR}",
                        "".join(_list_item(s) for s in below),
                        '<p class="fold-foot">Older stories stay on file. <a href="/archive">Every story on file</a></p>')
    hidden = model["hidden"]
    if hidden:
        lis = "".join(f'<li data-story="{s["id"]}"><a href="{story_href(s)}">{esc(s["headline"])}</a>'
                      f'<span class="meta">{esc(s["company"])} · Relevance {s["relevance"]}</span>'
                      f'<button type="button" class="btn" data-act="show" data-id="{s["id"]}">Show again'
                      f'<span class="visually-hidden">: {esc(s["headline"])}</span></button></li>' for s in hidden)
        out += fold_html("hidden", "Hidden this week", f"{len(hidden)} hidden", lis)
    return out


def foot_html(model: dict) -> str:
    extra = ""
    if model["unscored"]:
        extra = (f'<p>{model["unscored"]} {"item is" if model["unscored"] == 1 else "items are"} waiting for a score '
                 f'and not shown; scripts/rescore.py clears them.</p>')
    return ('<footer class="page-foot"><p>Times are shown in your own time zone. A story is one event; when several '
            'sources report it, the highest-scoring report leads and every report is on the story page. Hiring '
            'is not news here: job adverts and first hires are on <a href="/jobs">Jobs</a>. Relevance is how '
            f'close a story comes to a buying window; {BUYING} or more marks one, and only those reach the '
            'digest and your phone.</p><p>'
            'Headlines, summaries and the lines on why a story matters are written by the scoring model from the '
            f'sources. Check the source before you act on one.</p>{extra}</footer>')


# ================================================================ pages

def front_body(model: dict) -> str:
    any_section = any(sec["count"] for sec in model["sections"])
    if any_section:
        cols = f'<div class="cols">{"".join(section_html(sec) for sec in model["sections"])}</div>'
    else:
        older = sum(len(sec["earlier"]) for sec in model["sections"])
        cols = (f'<div class="cols cols-none"><section aria-labelledby="sec-none"><div class="sec-head">'
                f'<h2 id="sec-none">Sections</h2><span class="count">None this week</span></div>'
                f'<p class="empty-line">No section has a story at {FLOOR} or more this week. '
                + (f'{older} from earlier weeks {"is" if older == 1 else "are"} still on file.' if older
                   else 'Nothing from earlier weeks is open either.') + '</p></section></div>')
    front = f'<div class="front">{top_html(model)}{ranked_html(model)}{cols}</div>'
    return f'{masthead_html(model)}{notice_html(model)}{front}{folds_html(model)}{foot_html(model)}'


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
    parts.append(f'<a class="rec" href="/watchlist?company={cid}">{esc(c["display_name"])}’s record</a>'
                 f'<span class="meta"><span data-watch-line="{cid}">{esc(watch_line)}</span> '
                 f'<span data-contact-line="{cid}">{esc(contact)}</span></span>')
    roles = conn.execute(
        "SELECT id, title, tier, applied_at, location FROM opportunities WHERE company_id = ?"
        " AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL"
        " ORDER BY first_seen DESC LIMIT 6", (c["id"],)).fetchall()
    if roles:
        tiers = {"top": "Top prospect", "question": "One question away", "reading": "Reading", "filtered": "Filtered"}
        lis = "".join(f'<li><a href="/jobs/{r["id"]}">{esc(r["title"] or "A role")}</a><span class="meta">'
                      f'{"Applied" if r["applied_at"] else esc(tiers.get(r["tier"] or "", "Not scored"))}'
                      f'{" · " + esc(r["location"]) if r["location"] else ""}'
                      f'</span></li>' for r in roles)
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
            f'the next step and its due date. <a href="/watchlist?company={esc(s["company_id"])}">'
            f'Log a contact on the Watchlist<span class="visually-hidden">: {esc(s["company"])}</span></a></p>')


def story_body(conn, model: dict, s: dict) -> str:
    sec = s["section"]
    crumbs = (f'<nav class="crumbs" aria-label="You are here"><ol><li><a href="/insights">Insights</a></li>'
              f'<li><a href="/insights/section/{sec}">{esc(SECTION_NAME[sec])}</a></li>'
              f'<li><span aria-current="page">This story</span></li></ol></nav>')
    n = s["count"]
    facts = (f'<p class="facts-line"><span class="rel">Relevance {s["relevance"]}</span> · '
             f'{esc(TYPE_WORDS.get(s["type"], "Other news"))} · Found {esc(_day(s["found"]))} · '
             f'Reported by {n} source{"s" if n > 1 else ""}</p>')
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
                    f'{esc(_day(r["first_seen"]))} · Relevance {r["relevance"]}</span>{link}{split}</span></li>')
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
  var CHECK='<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false"><path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>';
  function setLine(sel, text){ document.querySelectorAll(sel).forEach(function(n){ n.textContent=text; }); }

  function watched(cid, name){
    var wrap=el('span','watch-done'), a=el('a','on-list'); a.href='/watchlist?company='+cid; a.innerHTML=CHECK;
    a.appendChild(document.createTextNode('On your Watchlist')); a.appendChild(vh(': '+name+', open it on the Watchlist'));
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
        announce(name+' is on your Watchlist, which will show it arrived from this story. Undo is next to it.');
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
    if(kind==='hide'){
      var card=b.closest('[data-story]'); if(!card) return; var id=Number(b.dataset.id);
      act(b,'/story/hide',{id:id},function(){
        var link=card.querySelector('.story-h a, h1'), headline=link?link.textContent:'This story';
        var target=card.classList.contains('article')?b.closest('.story-actions'):card;
        var ranked=document.querySelectorAll('[data-rank-story="'+id+'"]');
        var slot=el('div','hidden-slot'), t=el('span');
        t.appendChild(document.createTextNode('Hidden: ')); t.appendChild(el('span','hl',headline));
        t.appendChild(document.createTextNode('. It stays on file.')); slot.appendChild(t);
        var u=el('button','btn','Undo'); u.type='button'; u.appendChild(vh(' hiding this story')); slot.appendChild(u);
        u.addEventListener('click',function(){
          if(u.dataset.busy) return; busy(u,true); closeToast();
          post('/story/show',{id:id}).then(function(r2){ busy(u,false);
            if(!r2.ok){ fail(r2.note||'That did not stick. Try again.', u); return; }
            slot.remove(); target.hidden=false; ranked.forEach(function(n){ n.hidden=false; });
            var f=target.querySelector('.story-h a, button[data-act="hide"]'); if(f) f.focus();
            announce('Story back on the page.');
          });
        });
        target.parentNode.insertBefore(slot,target); target.hidden=true;
        ranked.forEach(function(n){ n.hidden=true; });
        u.focus(); announce('Story hidden. Undo is where it was.');
      });
      return;
    }
    if(kind==='show'){
      act(b,'/story/show',{id:Number(b.dataset.id)},function(){
        var s=el('span','shown','Back on the page when you reload.'); s.tabIndex=-1;
        b.replaceWith(s); s.focus(); announce('Story shown again.');
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
  document.querySelectorAll('.fold-btn').forEach(function(btn){
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
  var row=document.getElementById('top-row');
  if(row){
    var pos=document.querySelector('[data-pos]'), prev=document.querySelector('.top-nav [data-dir="-1"]'),
        next=document.querySelector('.top-nav [data-dir="1"]');
    function items(){ return [].slice.call(row.children).filter(function(li){ return li.getClientRects().length; }); }
    function current(){ var its=items(), best=0, d=1e9, left=row.getBoundingClientRect().left+(parseFloat(getComputedStyle(row).paddingLeft)||0);
      its.forEach(function(li,i){ var x=Math.abs(li.getBoundingClientRect().left-left); if(x<d){d=x;best=i;} }); return best; }
    var last=-1;
    function update(){ var i=current(), n=items().length; if(i===last) return; last=i;
      if(pos) pos.textContent=i+1; if(prev) prev.setAttribute('aria-disabled',String(i===0));
      if(next) next.setAttribute('aria-disabled',String(i>=n-1)); }
    var tick=null; row.addEventListener('scroll',function(){ clearTimeout(tick); tick=setTimeout(update,120); },{passive:true});
    [prev,next].forEach(function(btn){ if(!btn) return; btn.addEventListener('click',function(){
      if(btn.getAttribute('aria-disabled')==='true') return;
      var its=items(), i=current()+Number(btn.dataset.dir); if(!its[i]) return;
      row.scrollTo({left:its[i].offsetLeft-its[0].offsetLeft,behavior:reduce?'auto':'smooth'}); }); });
    row.addEventListener('focusin',function(e){ var li=e.target.closest('#top-row > li');
      if(li) li.scrollIntoView({block:'nearest',inline:'start',behavior:'auto'}); });
    update();
  }
})();
""".replace("__WRITE_HEADER__", bd.WRITE_HEADER)

TOAST = ('<p class="visually-hidden" id="live" role="status" aria-live="polite"></p>'
         '<div class="toast is-bad"><span class="toast-msg"></span>'
         '<button type="button" class="btn quiet toast-close" aria-label="Close message">Close</button></div>')


def page(title: str, body: str, jobs: dict | None) -> str:
    bar = page_frame.appbar_html("insights", jobs=jobs)
    css = FONT_CSS + page_frame.FRAME_CSS + PAGE_CSS
    return ("<!doctype html>\n<html lang=\"en-GB\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{esc(title)}</title><style>{css}</style></head><body>{bar}"
            f"<main id=\"main\" class=\"page news\" tabindex=\"-1\">{body}</main>{TOAST}"
            f"<script>{page_frame.FRAME_SCRIPT}{PAGE_SCRIPT}</script></body></html>")


def render_front(conn, config: dict | None = None, now: str | None = None) -> str:
    model = build_model(conn, config, now)
    return page("Insights · MedTech Radar", front_body(model), model["jobs"])


def render_story(conn, signal_id: int, config: dict | None = None, now: str | None = None) -> str | None:
    model = build_model(conn, config, now)
    s = find_story(model, signal_id)
    if s is None:
        return None
    title = s["headline"] if len(s["headline"]) <= 70 else s["headline"][:70].rsplit(" ", 1)[0] + "…"
    return page(f"{title} · Insights", story_body(conn, model, s), model["jobs"])


def render_section(conn, key: str, config: dict | None = None, now: str | None = None) -> str | None:
    if key not in SECTION_NAME:
        return None
    model = build_model(conn, config, now)
    return page(f"{SECTION_NAME[key]} · Insights", section_body(model, key), model["jobs"])
