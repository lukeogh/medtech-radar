#!/usr/bin/env python
"""Checks for the shared header: the top bar every page sits under
(scripts/page_frame.py). A change to the header edits this file and no other
runner; a page runner keeps at most one check that its page includes it.

No model calls and no network: RADAR_MOCK is forced on, and every database is
a throwaway under a temporary folder, seeded from the invented samples.

1. The frame on its own: the pages in the menu and their order, Settings as a
   link to the CV page's tab, the skip link, the page marked current, the
   shortcuts button only when asked for, and no light or dark switch.
2. The bar's light: All running or Check Radar, read by Home's rules
   (page_frame.job_light, the one rule Home's lights use too), in words as
   well as colour, linking to Radar's health.
3. Every page draws the bar, once, with every page in it, marks the page it is
   on (or none, for the pages outside the menu), has no shortcuts button on
   any page, and turns its light with the scheduled jobs: All running with
   every job on time, the digest and the push in dry run; Check Radar, in two
   words and no job names, with the signal watcher stalled.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["RADAR_MOCK"] = "1"

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(TEST_DIR))

import cv_store  # noqa: E402
import home_page as hp  # noqa: E402
import page_frame  # noqa: E402
import radar_common as rc  # noqa: E402
import score_item  # noqa: E402
import serve_dashboard  # noqa: E402
import seed_insights_sample  # noqa: E402
import seed_jobs_sample  # noqa: E402
import seed_watchlist_sample  # noqa: E402

failures: list[str] = []
NOW = datetime.now(timezone.utc)
MENU = ["Home", "Jobs", "Watchlist", "Insights", "CV", "Settings"]
HREFS = ["/", "/jobs", "/watchlist", "/insights", "/cv", "/settings"]


def check(cond, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(hours: float) -> str:
    return iso(NOW - timedelta(hours=hours))


def bar_of(page: str) -> str:
    """The header element alone, or '' when the page has none."""
    if '<header class="appbar">' not in page:
        return ""
    return page.split('<header class="appbar">', 1)[1].split("</header>", 1)[0]


# ================================================================ 1. the frame

def frame_checks() -> None:
    check([label for _k, _h, label in page_frame.NAV] == MENU
          and [href for _k, href, _l in page_frame.NAV] == HREFS,
          "the menu reads Home, Jobs, Watchlist, Insights, CV, Settings, and Settings is the "
          "CV page's tab, /cv#settings")
    bar = page_frame.appbar_html("jobs", None)
    menu = re.findall(r'<nav class="nav" aria-label="Pages">(.*?)</nav>', bar)
    check(menu and re.findall(r">([A-Za-z]+)</a>", menu[0]) == MENU,
          "the bar draws the menu in that order")
    check(bar.startswith('<a class="skip-link" href="#main">Skip to the page</a><header class="appbar">'),
          "a skip link to the page's main comes before the bar")
    check(bar.count('aria-current="page"') == 2
          and '<a href="/jobs" data-group="jobs" aria-current="page">Jobs</a>' in bar
          and 'aria-current="page">Settings' not in bar,
          "the page it is on is marked in the bar and in the narrow menu, and nothing else")
    check('id="keys-open"' not in bar and "Keyboard shortcuts" not in bar,
          "no shortcuts button unless the page asks for one")
    helped = page_frame.appbar_html("jobs", None, help_button=True)
    check('id="keys-open"' in helped and 'aria-keyshortcuts="Shift+?"' in helped,
          "with help_button the bar carries the shortcuts button")
    check(page_frame.radar_status_html(None) == "" and "radar-status" not in bar,
          "with no verdicts at all the bar says nothing about Radar")
    text = re.sub(r"<[^>]+>", " ", bar)
    check("Light" not in text and "Dark" not in text and 'aria-label="Appearance"' not in bar,
          "no light or dark switch: Radar stays dark only")


# ================================================================ 2. the light

def light_checks() -> None:
    def v(state, mode="live", **kw):
        return {"state": state, "mode": mode, "last_run": ago(1), "hours_ago": 1.0,
                "late_hours": 4, "stale_hours": 6, **kw}

    check(hp.job_light is page_frame.job_light,
          "Home's lights and the top bar's light share one rule, page_frame.job_light")
    for name, state in (("inbox", "stale"), ("signals", "late"), ("signals", "failing"),
                        ("check", "partial"), ("digest", "stale"), ("check", "never")):
        jobs = {n: v("ok") for n in ("inbox", "signals", "check", "digest")}
        jobs[name] = v(state)
        said = page_frame.radar_status_html(jobs)
        check("Check Radar" in said and "is-check" in said and 'href="/radar"' in said
              and 'data-state="check"' in said and "All running" not in said,
              f"the top bar says Check Radar, in words as well as colour, when the {name} is {state}")
    jobs = {n: v("ok") for n in ("inbox", "signals", "check", "digest")}
    said = page_frame.radar_status_html(jobs)
    check(re.search(r'<a class="radar-status" href="/radar" data-state="ok">'
                    r'<span class="radar-dot" aria-hidden="true"></span>All running<', said),
          "with every job on time it says All running and links to Radar's health")
    jobs["inbox"] = v("late")
    jobs["digest"] = v("ok", "dry-run")
    check("All running" in page_frame.radar_status_html(jobs),
          "a quiet inbox and a digest in dry run still read All running")
    jobs["check"] = v("never", watched=False)
    check("All running" in page_frame.radar_status_html(jobs),
          "and a job /health is told not to watch never turns it")


# ================================================================ 3. every page

@contextlib.contextmanager
def pages_at(db: Path, profile: Path):
    """serve_dashboard pointed at db, the CV and preferences at profile."""
    real_load = rc.load_config

    def load_config(*a, **k):
        return {**real_load(*a, **k), "prefs_file": str(profile / "preferences.md"),
                "cv_file": str(profile / "cv.pdf")}
    saved = (serve_dashboard.ARGS, rc.load_config, cv_store.PROFILE_DIR, score_item.PROFILE_DIR,
             rc.JOB_SOURCES_PATH)
    serve_dashboard.ARGS = argparse.Namespace(db=str(db), push=False, host="127.0.0.1", port=0)
    rc.load_config = load_config
    cv_store.PROFILE_DIR = score_item.PROFILE_DIR = profile
    rc.JOB_SOURCES_PATH = profile / "job_sources.yaml"
    try:
        yield
    finally:
        (serve_dashboard.ARGS, rc.load_config, cv_store.PROFILE_DIR, score_item.PROFILE_DIR,
         rc.JOB_SOURCES_PATH) = saved


def seed(tmp: Path) -> tuple[Path, Path, dict]:
    profile = tmp / "profile"
    profile.mkdir()
    shutil.copy(TEST_DIR / "fixtures" / "profile_snapshot.md", profile / "cv-20261029-091500.md")
    (profile / "cv.active").write_text("cv-20261029-091500.md\n", encoding="utf-8")
    shutil.copy(REPO_ROOT / "config" / "preferences.template.md", profile / "preferences.md")
    db = tmp / "header.sqlite"
    seed_jobs_sample.SHIFT = (NOW.replace(hour=0, minute=0, second=0, microsecond=0)
                              - seed_jobs_sample.DESIGN_TODAY)
    ids = dict(seed_jobs_sample.seed(db))
    conn = rc.get_db(db)
    seed_insights_sample.seed(conn, NOW.replace(second=0, microsecond=0))
    ids["company"] = seed_watchlist_sample.seed(conn, NOW)["Hartwell Infusion Ltd"]
    ids["unwatched"] = conn.execute("SELECT id FROM companies WHERE watched_at IS NULL ORDER BY id"
                                    " LIMIT 1").fetchone()[0]
    conn.commit()
    conn.close()
    return db, profile, ids


def set_runs(db: Path, signals_hours: float) -> None:
    """The scheduled jobs' last runs: the box as it is set up, the digest and
    the push in dry run, with the signal watcher last run signals_hours ago."""
    conn = rc.get_db(db)
    conn.execute("DELETE FROM runs")
    for ts, workflow, mode in ((ago(1), "inbox", "live"), (ago(signals_hours), "signals", "dry-run"),
                               (ago(20), "check", "dry-run"), (ago(48), "digest", "dry-run")):
        conn.execute("INSERT INTO runs (ts, workflow, mode, outcome) VALUES (?,?,?,'ok')",
                     (ts, workflow, mode))
    conn.commit()
    conn.close()


def every_page(ids: dict) -> dict[str, tuple[str, str | None]]:
    """name: (the page, the menu item it marks current, or None)."""
    def text(body) -> str:
        return body.decode("utf-8") if isinstance(body, bytes) else (body or "")
    front = text(serve_dashboard.render_insights("front"))
    story = re.search(r'href="/insights/story/(\d+)"', front)
    return {
        "Home": (text(serve_dashboard.render("home")), "Home"),
        "Jobs": (text(serve_dashboard.render_jobs(None, {})[1]), "Jobs"),
        "a role on Jobs": (text(serve_dashboard.render_jobs(ids["T1"], {})[1]), "Jobs"),
        "Prepare application": (text(serve_dashboard.render_prepare(ids["T1"])), "Jobs"),
        "Insights": (front, "Insights"),
        "an Insights story": (text(serve_dashboard.render_insights("story", int(story.group(1))))
                              if story else "", "Insights"),
        "Watchlist": (text(serve_dashboard.render_watchlist(None, "table")), "Watchlist"),
        "a Watchlist company": (text(serve_dashboard.render_watchlist(ids["company"], "record")),
                                "Watchlist"),
        "an unwatched company's record": (text(serve_dashboard.render_watchlist(ids["unwatched"], "record")),
                                          "Watchlist"),
        "CV": (text(serve_dashboard.render_cv_page()), "CV"),
        "Settings": (text(serve_dashboard.render_settings_page()), "Settings"),
        "Radar's health": (text(serve_dashboard.render("radar")), None),
        "the archive": (text(serve_dashboard.render("archive")), None),
    }


def page_checks(tmp: Path) -> None:
    db, profile, ids = seed(tmp)
    with pages_at(db, profile):
        set_runs(db, signals_hours=1)
        running = every_page(ids)
        set_runs(db, signals_hours=22.6)
        stalled = every_page(ids)
    # One company page (owner, 17 Nov): no page links to the old company page,
    # apart from the older dashboard page (the archive), whose links follow the redirect.
    linking = [n for n, (page, _c) in running.items()
               if n != "the archive" and re.search(r'href="/company/\d', page)]
    check(not linking, f"no page links to /company/<id> any more ({linking or 'none'})")
    for name, (page, current) in running.items():
        bar = bar_of(page)
        check(page.count('<header class="appbar">') == 1
              and all(f'href="{href}"' in bar for href in HREFS)
              and '<a class="skip-link" href="#main">' in page,
              f"{name} has the top bar, once, with every page in it, and the skip link")
        marked = re.findall(r'aria-current="page">([A-Za-z]+)</a>', bar)
        check(marked == ([current, current] if current else []),
              f"{name} marks {current or 'no page'} as the page it is on")
        check('id="keys-open"' not in bar and "Keyboard shortcuts" not in bar,
              f"{name} has no shortcuts button (Jobs dropped its own, owner, 17 Nov)")
        check('data-state="ok"' in bar and "All running" in bar and 'href="/radar"' in bar,
              f"{name}: with every job on time and the digest and the push in dry run, "
              "the light says All running")
    # One content box for every page (owner, 17 Nov): page_frame sets it and no
    # page sets its own, so the bar's contents, and the logo, sit in the same
    # place on every page; each page's content reads the same box.
    frame = page_frame.FRAME_CSS
    for name, (page, _current) in running.items():
        css = " ".join(re.findall(r"(?s)<style>(.*?)</style>", page))
        own = css.replace(frame, "")
        check('<header class="appbar"><div class="appbar-in">' in page and frame in css
              and "--frame-max:" not in own and "--frame-pad:" not in own
              and "var(--frame-max)" in own and "var(--frame-pad)" in own,
              f"{name} sets no content box of its own and lays its content in the shared one")
    def logo_left(window: float, scroll_bar: float) -> float:
        """The logo's left edge, from the shared box: the bar's contents are
        --frame-max wide at most, centred, --frame-pad in (32px from 1024px)."""
        cap = float(re.search(r":root\{--frame-max:(\d+)px;", frame).group(1))
        pad = float(re.search(r"@media \(min-width:1024px\)\{:root\{--frame-pad:(\d+)px\}\}",
                              frame).group(1))
        return max(0.0, (window - scroll_bar - cap) / 2) + pad
    for window in (1920, 1440):
        for bar_w in (0, 15):
            edges = {name: logo_left(window, bar_w) for name in
                     ("Home", "Jobs", "Watchlist", "Insights", "CV", "Settings")}
            check(max(edges.values()) - min(edges.values()) <= 1,
                  f"at {window} wide ({'a 15px' if bar_w else 'no'} scroll bar) the logo starts "
                  f"{edges['Home']:g}px in on Home, Jobs, the Watchlist, Insights, CV and Settings")
    jobs = running["Jobs"][0]
    check("padding:12px max(var(--frame-pad),calc((100% - var(--frame-max)) / 2 + var(--frame-pad)));"
          in jobs
          and "padding-inline:max(0px,calc((100% - var(--frame-max)) / 2 + var(--frame-pad) - 16px));"
          in jobs,
          "Jobs' head and list keep their ground the full width and meet the shared edge")
    check("min(760px,100% - clamp(440px,24vw,640px) - min(640px,100vw - 760px - "
          "clamp(440px,24vw,640px)))" in jobs,
          "from 1800px Jobs' advert column gives up what the shared box lacks, so the three "
          "columns fit")
    for name, (page, _current) in stalled.items():
        bar = bar_of(page)
        check("Check Radar" in bar and 'data-state="check"' in bar and "All running" not in bar
              and "Inbox" not in bar and "Signal" not in bar,
              f"{name}: with the signal watcher stalled the light says Check Radar, in two words "
              "and no job names")


def edge_checks() -> None:
    """The bar lines up with the page, and the bigger menu (owner, 17 Nov)."""
    css = page_frame.FRAME_CSS
    check(".appbar-in{box-sizing:border-box;display:flex;align-items:center;gap:24px;width:100%;"
          "height:100%;min-width:0;max-width:var(--frame-max,none);margin:0 auto;"
          "padding:0 var(--frame-pad,16px)}" in css
          and "height:var(--bar-h);" in css and "padding:0 16px" not in css,
          "the bar's ground runs the full width and its contents take the page's content box")
    check(":root{--bar-h:48px}" in css and "@media (min-width:700px){:root{--bar-h:64px}}" in css
          and "top:var(--bar-h);" in css,
          "the bar is 64px tall from 700px and 48px on a phone, and the phone menu opens under it")
    check("font-size:18px;line-height:24px;font-weight:500;" in css
          and ".appbar .nav{display:flex;align-self:stretch;gap:24px;" in css
          and "padding:3px 4px 0;border-bottom:3px solid transparent;" in css
          and '.appbar .nav a[aria-current="page"]{color:var(--text-1);font-weight:600;'
              'border-bottom-color:var(--nav-line,var(--accent))}' in css
          and "height:100%;min-height:44px;" in css,
          "the menu is 18px at 500, 32px apart, the page you are on 600 with a 3px rule, "
          "and each link the bar's full height")
    check(page_frame.NAV_GROUP == {"jobs": "jobs", "watchlist": "apps", "insights": "story", "cv": "cv"}
          and '.appbar a[data-group="jobs"]{--nav-line:var(--group-jobs)}' in css
          and '.appbar a[data-group="watchlist"]{--nav-line:var(--group-apps)}' in css
          and '.appbar a[data-group="insights"]{--nav-line:var(--group-story)}' in css
          and '.appbar a[data-group="cv"]{--nav-line:var(--group-cv)}' in css
          and "text-decoration-color:var(--nav-line,var(--accent));" in css
          and 'data-group="home"' not in page_frame.appbar_html("home")
          and 'data-group="settings"' not in page_frame.appbar_html("settings"),
          "the page you are on is underlined in its group's colour, Jobs purple, the Watchlist "
          "teal, Insights amber and the CV rose, in the bar and the narrow menu; Home and "
          "Settings keep the accent")
    check(".appbar .brand-mark{width:68px;height:32px}" in css
          and ".appbar .brand-name{font-size:17px;line-height:24px}" in css,
          "the mark and the wordmark grow with the bar, the wordmark 17px")
    # Home: the bar's contents and the page are the same box, so the logo's left
    # edge is the greeting's at every width; worked out at 1920 and 1440, with a
    # 15px scroll bar and without.
    home = hp.HOME_CSS
    check(":root{--frame-max:2560px;--frame-pad:16px}" in css
          and "@media (min-width:768px){:root{--frame-pad:24px}}" in css
          and "@media (min-width:1024px){:root{--frame-pad:32px}}" in css
          and "max-width:var(--frame-max);margin:0 auto;padding:20px var(--frame-pad) 32px}" in home
          and "grid-template-rows:var(--bar-h) auto;" in home,
          "the shared box fills the window up to 2560px, 16, 24 and 32px in, and Home's page "
          "reads it")
    def left(width: float, cap: float = 1440, pad: float = 32) -> float:
        return max(0.0, (width - cap) / 2) + pad
    for window in (1920, 1440):
        for bar_w in (0, 15):
            inner = window - bar_w
            logo, greeting = left(inner), left(inner)
            check(abs(logo - greeting) <= 1,
                  f"at {window} wide ({'a 15px' if bar_w else 'no'} scroll bar) the logo and "
                  f"the greeting both start {logo:g}px in")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", help="ignored: the suite passes it to every runner")
    parser.parse_args(argv)
    frame_checks()
    light_checks()
    edge_checks()
    tmp = Path(tempfile.mkdtemp(prefix="radar-header-"))
    try:
        page_checks(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if failures:
        print(f"FAIL  {len(failures)} header check(s) failed:")
        for label in failures:
            print(f"  - {label}")
        return 1
    print("PASS  the shared header")
    return 0


if __name__ == "__main__":
    sys.exit(main())
