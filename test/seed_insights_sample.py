#!/usr/bin/env python
"""Load the Insights news design's sample week into a fresh database.

    python test/seed_insights_sample.py --db /tmp/news.sqlite [--week typical|quiet|stale]

The stories, companies and sources are the fictional world the news page was
drawn from (design/insights/news on the design/round-1 branch), so the built
page can be put beside the frames. Dates are counted back from the moment the
seed runs, so "this week" is always the last seven days. typical is the
drawn week; quiet keeps only the stories under 10 (the page floor) this week; stale stops the
watcher 22 hours ago and leaves seven sources unanswered.

Writes only the database it is given, which must not exist yet. No model is
called and nothing is fetched. Not part of the suite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import radar_common  # noqa: E402

# company: what it builds, software, stage, city, country, region, people, on the Watchlist
COMPANIES = {
    "Hartwell Infusion Ltd": ("Volumetric and syringe infusion pumps, Class IIb.", "Embedded firmware, IEC 62304 class C.",
                              "series c", "Guildford", "UK", "Local", ["R. Okoye (VP Quality)"], True),
    "PDX Diagnostics": ("A benchtop IVD analyser for hospital laboratories.",
                        "Analyser control software and a results interface, under IVDR.", "series a", "Cambridge", "UK",
                        "UK", ["M. Farrant (CEO)"], True),
    "Selwick Photonics": ("A handheld Raman reader for wound infection.", None, "seed", "Enschede", "Netherlands",
                          "Europe", [], True),
    "Vantage Bio": ("A molecular test for sepsis at the bedside.", None, "seed", "Leeds", "UK", "UK", [], False),
    "Halvard Biosensing": ("An optical biosensor for kidney function tests at the bedside.", None, "seed",
                           "Copenhagen", "Denmark", "Europe", ["K. Lindahl (CEO)"], True),
    "Mirevo Health": ("A triage tool for chest CT.", "The product is software.", "seed", "Antwerp", "Belgium",
                      "Europe", ["N. Claes (CEO)"], True),
    "Okara Diagnostics": ("A rapid blood test for antibiotic resistance.", None, "seed", "Dublin", "Ireland",
                          "Europe", ["C. Brannagh (CEO)"], True),
    "Quillon Neuro": ("A wearable EEG headset for epilepsy monitoring.", None, "seed", "Delft", "Netherlands",
                      "Europe", [], True),
    "Dunmere Vision": ("A retinal imaging camera for community clinics.", None, "seed", "Leeds", "UK", "UK", [], True),
    "Ilvane Respiratory": ("A connected spirometer for home use.", "Device firmware and a patient app.", "seed",
                           "Reading", "UK", "Local", [], True),
    "Arvell Photonics": ("A photonic chip for blood gas analysis.", None, "seed", "Ghent", "Belgium", "Europe", [], False),
    "Lumenhal Photonics": ("A photonic biosensor reader for sepsis markers at the bedside.",
                           "Reader firmware and signal processing.", "seed", "Eindhoven", "Netherlands", "Europe",
                           ["S. Veldkamp (CEO)"], True),
    "Tessaly Diagnostics": ("A lab-on-chip assay for respiratory infections.",
                            "Reader firmware and an assay analysis service.", "series a", "Ghent", "Belgium", "Europe",
                            [], True),
    "Ashcombe Medical": ("Enteral feeding pumps for hospital and home use.",
                         "Embedded pump firmware and a set-up app for clinicians.", "private", "Crawley", "UK",
                         "Local", [], True),
    "Vowchurch Medical": (None, None, None, None, None, None, [], False),
    "Oakthorpe Laboratories": (None, None, None, None, None, None, [], False),
    "Elstow Medical": (None, None, None, None, None, None, [], False),
    "Jevington Health": (None, None, None, None, None, None, [], False),
    "Pallister Health": (None, None, None, None, None, None, [], False),
    "Unknown company": (None, None, None, None, None, None, [], False),
}

# days ago, hour, source id, company, type, relevance, headline, card summary, why, source words, extra
STORIES = [
    (2, 10, "job-advert", "Hartwell Infusion Ltd", "buying-window", 90,
     "Hartwell Infusion Ltd advertises for a Head of Software Quality, a first hire at a company you have been in touch with",
     "A first quality, regulatory or software hire at a company you have already contacted.",
     "A first software quality hire at a company you have already contacted. The post is open now.", None, {}),
    (1, 6, "european-device-funding-digest", "PDX Diagnostics", "funding", 74,
     "PDX Diagnostics raises €18m Series A for its IVD analyser",
     "A Series A heading into IVDR, with nothing said about software leadership.",
     "Series A at a UK analyser company heading into IVDR certification, with a quality lead named and nothing "
     "said about software leadership.", "PDX Diagnostics has raised €18m to take its benchtop analyser through IVDR "
     "certification and into its first NHS laboratories next year.", {}),
    (3, 6, "medtech-accelerator", "Vantage Bio", "accelerator", 72,
     "Vantage Bio joins MedTech Accelerator cohort 7",
     "Accelerator entry at a seed-stage test maker, with a quality lead already named.",
     "Accelerator entry at a seed-stage molecular test maker, with a quality lead named in the cohort notes, so no "
     "gap is stated.", None, {}),
    (3, 0, "nordic-diagnostics-fund", "Halvard Biosensing", "spin-off", 68,
     "A Copenhagen university hospital spins out Halvard Biosensing to develop a bedside kidney function test",
     "A new spin-off with a quality lead already among the founders.",
     "New spin-off from a Copenhagen hospital, with a quality lead already named among the founders.", None, {}),
    (4, 6, "brabant-deep-tech", "Mirevo Health", "accelerator", 64,
     "Mirevo Health joins the Brabant deep-tech accelerator with a triage tool for chest CT",
     "Accelerator entry at a software-only company whose founders include a regulatory lead.",
     "Accelerator entry at a software-only company whose founders include a regulatory lead.", None, {}),
    (2, 6, "irish-device-founders", "Okara Diagnostics", "funding", 58,
     "Okara Diagnostics wins €2.1m in grant funding for its rapid resistance test",
     "Grant funding for a seed-stage diagnostics company, a clinical study first.",
     "Grant funding at a seed-stage diagnostics company in Dublin, with a clinical study to run before any "
     "regulated product.", None, {}),
    (1, 12, "benelux-biosensor-network", "Quillon Neuro", "accelerator", 52,
     "Quillon Neuro enters a Benelux sensor programme with its EEG headset",
     "Right sector, at a stage where regulated work is still some way off.",
     "Accelerator entry in the right sector, at a stage where regulated work is still some way off.", None, {}),
    (4, 6, "northern-health-ventures", "Dunmere Vision", "funding", 47,
     "Dunmere Vision raises £1.2m seed round for a retinal camera for community clinics",
     "A small seed round at a hardware-first camera maker.",
     "Small seed round at a camera maker whose first product is hardware, with its software described as a later step.",
     None, {}),
    (3, 9, "qa-tripwire", "Ilvane Respiratory", "first-hire", 43,
     "Ilvane Respiratory advertises its first quality manager in Reading",
     "The advert suggests a first quality post, but does not say so.",
     "The advert suggests this is the company's first quality post, but does not say so.", None, {}),
    (2, 6, "mhra-news", "Unknown company", "regulation", 62,
     "MHRA opens a consultation on changes to software in devices already on the market",
     "A change to how software updates are handled after a device is on the market.",
     "A change to how software updates are handled would alter the regulatory plan of every UK device with "
     "software in it.", None, {}),
    (3, 6, "team-nb-news", "Unknown company", "market", 58,
     "Notified body capacity for IVDR class C devices stays short into 2027, a trade survey finds",
     "Long certification queues push IVD companies to start their software files earlier.",
     "Long certification queues push IVD companies to start their software files earlier.", None, {}),
    # The busy-week event: one spin-off, three sources, grouped by the rule.
    (1, 0, "lowlands-photonics-cluster", "Selwick Photonics", "spin-off", 93,
     "A university in Enschede spins out Selwick Photonics with a €5m seed round to build a handheld Raman reader for wound infection",
     "A seed-stage photonics spin-off with no software or quality lead named.",
     "New spin-off at seed stage with a software-driven diagnostic reader and no software or quality hires announced.",
     "Selwick Photonics, spun out of the university's optics group this month, has closed a seed round of €5 million "
     "led by Delta Seed Fund. The company is building a handheld Raman reader that tells a district nurse within "
     "minutes whether a wound is infected, without sending a swab to a laboratory. Its first clinical study starts "
     "in spring.", {"drafts": True}),
    (1, 6, "delta-seed-fund", "Selwick Photonics", "funding", 92,
     "Selwick Photonics raises €5m seed round to commercialise a Raman reader for wound infection spun out of a university in Enschede",
     "A fresh photonics spin-off with money and no quality hires announced.",
     "Fresh university spin-off at seed stage with a photonics-driven device and no software or quality hires announced.",
     None, {}),
    (1, 6, "photonics-in-medicine", "Selwick Photonics", "spin-off", 92,
     "Seed round of €5m for Selwick Photonics, a new spin-off with a handheld reader for wound infection",
     "A seed-stage spin-off where the report names no software or quality lead.",
     "Seed-stage spin-off with a diagnostic reader, where the report names no software or quality lead.", None, {}),
    (3, 6, "flanders-spin-off-register", "Arvell Photonics", "funding", 88,
     "Arvell Photonics closes a €4m seed round for a photonic chip that reads blood gases",
     "Newly funded, with a software-driven device and no software lead named.",
     "Newly funded photonics company with a software-driven device and no software lead named.", None, {}),
    # Under 40 this week.
    (3, 6, "trade-press", "Vowchurch Medical", "other", 35,
     "Vowchurch Medical gains CE marking for its second product line", "An established maker passing a milestone.",
     "Established device maker passing a regulatory milestone, so the early software and quality work is done.", None, {}),
    (4, 6, "trade-press", "Oakthorpe Laboratories", "funding", 22,
     "Oakthorpe Laboratories closes an $80m Series C to expand in the United States", "No European presence found.",
     "No European presence found, and a company at this stage is likely to have software leadership already.", None, {}),
    (2, 6, "trade-press", "Elstow Medical", "other", 18,
     "Elstow Medical is acquired by a private equity firm", "Ownership news, no regulated software angle.",
     "Ownership news with no funding, spin-off or accelerator event.", None, {}),
    (2, 12, "trade-press", "Jevington Health", "first-hire", 12,
     "Jevington Health recruits two field service engineers", "A hiring story; the jobs pipeline owns hiring.",
     "A hiring story with no funding, spin-off or accelerator event.", None, {}),
    (4, 0, "trade-press", "Pallister Health", "other", 5,
     "Pallister Health announces participation in two investor conferences", "An investor relations notice.",
     "Investor relations notice from a listed company. No funding event and no European link.", None, {}),
    (1, 0, "northern-health-ventures", "Unknown company", "other", 0,
     "Programme page from Northern Health Ventures picked up as a news item", "Not news.",
     "A website page with no announcement, no company and no event.", None, {}),
    # Earlier weeks, still on file.
    (25, 6, "lowlands-photonics-cluster", "Lumenhal Photonics", "funding", 92,
     "Lumenhal Photonics closes a €6.5m seed round for its photonic biosensor reader",
     "Newly funded, with a software-driven device and no software lead named.",
     "Newly funded photonics company with a software-driven device and no software lead named.", None, {}),
    (40, 6, "flanders-spin-off-register", "Tessaly Diagnostics", "funding", 80,
     "Tessaly Diagnostics raises €9m Series A to take its respiratory assay into clinical studies",
     "A Series A heading for clinical studies, software lead not named.",
     "Series A at an IVD company heading for clinical studies, with no software lead named.", None, {}),
    (7 + 9, 10, "job-advert", "Ashcombe Medical", "buying-window", 90,
     "Ashcombe Medical advertises for an Embedded Software Lead, a first hire at a company you have been in touch with",
     "A first quality, regulatory or software hire at a company you have already contacted.",
     "A first software hire at a company you have already contacted.", None, {}),
    (11, 6, "eu-mdcg-guidance", "Unknown company", "regulation", 48,
     "FDA finalises guidance on predetermined change control plans for device software",
     "Guidance that shapes how software changes are planned for the US market.",
     "Guidance that shapes how software changes are planned, for companies selling in the US.", None, {}),
    # Hidden this week.
    (2, 6, "trade-press", "Vowchurch Medical", "other", 41,
     "Vowchurch Medical opens a new site in Hereford", "Expansion at an established maker.",
     "Expansion at an established maker with settled teams.", None, {"hidden": True}),
]

SOURCES = ["job-advert", "european-device-funding-digest", "medtech-accelerator", "nordic-diagnostics-fund",
           "brabant-deep-tech", "irish-device-founders"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--week", choices=("typical", "quiet", "stale"), default="typical")
    args = ap.parse_args(argv)
    path = Path(args.db)
    if path.exists():
        print(f"{path} exists; the seed only writes a new database.", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    conn = radar_common.get_db(path)
    seed(conn, now, args.week)
    conn.close()
    print(f"Seeded {path} with the {args.week} week.")
    return 0


def seed(conn, now: datetime, week: str = "typical") -> dict:
    """The sample week on an open connection, counted back from now. Commits,
    and returns the companies' ids by name. The page snapshot runner calls
    this on its own database, beside the Jobs and Watchlist samples."""
    iso = lambda when: when.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    ids = {}
    for name, (builds, software, stage, city, country, region, people, watched) in COMPANIES.items():
        cid = radar_common.resolve_company(conn, name, iso(now - timedelta(days=60)))
        conn.execute("UPDATE companies SET what_they_build=?, software_content=?, stage=?, city=?, country=?,"
                     " region=?, people=?, watched_at=? WHERE id=?",
                     (builds, software, stage, city, country, region, json.dumps(people) if people else None,
                      iso(now - timedelta(days=30)) if watched else None, cid))
        ids[name] = cid
    for days, hour, source, company, kind, rel, headline, summary, why, words, extra in STORIES:
        if week == "quiet" and days < 7 and rel >= 10:
            continue
        when = (now - timedelta(days=days)).replace(hour=hour, minute=0)
        if when > now:
            when -= timedelta(days=1)
        url = f"https://example.org/{source}/{hashlib.sha1(headline.encode()).hexdigest()[:10]}"
        cur = conn.execute(
            "INSERT INTO signals (url_hash, first_seen, source_id, company, headline, summary, source_url,"
            " relevance, why, playbook_step, company_id, region, story_type, card_summary, acknowledged_at,"
            " draft_comment, draft_note, draft_source, drafted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (radar_common.url_hash(url), iso(when), source, company, headline, words, url, rel, why,
             "Watch for a first quality or software hire.", ids.get(company), COMPANIES[company][5], kind, summary,
             iso(now - timedelta(hours=3)) if extra.get("hidden") else None,
             "Reading a wound at the bedside in minutes rather than sending a swab away for two days is a real change "
             "for district nurses. Congratulations to the whole team on the launch and the round."
             if extra.get("drafts") else None,
             "Congratulations on the launch and the round. Getting from a lab prototype to a device a nurse can use is "
             "a hard step, and it is good to see it funded." if extra.get("drafts") else None,
             "article" if extra.get("drafts") else None, iso(when) if extra.get("drafts") else None))
        radar_common.assign_event_key(conn, cur.lastrowid)
    conn.execute("INSERT INTO touches (company, touched_at, channel, note, company_id) VALUES (?,?,?,?,?)",
                 ("PDX Diagnostics", iso(now - timedelta(days=14)), "comment", "Commented on the Series A post.",
                  ids["PDX Diagnostics"]))
    conn.execute(
        "INSERT INTO opportunities (url_hash, first_seen, source, title, company, location, tier, company_id)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (radar_common.url_hash("https://example.org/job/pdx"), iso(now - timedelta(days=23)), "linkedin-alert",
         "Software Quality Lead, IVD analyser", "PDX Diagnostics", "Remote (EU)",
         "top", ids["PDX Diagnostics"]))
    watcher_ran = now - timedelta(hours=22 if week == "stale" else 3)
    radar_common.log_run(conn, "inbox", items_in=1, items_new=1)
    conn.execute("UPDATE runs SET ts = ? WHERE workflow = 'inbox'", (iso(now - timedelta(minutes=12)),))
    conn.execute("INSERT INTO runs (ts, workflow, mode, items_in, items_new, note, outcome) VALUES (?,?,?,?,?,?,?)",
                 (iso(watcher_ran), "signals", "live", 30, 4, "sample run", "ok"))
    import build_dashboard
    for i, src in enumerate(build_dashboard.load_watchlist()):
        if (src.get("status") or "live") == "unverified":
            continue
        bad = week == "stale" and i % 3 == 0
        conn.execute("INSERT OR REPLACE INTO source_state (source_id, url, method, last_checked, last_status)"
                     " VALUES (?,?,?,?,?)", (src["id"], src.get("url"), src.get("method"), iso(watcher_ran),
                                             "599" if bad else "200"))
    conn.commit()
    return ids


if __name__ == "__main__":
    sys.exit(main())
