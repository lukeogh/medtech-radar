#!/usr/bin/env python
"""Checks for Insights as a news page (variation B, design/insights/news).

1. The story fields: the migrations add story_type, card_summary, event_key
   and companies.watched_at; the cleaners keep a type, a card summary and a
   headline in the page's one style.
2. One story per event: reports about one company, found within seven days,
   with similar headlines, share an event_key; other companies, older
   reports, unlike headlines, "Unknown company" and a split-off report do not.
3. The writers: the watcher stores the scorer's type and card summary and
   the event; the buying window and the tripwire's first hire are typed in
   code. The backfill groups for free and types only when told to.
4. The pages, from the fictional sample week: Top stories (the lead and
   four), Most relevant 1 to 10, the four sections, More stories under 40 and
   the Hidden fold; the story page with every report, Not the same story,
   the drafts, a link to log a contact on the Watchlist (no form of its own)
   and the company; On your Watchlist leads to /watchlist/<id>; Add
   to Watchlist sends via insights and the story's signal id, and its Undo
   undo_add; a section page; a quiet week and a stopped watcher. No
   suggestions and no I did it anywhere.
5. The routes through the real server and its write guard: /story/hide and
   /story/show (the whole event), /story/split, /watchlist/add (recording
   the story it came from) and /remove (Undo puts the company back as it
   was), /touch/log, and the read-only pages and fonts.

No network beyond a loopback server this runner starts, no model, no n8n.
Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(TEST_DIR))

import radar_common as rc  # noqa: E402
import insights_page  # noqa: E402
import check_signals  # noqa: E402
import process_email  # noqa: E402
import backfill_stories  # noqa: E402
import serve_dashboard  # noqa: E402
import mocks_signals  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def ago(days: float) -> str:
    return iso(NOW - timedelta(days=days))


def put(conn, company, headline, days=1.0, relevance=60, kind="funding", source="trade-press", **cols) -> int:
    cid = rc.resolve_company(conn, company) if company else None
    url = f"https://example.org/{abs(hash((company, headline, days)))}"
    fields = {"url_hash": rc.url_hash(url), "first_seen": ago(days), "source_id": source, "company": company,
              "headline": headline, "source_url": url, "relevance": relevance, "why": "Why it matters.",
              "company_id": cid, "story_type": kind, "card_summary": "A short card summary."}
    fields.update(cols)
    names = ",".join(fields)
    cur = conn.execute(f"INSERT INTO signals ({names}) VALUES ({','.join('?' * len(fields))})", list(fields.values()))
    rc.assign_event_key(conn, cur.lastrowid)
    conn.commit()
    return cur.lastrowid


def key(conn, sid) -> str:
    return conn.execute("SELECT event_key FROM signals WHERE id = ?", (sid,)).fetchone()[0]


# ------------------------------------------------------------------ 1 and 2

def field_checks(tmp: Path) -> None:
    conn = rc.get_db(tmp / "fields.sqlite")
    sig = {r[1] for r in conn.execute("PRAGMA table_info(signals)")}
    com = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    check({"story_type", "card_summary", "event_key"} <= sig and "watched_at" in com,
          "the migrations add story_type, card_summary, event_key and companies.watched_at")
    check([rc.clean_story_type(v) for v in ("Funding", "spin-out", "Accelerator entry", "regulatory", "Nonsense", None)]
          == ["funding", "spin-off", "accelerator", "regulation", "other", "other"],
          "clean_story_type maps a model's words onto the eight types, other when unsure")
    long = "word " * 60
    summ = rc.clean_card_summary(long)
    check(summ.endswith(".") and len(summ) <= rc.CARD_SUMMARY_MAX + 1 and rc.clean_card_summary("  ") is None,
          "clean_card_summary closes with a full stop, caps the length and says None for nothing")
    check(rc.clean_headline("Acme  raises £5m.") == "Acme raises £5m",
          "clean_headline drops a closing full stop and runs of spaces")
    check(rc.clean_card_summary(": " + "y" * 150) is None,
          "a card summary that its own cut empties is None, not a crash")
    check(all(rc.is_placeholder_company(n) for n in ("Unknown company", "Unknown", "N/A", "Not stated", ""))
          and not rc.is_placeholder_company("Acme Medical"),
          "the ways a model says it could not name a company are all placeholders")
    conn.close()


def grouping_checks(tmp: Path) -> None:
    conn = rc.get_db(tmp / "group.sqlite")
    a = put(conn, "Selwick Photonics", "A university spins out Selwick Photonics with a seed round for a Raman reader", 2)
    b = put(conn, "Selwick Photonics", "Selwick Photonics raises a seed round for its Raman reader for wounds", 1)
    c = put(conn, "Selwick Photonics", "Selwick Photonics opens an office in Ghent", 1)
    d = put(conn, "Selwick Photonics", "Selwick Photonics raises a seed round for its Raman reader", 30)
    e = put(conn, "Other Photonics", "Other Photonics raises a seed round for its Raman reader for wounds", 1)
    u1 = put(conn, "Unknown company", "Guidance on software changes for devices on the market", 1)
    u2 = put(conn, "Unknown company", "Guidance on software changes for devices on the market, final", 1)
    check(key(conn, a) == f"e{a}" and key(conn, b) == key(conn, a),
          "a second report about the same company, within seven days, with a similar headline joins the first's event")
    check(key(conn, c) == f"e{c}", "an unlike headline about the same company is its own event")
    check(key(conn, d) == f"e{d}", "a like headline four weeks earlier is its own event")
    check(key(conn, e) == f"e{e}", "a like headline about another company is its own event")
    check(key(conn, u2) == f"e{u2}" and key(conn, u1) == f"e{u1}", "reports about “Unknown company” never group")
    conn.execute("UPDATE signals SET event_key = ? WHERE id = ?", (f"solo-{b}", b))
    conn.commit()
    f = put(conn, "Selwick Photonics", "Selwick Photonics raises a seed round for its Raman reader for wound care", 0.5)
    check(key(conn, f) == key(conn, a) and rc.assign_event_key(conn, b) == f"solo-{b}",
          "a split-off report keeps its solo key and nothing joins it")
    g = put(conn, "Hartwell Infusion Ltd", "Hartwell Infusion Ltd raises a £5m seed round", 2)
    h = put(conn, "Hartwell Infusion Ltd", "Hartwell Infusion Ltd appoints a new CFO", 1)
    check(key(conn, h) == f"e{h}" and key(conn, g) == f"e{g}",
          "the company's own name does not make two unrelated stories about it one event")
    i = put(conn, "Brackley Sensing", "Brackley Sensing raises £2.5m for its fibre-optic pressure sensor", 2)
    conn.execute("UPDATE signals SET acknowledged_at = ? WHERE id = ?", (ago(1), i))
    conn.commit()
    j = put(conn, "Brackley Sensing", "Brackley Sensing raises £2.5m to scale its fibre-optic pressure sensor", 0.5)
    check(key(conn, j) == key(conn, i) and conn.execute("SELECT acknowledged_at FROM signals WHERE id = ?",
                                                        (j,)).fetchone()[0],
          "a new report of a story a human hid joins it hidden, so the story does not come back")
    k = put(conn, "Brackley Sensing", "Brackley Sensing raises £2.5m round for its pressure sensor", 3)
    check(key(conn, k) == f"e{k}", "only earlier reports are joined, so store time and the backfill agree")
    conn.close()


# ------------------------------------------------------------------ 3

def writer_checks(tmp: Path) -> None:
    db = tmp / "writers.sqlite"
    conn = rc.get_db(db)
    config = rc.load_config()
    blocks = [{"type": "text", "text": check_signals.RUBRIC_PATH.read_text(encoding="utf-8")}]
    result = {"items_in": 0, "items_new": 0, "duplicates": 0, "pushed": 0, "payloads": [], "notes": [],
              "usage": {}}
    item = {"source_id": "imec-press", "source_name": "imec", "url": "https://example.org/a",
            "headline": "Lumo Diagnostics raises a seed round as a Ghent university spin-off for its IVD reader.",
            "text": "Spin-off with a seed round for a photonic IVD reader, medical device, no software team."}
    check_signals.process_item(item, conn, config, blocks, mocks_signals.mock_signal_scorer, False, result)
    row = conn.execute("SELECT story_type, card_summary, event_key, headline FROM signals").fetchone()
    check(row is not None and row["story_type"] == "spin-off" and row["card_summary"]
          and row["event_key"] and not row["headline"].endswith("."),
          "the watcher stores the scorer's type and card summary, a clean headline, and the event")
    item2 = dict(item, url="https://example.org/b",
                 headline="Lumo Diagnostics closes a seed round for its IVD reader as a Ghent university spin-off")
    check_signals.process_item(item2, conn, config, blocks, mocks_signals.mock_signal_scorer, False, result)
    keys = [r[0] for r in conn.execute("SELECT event_key FROM signals ORDER BY id")]
    check(len(keys) == 2 and keys[0] == keys[1], "a second source for the same event lands in the same story")
    cid = rc.resolve_company(conn, "Hartwell Infusion Ltd")
    wrote = process_email.record_buying_window(conn, "Hartwell Infusion Ltd", "Head of Software Quality, Class II",
                                               "https://example.org/job", rc.url_hash("https://example.org/job"),
                                               rc.now_iso(), cid)
    conn.commit()
    bw = conn.execute("SELECT story_type, card_summary, headline, event_key FROM signals"
                      " WHERE source_id = 'job-advert'").fetchone()
    check(wrote and bw["story_type"] == "buying-window" and bw["card_summary"] and bw["event_key"]
          and bw["headline"].startswith("Hartwell Infusion Ltd advertises for a Head of Software Quality"),
          "a buying window is typed, summarised and headlined in code")
    # The backfill: rows stored before the deploy have no key and no type.
    old = put(conn, "Lumo Diagnostics", "Lumo Diagnostics raises a seed round for its IVD reader as a spin-off", 3,
              kind=None, card_summary=None, event_key=None)
    conn.execute("UPDATE signals SET story_type = NULL, card_summary = NULL, event_key = NULL WHERE id = ?", (old,))
    conn.execute("UPDATE signals SET event_key = NULL WHERE company = 'Lumo Diagnostics'")
    tw = put(conn, "Kern Medical", "Kern Medical advertises its first quality manager", 2, kind=None, source="qa-tripwire")
    conn.execute("UPDATE signals SET story_type = NULL WHERE id = ?", (tw,))
    conn.commit()
    conn.close()
    env = dict(os.environ, RADAR_MOCK="")
    env.pop("RADAR_MOCK")
    dry = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "backfill_stories.py"), "--db", str(db)],
                         capture_output=True, text=True, env=env, timeout=120)
    conn = rc.get_db(db)
    row = conn.execute("SELECT story_type, event_key FROM signals WHERE id = ?", (old,)).fetchone()
    lumo = {r[0] for r in conn.execute("SELECT event_key FROM signals WHERE company = 'Lumo Diagnostics'")}
    tws = conn.execute("SELECT story_type FROM signals WHERE id = ?", (tw,)).fetchone()[0]
    check(dry.returncode == 0 and "Dry run" in dry.stdout and row["story_type"] is None
          and lumo == {f"e{old}"} and tws == "first-hire",
          "the backfill groups old rows in order of arrival for free, types the code's hiring rows, "
          "and makes no call without --live")
    conn.close()
    again = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "backfill_stories.py"), "--db", str(db)],
                           capture_output=True, text=True, env=env, timeout=120)
    check(again.returncode == 0 and "0 rows were given a key" in again.stdout, "a second run changes no keys")
    live_mock = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "backfill_stories.py"), "--mock"],
                               capture_output=True, text=True, timeout=120)
    check(live_mock.returncode == 2, "the mock refuses to run against the live database")
    mocked = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "backfill_stories.py"), "--db", str(db),
                             "--mock"], capture_output=True, text=True, timeout=120)
    conn = rc.get_db(db)
    row = conn.execute("SELECT story_type, card_summary FROM signals WHERE id = ?", (old,)).fetchone()
    check(mocked.returncode == 0 and row["story_type"] in rc.STORY_TYPES and row["card_summary"],
          "the paid pass, here with the mock, fills type and card summary on rows that had none")
    # A row the scorer could not read, scored later by rescore.py, gets everything a row scored on arrival has.
    conn.execute("INSERT INTO signals (url_hash, first_seen, source_id, headline, summary, source_url, why)"
                 " VALUES (?,?,?,?,?,?,?)", (rc.url_hash("https://example.org/late"), rc.now_iso(), "imec-press",
                                            "Lumo Diagnostics raises a seed round for its IVD reader, a spin-off",
                                            "Spin-off with a seed round for an IVD reader, medical device.",
                                            "https://example.org/late", "scorer returned unparseable output"))
    conn.commit()
    conn.close()
    late = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "rescore.py"), "--db", str(db), "--mock",
                           "--quiet"], capture_output=True, text=True, timeout=120)
    conn = rc.get_db(db)
    row = conn.execute("SELECT story_type, card_summary, company_id, event_key FROM signals"
                       " WHERE source_url = 'https://example.org/late'").fetchone()
    check(late.returncode == 0 and row["story_type"] and row["card_summary"] and row["company_id"]
          and row["event_key"] == f"e{old}",
          "a row rescored later gets its type, summary, company and event like one scored on arrival")
    conn.close()


# ------------------------------------------------------------------ 4

def seeded(tmp: Path, week: str) -> Path:
    db = tmp / f"seed-{week}-{len(list(tmp.glob('seed-*')))}.sqlite"
    out = subprocess.run([sys.executable, str(TEST_DIR / "seed_insights_sample.py"), "--db", str(db), "--week", week],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return db


def ro(db: Path):
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def page_checks(tmp: Path) -> None:
    db = seeded(tmp, "typical")
    conn = ro(db)
    before = conn.execute("SELECT COUNT(*), SUM(LENGTH(COALESCE(acknowledged_at,''))) FROM signals").fetchone()
    model = insights_page.build_model(conn)
    html = insights_page.render_front(conn)
    # The shared look (owner, 18 Nov): title row, contents, Top stories as
    # cards, the week's other stories and three sections as rows, More stories.
    head = re.search(r'<header class="ins-head">.*?</header>', html, re.S).group(0)
    check('<span class="gchip g-story"' in head and "<h1>Insights</h1>" in head
          and re.search(r'class="sec-count upd"><span>Updated <time datetime="[^"]+" data-hm>\d\d:\d\d</time></span></span>', head)
          and 'class="plain ins-health" href="/health">' in head,
          "the title row: amber chip, Insights, Updated HH:MM, and the health link at the right")
    toc = re.search(r'<nav class="ins-toc".*?</nav>', html, re.S).group(0)
    order = re.findall(r'href="#([a-z-]+)">([^<]+)<span class="visually-hidden">, </span><span class="toc-n">(\d+)', toc)
    check([o[1] for o in order] == ["Top stories", "Other stories this week", "Funding",
                                    "Spin-offs and accelerators", "Regulation and market news", "More stories"]
          and all(f'id="{o[0]}"' in html for o in order) and toc.count('<span class="dv-i">') == 6
          and '<span class="dv"><span class="dv-in">' in toc and "fact-rule" not in toc,
          "the contents row links each section with its count, in order, the shared divider between")
    tops = re.findall(r'<article class="card g-story glow-faint tcard[^"]*".*?</article>', html, re.S)
    check(len(tops) == len(model["top"]) and all(s["relevance"] >= 40 and not s["hidden"] for s in model["top"])
          and 'tcard is-first' in tops[0] and 'tcard is-first' not in "".join(tops[1:]),
          "Top stories: a card for every open story at 40 or more, the first one larger")
    lead = model["top"][0]
    check(f'href="{lead["url"]}" target="_blank" rel="noopener">' in tops[0] and "and 2 more sources" in tops[0]
          and 'class="tmove-l">Next move</p>' in tops[0] and 'class="treason"' in tops[0],
          "a top story's headline goes straight to the article, with its sources, reason and Next move")
    check('data-act="done"' in tops[0] and ">Done<" in tops[0] and "Hide" not in html,
          "Done replaces Hide")
    check(".th{font:600 16px/24px" in html and ".tcard.is-first .th{font-size:20px;line-height:28px}" in html
          and ".tmove-l{color:var(--group-story);font-size:13px;line-height:20px;font-weight:600}" in html,
          "headlines 16px/600, the first 20px/600, the Next move label 13px/600 amber")
    other = re.search(r'<section class="ins-sec" id="other-stories".*?</section>', html, re.S).group(0)
    check(other.count('<li class="srow"') == len(model["other"]) == 5
          and not {s["key"] for s in model["other"]} & {s["key"] for s in model["top"]},
          "Other stories this week: five rows, none of them in Top stories")
    for name in ("Funding", "Spin-offs and accelerators", "Regulation and market news"):
        check(re.search(rf'<h2 class="sec-title" id="[a-z-]+-h">{name}</h2><span class="sec-count">\d+</span>', html),
              f"the {name} section is on the page with its count, even at 0")
    hiring = [r for r in conn.execute("SELECT id, headline FROM signals WHERE story_type IN ('buying-window', 'first-hire')")]
    check(len(hiring) >= 3 and "Hiring and buying windows" not in html
          and not any(rc.clean_headline(r["headline"]) in html for r in hiring)
          and all(s["type"] not in rc.HIRING_TYPES for s in model["stories"]),
          "hiring is not news: no buying window or first hire is anywhere on the page, folds included")
    check(all(insights_page.render_story(conn, r["id"]) is None for r in hiring)
          and insights_page.render_section(conn, "hiring") is None,
          "a hiring story has no story page and hiring has no section page")
    check('id="more-list" hidden' in html and f"Show all {len(model['below'])} headlines" in html
          and all(s["relevance"] < 10 for s in model["below"]),
          "More stories folds everything under 10 behind Show all N headlines")
    check("Done this week" in html and "Put back" in html, "the stories marked Done this week can be put back")
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    text = re.sub(r"<[^>]+>", " ", text)
    check(not re.search(r"Relevance \d|Found \d|relevance 40 or more|Times are shown|Check the source", text)
          and "page-foot" not in html and "tally" not in html and "\u00b7" not in text and "\u2713" not in text,
          "no relevance numbers, found dates, counts line, footnotes, middle dots or ticks on the front page")
    check("IBM Plex Serif" not in html and "font-serif:var(--font-sans)" in html,
          "the serif is gone: no font loads, and the story pages read the shared sans")
    check('[hidden]{display:none!important}' in html, "hidden parts of the page are hidden whatever their display rule")
    check("toast-undo" not in html and 'id="live" role="status"' in html,
          "the message bar is for failures only, and an always-present live region carries the rest")
    check(not re.search(r"I did it|Suggestion|Do today", html, re.I),
          "no suggestions and no I did it on the front page")
    check("Add company to Watchlist" in html and 'class="wpill" href="/watchlist/' in html,
          "a company off the Watchlist offers Add company to Watchlist and one on it the On your Watchlist pill")
    check("X-Radar-Request" in html, "the page's writes carry the write header")
    check(re.search(r'<a class="plain" href="/watchlist/\d+">', html) and "/company/" not in html,
          "company names link to their record on the Watchlist")
    check("@media (max-width:767.98px)" in html and "repeat(auto-fill,minmax(min(100%,440px),1fr))" in html
          and ".story-actions{display:flex;flex-wrap:wrap" in html,
          "on a phone the cards are one column and the actions wrap under the text")
    sid = model["top"][0]["id"]
    story = insights_page.render_story(conn, sid)
    check(story.count('<li><span class="src">') == 3 and story.count("Not the same story") == 2,
          "the story page lists every report and offers Not the same story on all but the lead")
    lead_cid = model["top"][0]["company_id"]
    check("Drafts to copy" in story and "Copy the comment" in story,
          "the story page holds the drafts with Copy")
    check("logform" not in story and "<form" not in story and "<textarea" not in story
          and f'href="/watchlist/{lead_cid}">Log a contact on the Watchlist' in story,
          "the story page has no Log a contact form of its own and links to the Watchlist to log one")
    check("About Selwick Photonics" in story and "Enschede" in story
          and f'class="rec" href="/watchlist/{lead_cid}"' in story and "/company/" not in story,
          "the story page shows the company's facts and links its record on the Watchlist")
    off_story = next(s for s in model["stories"] if s["company_id"] and not s["watched"])
    check(f'data-act="watch" data-cid="{off_story["company_id"]}" data-signal="{off_story["id"]}"' in html,
          "Add to Watchlist carries the story's signal id")
    check("act(b,'/watchlist/add',add," in html and "via:'insights'" in html and "add.signal_id=sid" in html
          and "'/watchlist/remove',{company_id:cid2,undo_add:true,since:since[cid2]||''}" in html
          and "since[cid]=res.already?null:res.watched_at;" in html,
          "the script sends via insights and the signal id, and its Undo sends undo_add with the add's own stamp")
    check('class="wpill" href="/watchlist/' in html and "a.href='/watchlist/'+cid" in html
          and "/company/" not in html,
          "On your Watchlist leads to the company on the Watchlist, drawn or added, never /company/")
    any_report = model["top"][0]["reports"][-1]["id"]
    check(insights_page.render_story(conn, any_report) is not None, "any report's id opens its story")
    hw = next(s for s in model["stories"] if s["company"] == "PDX Diagnostics")
    hstory = insights_page.render_story(conn, hw["id"])
    check("Roles on Jobs" in hstory and "/jobs/" in hstory and "Last contact" in hstory,
          "a company with a role links it on Jobs and says when it was last contacted")
    unknown = next(s for s in model["stories"] if s["type"] == "regulation")
    check("Add to Watchlist" not in insights_page.render_story(conn, unknown["id"]).split("About the company")[0]
          and unknown["company"] == "Regulation", "a story with no company shows its type and offers no Watchlist")
    sec = insights_page.render_section(conn, "funding")
    check(sec and "The last seven days" in sec and "Earlier" in sec and insights_page.render_section(conn, "x") is None,
          "a section page lists its stories by week, and an unknown section is not a page")
    after = conn.execute("SELECT COUNT(*), SUM(LENGTH(COALESCE(acknowledged_at,''))) FROM signals").fetchone()
    check(tuple(before) == tuple(after), "rendering writes nothing")
    conn.close()
    # A week with nothing at 40: Top stories is empty and says so with 0;
    # the week's stories are under Other stories and the sections.
    low = seeded(tmp, "typical")
    w = sqlite3.connect(low)
    w.execute("UPDATE signals SET relevance = MIN(relevance, 22)")
    w.commit()
    w.close()
    lconn = ro(low)
    lmodel = insights_page.build_model(lconn)
    lhtml = insights_page.render_front(lconn)
    check(not lmodel["top"] and "No top stories." in lhtml
          and '<h2 class="sec-title" id="top-stories-h">Top stories</h2><span class="sec-count">0</span>' in lhtml
          and len(lmodel["other"]) == 5 and lmodel["other"][0]["relevance"] == 22,
          "a week with nothing at 40 shows Top stories at 0, and its five best under Other stories")
    funding = next(sec for sec in lmodel["sections"] if sec["key"] == "funding")
    check(len(funding["stories"]) == 5 and "Show the other 2" in lhtml and 'id="funding-rest" hidden' in lhtml
          and len(funding["earlier"]) == 2 and "From earlier weeks 2" in lhtml and 'id="funding-earlier" hidden' in lhtml,
          "a section shows three rows, folds the rest behind Show the other N and older ones behind From earlier weeks N")
    check(all(s["relevance"] < 10 for s in lmodel["below"])
          and all(s["relevance"] >= 10 for sec in lmodel["sections"] for s in sec["stories"]),
          "only stories under the floor of 10 are folded under More stories, and none of them is in a section")
    lconn.close()
    quiet = ro(seeded(tmp, "quiet"))
    qmodel = insights_page.build_model(quiet)
    qhtml = insights_page.render_front(quiet)
    earlier = sum(len(sec["earlier"]) for sec in qmodel["sections"])
    check(qhtml.count('<span class="sec-count">0</span>') >= 3
          and (not earlier or "From earlier weeks" in qhtml),
          "a quiet week shows each section's title at 0 and its earlier weeks behind From earlier weeks N")
    quiet.close()
    stale = ro(seeded(tmp, "stale"))
    shtml = insights_page.render_front(stale)
    check("The watcher has stopped" in shtml and "Last finished run" in shtml
          and "Anything reported since then" not in shtml,
          "a stopped watcher is said at the top with its last run, and no explanation")
    stale.close()


# ------------------------------------------------------------------ 5

@contextlib.contextmanager
def serving(db):
    saved = (serve_dashboard.ARGS, serve_dashboard.GUARD, serve_dashboard.Handler.log_message,
             serve_dashboard._MIGRATED)
    serve_dashboard.ARGS = argparse.Namespace(db=str(db), push=False, host="127.0.0.1", port=0)
    serve_dashboard.GUARD = serve_dashboard.WriteGuard("enforce", serve_dashboard.LOOPBACK_NAMES)
    serve_dashboard.Handler.log_message = lambda *_a, **_k: None
    serve_dashboard._MIGRATED = False
    server = serve_dashboard.RadarServer(("127.0.0.1", 0), serve_dashboard.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        (serve_dashboard.ARGS, serve_dashboard.GUARD, serve_dashboard.Handler.log_message,
         serve_dashboard._MIGRATED) = saved


def call(port, method, path, body=None, header=True):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    headers = {"Content-Type": "application/json", "Host": f"127.0.0.1:{port}"}
    if header:
        headers["X-Radar-Request"] = "1"
    try:
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def route_checks(tmp: Path) -> None:
    db = seeded(tmp, "typical")
    conn = ro(db)
    model = insights_page.build_model(conn)
    lead = model["top"][0]
    ids = [r["id"] for r in lead["reports"]]
    off = next(s for s in model["stories"] if s["company_id"] and not s["watched"])
    conn.close()
    with serving(db) as port:
        st, body = call(port, "GET", "/insights")
        check(st == 200 and b"Top stories" in body, "GET /insights is the news page")
        check(call(port, "GET", f"/insights/story/{ids[1]}")[0] == 200, "GET /insights/story/<id> opens a story")
        check(call(port, "GET", "/insights/story/999999")[0] == 404 and call(port, "GET", "/insights/section/x")[0] == 404
              and call(port, "GET", "/insights/what")[0] == 404, "an unknown story, section or path is a 404")
        check(call(port, "GET", "/static/fonts/../../db/radar.sqlite")[0] == 404
              and call(port, "GET", "/static/fonts/other.woff2")[0] == 404,
              "the font route serves only the three named files")
        fonts = [call(port, "GET", f"/static/fonts/{name}") for name in insights_page.FONT_FILES]
        check(all(st == 200 and body[:4] == b"wOF2" for st, body in fonts)
              and call(port, "GET", "/static/fonts/OFL.txt")[0] == 404,
              "the three IBM Plex Serif files are in the repo and served as woff2, the licence beside them is not")
        st, _ = call(port, "POST", "/story/hide", {"id": ids[0]}, header=False)
        check(st == 403, "a write without the header is refused by the guard")
        check(call(port, "POST", "/story/hide", {"id": True})[0] == 400, "an id that is not an integer is refused")
        st, body = call(port, "POST", "/story/hide", {"id": ids[0]})
        c = ro(db)
        hidden = [r[0] for r in c.execute(f"SELECT acknowledged_at FROM signals WHERE id IN ({','.join(map(str, ids))})")]
        c.close()
        check(st == 200 and json.loads(body)["reports"] == 3 and all(hidden),
              "Hide hides every report of the event, so it cannot come back through a second source")
        check(call(port, "POST", "/story/hide", {"id": ids[0]})[0] == 409, "hiding a hidden story says so")
        st, body = call(port, "POST", "/story/show", {"id": ids[2]})
        c = ro(db)
        shown = [r[0] for r in c.execute(f"SELECT acknowledged_at FROM signals WHERE id IN ({','.join(map(str, ids))})")]
        c.close()
        check(st == 200 and not any(shown), "Show again (or Undo) brings back every report")
        st, _ = call(port, "POST", "/story/split", {"id": ids[2]})
        c = ro(db)
        k = c.execute("SELECT event_key FROM signals WHERE id = ?", (ids[2],)).fetchone()[0]
        n = insights_page.find_story(insights_page.build_model(c), ids[0])["count"]
        c.close()
        check(st == 200 and k == f"solo-{ids[2]}" and n == 2, "Not the same story splits one report off for good")
        st, _ = call(port, "POST", "/watchlist/add", {"company_id": off["company_id"], "via": "insights",
                                                     "signal_id": off["id"]})
        c = ro(db)
        w = c.execute("SELECT watched_at, watched_via, watched_signal_id FROM companies WHERE id = ?",
                      (off["company_id"],)).fetchone()
        c.close()
        check(st == 200 and w["watched_at"] and w["watched_via"] == "insights"
              and w["watched_signal_id"] == off["id"],
              "Add to Watchlist stamps the company, via insights, with the story it came from")
        st0, _ = call(port, "POST", "/watchlist/remove", {"company_id": off["company_id"], "undo_add": True})
        check(st0 == 409, "an Undo without the add's own stamp is refused, so it never takes off an older add")
        st, _ = call(port, "POST", "/watchlist/remove", {"company_id": off["company_id"], "undo_add": True,
                                                          "since": w["watched_at"]})
        c = ro(db)
        w = c.execute("SELECT watched_at, watched_via, watched_signal_id, unwatched_at, watched_was"
                      " FROM companies WHERE id = ?", (off["company_id"],)).fetchone()
        c.close()
        check(st == 200 and w["watched_at"] is None and w["watched_via"] is None
              and w["watched_signal_id"] is None and w["unwatched_at"] is None and w["watched_was"] is None,
              "its Undo puts the company back as it was, never marked as removed")
        check(call(port, "POST", "/watchlist/add", {"company_id": 999999})[0] == 409, "an unknown company is refused")
        st, _ = call(port, "POST", "/touch/log", {"company_id": off["company_id"], "channel": "comment",
                                                 "note": "Commented on the post."})
        bad = call(port, "POST", "/touch/log", {"company_id": off["company_id"], "channel": "phone", "note": ""})[0]
        c = ro(db)
        t = c.execute("SELECT channel, note, company_id FROM touches ORDER BY id DESC LIMIT 1").fetchone()
        c.close()
        check(st == 200 and t["channel"] == "comment" and t["company_id"] == off["company_id"] and bad == 409,
              "Log a contact writes one touch for the company and refuses a channel the log does not know")


def main() -> int:
    ap = argparse.ArgumentParser(description="Insights news page checks")
    ap.add_argument("--db", help="accepted for suite symmetry, unused")
    ap.parse_args()
    os.environ["RADAR_MOCK"] = "1"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        for section in (field_checks, grouping_checks, writer_checks, page_checks, route_checks):
            try:
                section(tmp_path)
            except Exception as err:  # noqa: BLE001  one section stopping is a failure
                import traceback
                traceback.print_exc()
                check(False, f"{section.__name__} stopped on {type(err).__name__}: {err}")
    print()
    if failures:
        print(f"FAIL. {len(failures)} check(s) failed.")
        for label in failures:
            print("  - " + label)
        return 1
    print("PASS. Every Insights page check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
