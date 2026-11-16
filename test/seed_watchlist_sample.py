#!/usr/bin/env python
"""Seed a throwaway database with an invented Watchlist, for looking at the
page and for the Watchlist runner. Every company, person, story and role here
is invented, the design's own world (design/watchlist/final): no real company,
no real person.

Usage: python test/seed_watchlist_sample.py --db test/watchlist_sample.sqlite [--now 2026-11-13T09:14:00Z]

Then: python scripts/serve_dashboard.py --db test/watchlist_sample.sqlite
and open /watchlist.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("RADAR_MOCK", "1")
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import radar_common as rc  # noqa: E402
import watchlist  # noqa: E402


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def seed(conn, now: datetime) -> dict:
    """The design's ten watched companies, three suggestions and their
    stories, threads and roles, dated around now. Returns names to ids."""
    day = lambda n, h=9: stamp((now - timedelta(days=n)).replace(hour=h, minute=0, second=0))  # noqa: E731
    date_of = lambda n: (now + timedelta(days=n)).strftime("%Y-%m-%d")  # noqa: E731
    ids = {}

    def company(name, city, country, **facts):
        cid = rc.resolve_company(conn, name, day(40))
        conn.execute("UPDATE companies SET city = ?, country = ? WHERE id = ?", (city, country, cid))
        if facts:
            sources = {k: {"from": "site", "label": f"Company site, {name.split()[0].lower()}.example",
                           "at": day(30)} for k in facts}
            cols = ", ".join(f"{k} = ?" for k in facts)
            conn.execute(f"UPDATE companies SET {cols}, enriched_at = ?, enrich_status = 'ok',"
                         f" fact_sources = ? WHERE id = ?",
                         (*facts.values(), day(30), json.dumps(sources), cid))
        ids[name] = cid
        return cid

    n = [0]

    def signal(cid, name, headline, kind, relevance, days_ago, source="eu-funding-digest", **extra):
        n[0] += 1
        cur = conn.execute(
            "INSERT INTO signals (url_hash, first_seen, source_id, company, headline, summary, source_url,"
            " relevance, why, status, company_id, story_type, card_summary, draft_comment, draft_note,"
            " drafted_at) VALUES (?,?,?,?,?,?,?,?,?,'new',?,?,?,?,?,?)",
            (f"wl-sample-{n[0]}", day(days_ago), source, name, headline, extra.get("summary", ""),
             extra.get("url", f"https://news.example/{n[0]}"), relevance, "Invented for the sample.",
             cid, kind, extra.get("card", headline[:90]), extra.get("comment"), extra.get("note"),
             day(days_ago) if extra.get("comment") or extra.get("note") else None))
        rc.assign_event_key(conn, cur.lastrowid)
        return cur.lastrowid

    def touch(cid, name, days_ago, channel, note, step=None, due=None, outcome="none"):
        conn.execute("INSERT INTO touches (company, touched_at, channel, note, next_action, next_action_date,"
                     " outcome, company_id) VALUES (?,?,?,?,?,?,?,?)",
                     (name, day(days_ago, 10), channel, note, step, due, outcome, cid))

    def role(cid, name, title, tier, days_ago, url_hash=None, window=0, **extra):
        cur = conn.execute(
            "INSERT INTO opportunities (url_hash, first_seen, source, company, title, location, salary_rate,"
            " source_url, cv_match, tier, status, company_id, buying_window, engagement_type, score_basis)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,'new',?,?,?,?)",
            (url_hash or f"wl-role-{name}-{title}", day(days_ago), "linkedin-alert", name, title,
             extra.get("location", "Guildford (hybrid)"), extra.get("pay", ""),
             f"https://jobs.example/{abs(hash(title)) % 10000}", extra.get("score", 74), tier, cid, window,
             extra.get("engagement", "contract"), "card"))
        return cur.lastrowid

    week3 = "Week three. One genuine engagement with their content, brief."
    q = company("Qurio", "Leuven", "Belgium")
    signal(q, "Qurio", "A university lab spins out Qurio to commercialise a wearable ECG patch",
           "spin-off", 78, 24)
    touch(q, "Qurio", 22, "comment", "Commented on the spin-off post.", week3, date_of(-1))
    lu = company("Lumenhal Photonics", "Eindhoven", "Netherlands")
    signal(lu, "Lumenhal Photonics", "Lumenhal Photonics closes a £6.5m seed round for its photonic biosensor "
           "reader", "funding", 92, 23)
    touch(lu, "Lumenhal Photonics", 21, "comment", "Commented on the seed round.",
          "Send the connection note to the chief executive.", date_of(0))
    h = company("Hartwell Infusion Ltd", "Guildford", "United Kingdom",
                what_they_build="Volumetric and syringe infusion pumps for hospital wards, Class IIb.",
                software_content="Pump firmware and a drug library editor. The site names IEC 62304.",
                stage="series c",
                people=json.dumps([{"name": "Marcus Hale", "role": "ceo"},
                                   {"name": "Rosa Okoye", "role": "cto"}]))
    sc = signal(h, "Hartwell Infusion Ltd", "Hartwell Infusion raises £32m Series C to scale its smart pump range",
                "funding", 81, 30,
                comment="Congratulations on the Series C. Scaling a pump range while keeping the IEC 62304 class C "
                        "file in step is where most teams feel the strain first. Good luck with the next phase.",
                note="Hello Rosa, I work on software quality and IEC 62304 for infusion and other Class IIb "
                     "devices, near Reading. I enjoyed your post on class C firmware reviews and would be glad "
                     "to connect. Alex")
    signal(h, "Hartwell Infusion Ltd", "Hartwell Infusion opens a firmware team in Guildford", "market", 52, 9)
    role(h, "Hartwell Infusion Ltd", "Software Quality Lead, Class II infusion pumps", "top", 23,
         pay="£650 to £750 a day", score=81)
    adv = role(h, "Hartwell Infusion Ltd", "Head of Software Quality, Class II infusion pumps", "question", 2,
               url_hash="wl-window-hartwell", window=1, engagement="permanent", score=71)
    conn.execute(
        "INSERT INTO signals (url_hash, first_seen, source_id, company, headline, relevance, why, status,"
        " company_id, story_type) VALUES (?,?,?,?,?,?,?,'new',?,'buying-window')",
        ("wl-window-hartwell", day(2), "job-advert", "Hartwell Infusion Ltd",
         "Hartwell Infusion Ltd is hiring a Head of Software Quality", 90, "A first software quality hire.", h))
    touch(h, "Hartwell Infusion Ltd", 30, "engagement", "Liked and shared the Series C announcement.",
          "Comment on their next post.", date_of(-17))
    touch(h, "Hartwell Infusion Ltd", 14, "comment", "Commented on Rosa Okoye's post about class C firmware "
          "reviews.", "Send the connection note to Rosa Okoye.", date_of(2), outcome="reply")
    t = company("Tessaly Diagnostics", "Edinburgh", "United Kingdom")
    signal(t, "Tessaly Diagnostics", "Tessaly Diagnostics raises £9m Series A to take its respiratory assay "
           "into clinical studies", "funding", 80, 39)
    touch(t, "Tessaly Diagnostics", 10, "artefact", "Sent the IVDR checklist article.",
          "Share the IVDR software checklist they asked about.", date_of(7))
    conn.execute("UPDATE companies SET state = 'in-conversation' WHERE id = ?", (t,))
    cv = company("Corvane Surgical", "Leeds", "United Kingdom")
    signal(cv, "Corvane Surgical", "Corvane Surgical raises £8m Series A for its spinal navigation system",
           "funding", 76, 9)
    touch(cv, "Corvane Surgical", 7, "comment", "Commented on the Series A.", week3, date_of(14))
    a = company("Arvell Photonics", "Ghent", "Belgium")
    signal(a, "Arvell Photonics", "Arvell Photonics closes a €3.1m seed round for its photonic blood-gas chip",
           "funding", 68, 16)
    touch(a, "Arvell Photonics", 15, "engagement", "Liked the seed round post.",
          "Look again once they name a device partner.", None)
    se = company("Selwick Photonics", "Enschede", "Netherlands")
    sel = signal(se, "Selwick Photonics", "Selwick Photonics spins out with a €2.4m seed round for a handheld "
                 "Raman reader", "spin-off", 76, 1)
    sr = company("Severn Renal Systems", "Bristol", "United Kingdom")
    signal(sr, "Severn Renal Systems", "Severn Renal Systems opens a home dialysis software team in Bristol",
           "market", 55, 4)
    touch(sr, "Severn Renal Systems", 16, "artefact", "Sent the home-use software article.")
    role(sr, "Severn Renal Systems", "Software Verification Engineer", "reading", 5, location="Bristol")
    w = company("Wrenfield Digital Health", "Manchester", "United Kingdom")
    signal(w, "Wrenfield Digital Health", "Wrenfield Digital Health adds £4m to take its SaMD triage app to more "
           "trusts", "funding", 62, 11)
    role(w, "Wrenfield Digital Health", "QA Lead, SaMD", "question", 6, location="Manchester")
    b = company("Brackley Sensing", "Oxford", "United Kingdom")
    signal(b, "Brackley Sensing", "Brackley Sensing raises £2.5m to scale a fibre-optic pressure sensor for "
           "catheter makers", "funding", 72, 16)
    # The not watched: three suggestions to be, and a removed company.
    ok = company("Okara Diagnostics", "Cork", "Ireland")
    touch(ok, "Okara Diagnostics", 11, "engagement", "Liked the launch post.")
    conn.execute(
        "INSERT INTO signals (url_hash, first_seen, source_id, company, headline, relevance, why, status,"
        " company_id, story_type) VALUES (?,?,?,?,?,?,?,'new',?,'buying-window')",
        ("wl-window-okara", day(1), "job-advert", "Okara Diagnostics",
         "Okara Diagnostics advertises for a Quality Manager", 90, "A first quality hire.", ok))
    role(ok, "Okara Diagnostics", "Quality Manager", "top", 1, url_hash="wl-window-okara", window=1,
         location="Cork")
    pdx = company("PDX Diagnostics", "Leiden", "Netherlands")
    signal(pdx, "PDX Diagnostics", "PDX Diagnostics raises an €18m Series A for its benchtop IVD analyser",
           "funding", 74, 1)
    vb = company("Vantage Bio", "Cambridge", "United Kingdom")
    signal(vb, "Vantage Bio", "Vantage Bio joins a Cambridge medtech accelerator cohort with a point-of-care "
           "sepsis test", "accelerator", 72, 3)
    company("Okara Medical Imaging", "Lyon", "France")
    for name in ("Qurio", "Lumenhal Photonics", "Tessaly Diagnostics", "Corvane Surgical", "Arvell Photonics",
                 "Severn Renal Systems", "Wrenfield Digital Health", "Brackley Sensing"):
        watchlist.watch(conn, ids[name], "name", None, day(35))
    watchlist.watch(conn, h, "insights", sc, day(30))
    conn.execute("UPDATE companies SET arrival_seen_at = ? WHERE id = ?", (day(30), h))
    watchlist.watch(conn, se, "insights", sel, stamp(now - timedelta(minutes=3)))
    rc.log_run(conn, "signals", mode="mock", note="sample", outcome="ok")
    watchlist.refresh_suggestions(conn, now)
    conn.commit()
    return ids


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True)
    parser.add_argument("--now", help="ISO moment the sample is drawn around, default now")
    args = parser.parse_args(argv)
    path = Path(args.db).resolve()
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    now = watchlist.moment_of(args.now) if args.now else datetime.now(timezone.utc)
    conn = rc.get_db(path)
    ids = seed(conn, now)
    conn.close()
    print(json.dumps({"ok": True, "db": str(path), "companies": len(ids)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
