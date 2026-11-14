#!/usr/bin/env python
"""Load the round-2 design's sample data into a fresh database.

    python test/seed_jobs_sample.py --db /tmp/sample.sqlite [--today 2026-11-13]

The roles, gates, reasons and the one stored advert are the fictional sample
the Jobs design was drawn from (sample-data.md and sample-advert.md, round 2,
13 Nov 2026), so the built page can be put beside the design frames. Its dates
are moved so that the design's "today", 13 Nov, is the day the seed runs
(or --today), and Live, Unknown and the expiry dates read as they were drawn.

Writes only the database it is given, which must not exist yet. No model is
called and nothing is fetched. Not part of the suite.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import radar_common  # noqa: E402

DESIGN_TODAY = datetime(2026, 11, 13, tzinfo=timezone.utc)
SHIFT = timedelta(0)
MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
     "Nov", "Dec"), 1)}
MODEL = "claude-sonnet-4-6"
PROMPT = "97f11cce"
CV = "cv-20261029-091500.md"

ADVERT = """About us

Hartwell Infusion makes volumetric and syringe infusion pumps for hospital wards and home care. Our pumps are Class IIb devices, CE and UKCA marked, and run on embedded firmware that we design, verify and maintain in house. We are 140 people across engineering, quality, manufacturing and clinical support, based at our Guildford site. The current range is the V2 volumetric pump and the S1 syringe driver, used in more than 300 NHS and private hospitals. We ship two firmware releases a year for each pump, and a third product, an ambulatory pump for home infusion, is in design. In October we closed a £32m Series C to widen the range and to prepare our first FDA 510(k) submission, planned for 2027. Software is now the largest part of each new pump, and we want a quality lead who has done this before.

The role

You will own the IEC 62304 software development lifecycle for our class C pump firmware. You will be the person our engineers, our notified body and our auditors turn to on software quality. In your first three months you will prepare the software technical file ahead of our notified body audit in Q1. After that you will set up the processes we need for the US submission and help us hire into the team. You will report to the VP Quality and work closely with the Head of Embedded Engineering, whose team of 22 writes the firmware. Our software quality team is six people today, and we plan to add two more in 2027.

What you will do

- Act as internal auditor for software processes under ISO 13485.
- Lead a team of six software test and quality engineers.
- Run ISO 14971 risk management for software hazards, from hazard analysis to the risk management report.
- Set the verification and validation strategy for embedded firmware, including hardware-in-the-loop testing.
- Maintain SOUP and cybersecurity records under IEC 81001-5-1.
- Keep MISRA C and static analysis in a regulated codebase under control, with clear rules on deviations.
- Own traceability in Polarion, from user needs to verification results.
- Present software quality status to the leadership team each month.

What you will bring

- Software quality leadership on Class IIb or Class III devices.
- Experience of FDA 510(k) submissions.
- Working knowledge of IEC 60601-1 and IEC 62366-1.
- A degree in engineering or a physical science.
- Confidence with auditors, notified bodies and engineering teams alike.
- Clear written English; you will write much of the technical file.
- Desirable: infusion pumps under IEC 60601-2-24.
- Desirable: an ISO 13485 lead auditor certificate.

Your first year

- Months 1 to 3: the software technical file and the notified body audit.
- Months 4 to 6: a gap assessment against what the FDA will expect in our 510(k).
- Months 7 to 12: the software plan for the ambulatory pump, and two new hires.

Working pattern and pay

Hybrid, two days a week at our Guildford site. The rest of the week is remote, with occasional travel to our contract manufacturer in Swindon. This is a 12-month contract with a view to extension. Day rate £700–£750, outside IR35. Start in January.

How to apply

Send your CV and a short note on a technical file you have prepared. We will reply to every applicant within ten working days. No agencies, please."""

SOURCES = {"LinkedIn": "linkedin-alert", "Reed": "reed-alert",
           "Indeed": "indeed-alert", "CV-Library": "cvlibrary-alert"}


def day(text: str, hour: int = 9, minute: int = 0) -> str:
    """'21 Oct' as a stored timestamp, moved by SHIFT."""
    d, m = text.split()
    moment = datetime(2026, MONTHS[m], int(d),
                      hour, minute, tzinfo=timezone.utc) + SHIFT
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# id, new, title, company, location, type, rate, cv, first, last, source,
# tier, failed, lead (why or question), sector note, cv note, location note,
# rate note, gates (s, c, l, r: 1, 0, None)
ROLES = [
    ("T1", 0, "Head of Software Quality, Class II infusion pumps", "Hartwell Infusion Ltd",
     "Guildford (hybrid, 2 days)", "contract", "£700–£750/day, outside IR35", 86, "21 Oct", "11 Nov",
     "LinkedIn", "top", [], "Owns the IEC 62304 software file through a notified body audit. Your CV leads with the same work.",
     "Infusion pumps, Class IIb. In scope.", "86. IEC 62304 class C and notified body audit work are both on the CV.",
     "Guildford, two days a week on site.", "£700–£750/day, clears the £700 floor.", (1, 1, 1, 1)),
    ("T6", 1, "Interim Head of Software | Class IIb | 6 months | £700-£750pd", "Marchetti Devices",
     "Basingstoke", "contract", "£700-£750pd", 83, "13 Nov", "13 Nov", "LinkedIn", "top", [],
     "Six-month interim lead for Class IIb device software, starting in December.",
     "Class IIb device software", "83% clears the 70% pass mark", "Basingstoke",
     "£750/day clears the £700 floor", (1, 1, 1, 1)),
    ("T2", 1, "Fractional Software Director, IVD analyser", "PDX Diagnostics",
     "Cambridge, 3 days on site", "contract", "£800/day", 81, "13 Nov", "13 Nov", "LinkedIn", "top", [],
     "Three days a week on site leading an IVD analyser's software through IVDR certification.",
     "IVD analyser.", "81. IVD analyser verification under IVDR is on the CV.",
     "Cambridge, three days a week on site, UK.", "£800/day, clears the £700 floor.", (1, 1, 1, 1)),
    ("T5", 1, "Principal Software Engineer, cardiac monitoring", "Brandt Cardio", "Bracknell",
     "contract", None, 79, "12 Nov", "12 Nov", "LinkedIn", "top", [], "",
     "cardiac monitoring, Class IIa", "79% clears the 70% pass mark", "Bracknell", "not stated", (1, 1, 1, 1)),
    ("T8", 0, "Head of Regulatory Software, SaMD", "Quayle Health", "London (hybrid, 1 day)",
     "permanent", "£105,000/year", 77, "3 Nov", "8 Nov", "LinkedIn", "top", [],
     "Leads regulatory work for software as a medical device, one day a week in London.",
     "SaMD", "77% clears the 70% pass mark", "London (hybrid, 1 day)",
     "£105,000/year clears the £100,000 floor", (1, 1, 1, 1)),
    ("T4", 0, "Software Engineering Manager, surgical robotics", "Rowe & Keane Surgical", "London",
     "permanent", "£95,000–£110,000/year", 74, "23 Oct", "1 Nov", "Indeed", "top", [],
     "Manages two firmware teams on a Class IIb robotic arm heading for a 510(k).",
     "surgical robotics, Class IIb", "74% clears the 70% pass mark", "London",
     "£110,000/year clears the £100,000 floor", (1, 1, 1, 1)),
    ("T10", 0, "Fractional QA/RA Lead, digital pathology", "TMD Instruments Ltd", "Oxford",
     "contract", "£700/day, 2 days a week", 72, "28 Oct", "4 Nov", "LinkedIn", "top", [],
     "Two days a week on QA and regulatory work for digital pathology software under IVDR.",
     "digital pathology", "72% clears the 70% pass mark", "Oxford", "£700/day clears the £700 floor", (1, 1, 1, 1)),
    ("T12", 0, "IEC 62304 Consultant, hearing implants", "Thorne Audiology", "Cambridge",
     "contract", "£650–£700/day", 71, "29 Sep", "6 Oct", "LinkedIn", "top", [],
     "IEC 62304 consultancy on hearing implant software. Your CV has implant firmware verification.",
     "hearing implants", "71% clears the 70% pass mark", "Cambridge", "£700/day clears the £700 floor", (1, 1, 1, 1)),
    ("T3", 0, "Interim QA/RA Software Lead", "Ainsworth Medical", "Remote, UK", "contract", "£650/day", 78,
     "4 Nov", "10 Nov", "LinkedIn", "question", ["rate"],
     "Would they move on the rate? £650/day is £50 under the £700 floor.",
     "medical devices", "78% clears the 70% pass mark", "Remote, UK", "£650/day is under the £700 floor", (1, 1, 1, 0)),
    ("T9", 1, "Software Verification Manager, ophthalmic lasers", "Kinross Optics",
     "Southampton (hybrid, 1 day)", "contract", "£675/day", 77, "13 Nov", "13 Nov", "Reed", "question", ["rate"],
     "Would they move on the rate? £675/day is £25 under the £700 floor.",
     "ophthalmic lasers", "77% clears the 70% pass mark", "Southampton (hybrid, 1 day)",
     "£675/day is under the £700 floor", (1, 1, 1, 0)),
    ("Q3", 0, "IEC 62304 Lead - Digital Pathology - Amsterdam (3 days on site)", "Pellucid Imaging BV",
     "Amsterdam", "contract", "€850/day", 76, "6 Nov", "10 Nov", "LinkedIn", "question", ["location"],
     "Would they go remote? Three days a week on site in Amsterdam.",
     "digital pathology", "76% clears the 70% pass mark", "on site 3 days in Amsterdam",
     "€850/day is £722.50, clears the £700 floor", (1, 1, 0, 1)),
    ("T7", 0, "Software Quality Engineer - Medical Devices - Contract - Outside IR35 - Hybrid (Surrey)",
     "Lindqvist Medical UK", "Woking (hybrid)", "contract", "£675/day, outside IR35", 76, "30 Oct", "9 Nov",
     "Reed", "question", ["rate"], "Would they move on the rate? £675/day is £25 under the £700 floor.",
     "medical devices", "76% clears the 70% pass mark", "Woking (hybrid)",
     "£675/day is under the £700 floor", (1, 1, 1, 0)),
    ("T11", 0, "Embedded Software Lead, enteral feeding pumps", "Ashcombe Medical", "Crawley", "contract",
     "£690/day", 75, "5 Nov", "7 Nov", "CV-Library", "question", ["rate"],
     "Would they move on the rate? £690/day is £10 under the £700 floor.",
     "enteral feeding pumps", "75% clears the 70% pass mark", "Crawley", "£690/day is under the £700 floor", (1, 1, 1, 0)),
    ("Q4", 1, "Software Quality Lead, dental imaging", "Carrow Dental Systems", "Norwich", "contract",
     "£600/day", 74, "12 Nov", "12 Nov", "LinkedIn", "question", ["rate"],
     "Would they move on the rate? £600/day is £100 under the £700 floor.",
     "dental imaging, Class IIa", "74% clears the 70% pass mark", "Norwich",
     "£600/day is under the £700 floor", (1, 1, 1, 0)),
    ("Q6", 0, "Software Lead, sleep apnoea devices", "Mortimer Respiratory", "Manchester, on site 3 days",
     "contract", "£700/day", 73, "11 Nov", "11 Nov", "LinkedIn", "question", ["location"],
     "Would they take two days on site? The advert asks for three in Manchester.",
     "sleep apnoea devices", "73% clears the 70% pass mark", "on site 3 days in Manchester",
     "£700/day clears the £700 floor", (1, 1, 0, 1)),
    ("Q1", 0, "Software Verification Lead, insulin delivery", "Vandeput Medical NV", "Leuven, Belgium",
     "contract", "€720/day", 72, "2 Nov", "9 Nov", "LinkedIn", "question", ["rate"],
     "Would they move on the rate? €720/day is £612, £88 under the £700 floor.",
     "insulin delivery", "72% clears the 70% pass mark", "Leuven, Belgium",
     "€720/day is £612, under the £700 floor", (1, 1, 1, 0)),
    ("Q5", 0, "QA Manager, IVD reagents (software)", "Ellery Bio", "Glasgow, on site 5 days", "permanent",
     "£100,000/year", 71, "22 Oct", "30 Oct", "Indeed", "question", ["location"],
     "Would they go hybrid? Five days a week on site in Glasgow.",
     "IVD reagents", "71% clears the 70% pass mark", "on site 5 days in Glasgow",
     "£100,000/year clears the £100,000 floor", (1, 1, 0, 1)),
    ("Q2", 0, "Medical Device Software Consultant", "MacLeod Imaging", "Edinburgh, on site 4 days",
     "contract", "£700/day", 70, "31 Oct", "7 Nov", "CV-Library", "question", ["location"],
     "Would they go remote? The advert asks for four days a week on site in Edinburgh.",
     "medical device software", "70% clears the 70% pass mark", "on site 4 days in Edinburgh",
     "£700/day clears the £700 floor", (1, 1, 0, 1)),
    ("R5", 0, "Head of QA, cosmetic devices", "Sabine Beauty Tech", "London", "permanent", "£90,000/year", 61,
     "8 Nov", "8 Nov", "LinkedIn", "reading", ["sector", "cv", "rate"], "A quality lead for cosmetic devices, out of scope.",
     "cosmetic devices, out of scope", "61% is under the 70% pass mark", "London",
     "£90,000/year is under the £100,000 floor", (0, 0, 1, 0)),
    ("R4", 1, "Clinical Software Specialist", "Whitlock Health", "Leeds", "contract", "£450/day", 55,
     "13 Nov", "13 Nov", "LinkedIn", "reading", ["cv", "rate"], "A clinical software role, not leadership.",
     "clinical software", "55% is under the 70% pass mark", "Leeds", "£450/day is under the £700 floor", (1, 0, 1, 0)),
    ("R1", 0, "Senior Embedded Engineer (Zephyr), wearables", "Okafor Wearables", "Crawley", "permanent",
     "£70,000/year", 52, "27 Oct", "11 Nov", "Indeed", "reading", ["cv", "rate"], "Hands-on embedded work on wearables.",
     "wearables", "52% is under the 70% pass mark", "Crawley", "£70,000/year is under the £100,000 floor", (1, 0, 1, 0)),
    ("R2", 0, "Regulatory Affairs Manager, SaMD", "Pemberton Digital Health", "Woking", "permanent",
     "£75,000/year", 48, "16 Oct", "24 Oct", "Reed", "reading", ["cv", "rate"], "Regulatory affairs for SaMD, below the floor.",
     "SaMD", "48% is under the 70% pass mark", "Woking", "£75,000/year is under the £100,000 floor", (1, 0, 1, 0)),
    ("R3", 0, "Firmware Engineer, hearing aids", "Calder Audio", "Bristol", "permanent", "£62,000/year", 45,
     "10 Nov", "10 Nov", "Indeed", "reading", ["cv", "rate"], "Firmware for hearing aids.",
     "hearing aids", "45% is under the 70% pass mark", "Bristol, no working pattern given",
     "£62,000/year is under the £100,000 floor", (1, 0, None, 0)),
]

APPLIED = [
    ("A1", "Software Lead, surgical staplers", "Tamesis Surgical", "London", "£690/day", 74, "3 Nov", "9 Nov", "Reed"),
    ("A2", "Fractional QA/RA Lead, SaMD", "Wrenfield Digital Health", "Remote, UK", "£700/day", 78, "28 Oct", "6 Nov", "LinkedIn"),
    ("A3", "Software Quality Lead, respiratory devices", "Fenwick Respiratory", "Leeds", "£725/day", 80, "30 Oct", "4 Nov", "CV-Library"),
    ("A4", "IEC 62304 Consultant", "Clearwater Ophthalmic", "Remote, UK", "£680/day", 72, "24 Oct", "31 Oct", "LinkedIn"),
    ("A5", "Interim Head of Software, enteral feeding pumps", "Marlow Medical", "Marlow", "£750/day", 82, "20 Oct", "28 Oct", "LinkedIn"),
    ("A6", "Software Verification Manager, IVD", "Ashdown Diagnostics", "Oxford", "£88,000/year", 70, "15 Oct", "22 Oct", "Indeed"),
    ("A7", "Head of Software, neuromodulation", "Calder Neurotech", "Edinburgh", "£105,000/year", 77, "6 Oct", "15 Oct", "LinkedIn"),
]

FILTERED = [
    ("F1", "Graduate Quality Technician", "Brightwell Labs", "Leeds", 31, "cv match under 40"),
    ("F2", "Junior Firmware Tester", "Nimbus Devices", "Reading", 28, "cv match under 40"),
    ("F3", "Field Service Engineer", "Arden Imaging", "Birmingham", 35, "cv match under 40"),
    ("F4", "Sales Manager, surgical consumables", "Oakfield Surgical", "London", 52, "not a real leadership role"),
]


def reasons_json(scored_at: str, basis: str, items: list) -> str:
    return json.dumps({"v": radar_common.REASONS_VERSION, "scored_at": scored_at,
                       "basis": basis, "dropped": 0,
                       "reasons": [{"reason": r, "quote": q} for r, q in items]})


def seed(path: Path) -> dict:
    conn = radar_common.get_db(path)
    ids: dict = {}
    n = 0

    def insert(cols: dict) -> int:
        nonlocal n
        n += 1
        cols.setdefault("url_hash", f"sample-{n}")
        conn.execute(f"INSERT INTO opportunities ({', '.join(cols)}) VALUES"
                     f" ({', '.join('?' for _ in cols)})", tuple(cols.values()))
        return conn.execute("SELECT id FROM opportunities WHERE url_hash = ?",
                            (cols["url_hash"],)).fetchone()[0]

    read_stamp = day("12 Nov", 18, 40)
    for (rid, new, title, company, loc, etype, rate, cv, first, last, source, tier,
         failed, lead, s_note, c_note, l_note, r_note, gates) in ROLES:
        cid = radar_common.resolve_company(conn, company)
        scored = day(last if rid in ("T2", "T6", "T5", "T9", "R4") else "13 Nov", 8, 0)
        pre = rid not in ("T1", "T2", "T6", "T5", "T9", "R4")
        cols = {
            "first_seen": day(first), "last_seen": day(last), "source": SOURCES[source],
            "company": company, "title": title, "location": loc, "salary_rate": rate,
            "source_url": f"https://www.linkedin.com/jobs/view/40000{n + 1:05d}/",
            "thread_type": "inbound", "cv_match": cv, "one_line_why": lead if tier != "question" else "",
            "red_flags": "[]", "tier": tier, "failed_gates": json.dumps(failed),
            "question_text": lead if tier == "question" else None,
            "gate_sector": gates[0], "gate_sector_note": s_note,
            "gate_cv": gates[1], "gate_cv_note": c_note,
            "gate_location": gates[2], "gate_location_note": l_note,
            "gate_rate": gates[3], "gate_rate_note": r_note,
            "rate_band": "unstated" if rate is None else "above",
            "engagement_type": etype, "role_key": radar_common.role_key(title, company),
            "company_id": cid, "score_basis": "card",
            "prompt_version": "pre-record" if pre else PROMPT,
            "score_model": "unknown" if pre else MODEL,
            "scored_at": None if pre else scored, "cv_version": None if pre else CV,
            "read_at": None if new else read_stamp, "seen_count": 1,
        }
        if rid == "T2":
            cols["score_reasons"] = reasons_json(scored, "card", [
                ("Takes analyser software through IVDR certification.", "IVD analyser"),
                ("Fractional director, three days a week in Cambridge.", "Cambridge, 3 days on site")])
        if rid == "T1":
            scored = day("13 Nov", 8, 0)
            cols.update({
                "score_basis": "spec", "scored_at": scored, "ir35": "outside",
                "salary_rate": "£700–£750/day",
                "description_text": ADVERT, "description_fetched_at": day("13 Nov", 7, 50),
                "description_hash": radar_common.description_version(ADVERT),
                "score_reasons": reasons_json(scored, "spec", [
                    ("Leads the IEC 62304 class C software lifecycle.",
                     "You will own the IEC 62304 software development lifecycle for our class C pump firmware."),
                    ("Internal audit lead under ISO 13485.",
                     "Act as internal auditor for software processes under ISO 13485."),
                    ("Technical file for a notified body audit.",
                     "In your first three months you will prepare the software technical file ahead of our notified body audit in Q1."),
                    ("Two days a week in Guildford.",
                     "Hybrid, two days a week at our Guildford site.")]),
                "score_working": json.dumps({
                    "scored_at": scored, "cv_version": CV,
                    "asks": [{"ask": f"Ask {i}", "status": s, "lines": []}
                             for i, s in enumerate(["met"] * 12 + ["partly"] * 5 + ["missing"], 1)],
                    "counts": {"met": 12, "partly": 5, "missing": 1},
                    "rate_quote": "Day rate £700–£750, outside IR35.",
                    "rate_quote_from": "scorer"}),
                "seen_count": 2,
            })
            # Two older adverts on T1's chain: four sightings across three adverts.
            for h, (f1, l1) in enumerate((("21 Oct", "24 Oct"), ("29 Oct", "2 Nov"))):
                old = dict(cols)
                old.update({"url_hash": f"sample-t1-{h}", "first_seen": day(f1),
                            "last_seen": day(l1), "seen_count": 1,
                            "description_text": None, "description_hash": None,
                            "description_fetched_at": None, "score_working": None,
                            "score_reasons": None, "score_basis": "card",
                            "prompt_version": "pre-record", "score_model": "unknown",
                            "scored_at": None})
                insert(old)
            cols["first_seen"] = day("5 Nov")
        if rid == "Q4":
            # Q4 came back after expiring: its first advert expired 9 Oct.
            old = dict(cols)
            old.update({"url_hash": "sample-q4-old", "first_seen": day("12 Aug"),
                        "last_seen": day("25 Aug"), "seen_count": 2,
                        "expired_at": day("9 Oct"), "read_at": read_stamp})
            insert(old)
        ids[rid] = insert(cols)

    for rid, title, company, loc, rate, cv, first, applied, source in APPLIED:
        cid = radar_common.resolve_company(conn, company)
        cols = {"first_seen": day(first), "last_seen": day(first), "source": SOURCES[source],
                "company": company, "title": title, "location": loc, "salary_rate": rate,
                "source_url": f"https://www.linkedin.com/jobs/view/41000{n + 1:05d}/",
                "thread_type": "inbound", "cv_match": cv, "tier": "top", "failed_gates": "[]",
                "one_line_why": "Two days a week leading QA/RA for a SaMD triage app under UKCA and MDR."
                if rid == "A2" else "An application on file.",
                "gate_sector": 1, "gate_sector_note": "SaMD, Class IIa. In scope." if rid == "A2" else "In scope.",
                "gate_cv": 1, "gate_cv_note": f"{cv}. Regulatory work is on the CV.",
                "gate_location": 1, "gate_location_note": f"{loc}.",
                "gate_rate": 1, "gate_rate_note": f"{rate}, clears the floor.",
                "rate_band": "above", "engagement_type": "contract" if "day" in rate else "permanent",
                "role_key": radar_common.role_key(title, company), "company_id": cid,
                "score_basis": "card", "prompt_version": "pre-record", "score_model": "unknown",
                "red_flags": "[]", "read_at": read_stamp, "applied_at": day(applied, 10, 0),
                "seen_count": 2 if rid == "A2" else 1}
        ids[rid] = insert(cols)

    for rid, title, company, loc, cv, reason in FILTERED:
        cid = radar_common.resolve_company(conn, company)
        ids[rid] = insert({
            "first_seen": day("10 Nov"), "last_seen": day("10 Nov"), "source": "linkedin-alert",
            "company": company, "title": title, "location": loc, "salary_rate": None,
            "source_url": f"https://www.linkedin.com/jobs/view/42000{n + 1:05d}/",
            "thread_type": "inbound", "cv_match": cv, "tier": "filtered", "failed_gates": '["cv"]',
            "filter_reason": reason, "one_line_why": "Not a fit for the leadership search.",
            "gate_sector": 1, "gate_sector_note": "In scope.", "gate_cv": 0,
            "gate_cv_note": f"{cv}% is under the 40% floor", "gate_location": 1,
            "gate_location_note": loc, "gate_rate": 1, "gate_rate_note": "not stated",
            "rate_band": "unstated", "role_key": radar_common.role_key(title, company),
            "company_id": cid, "score_basis": "card", "prompt_version": "pre-record",
            "score_model": "unknown", "red_flags": "[]", "read_at": read_stamp})

    # The recently retired, for the archive and for Undo last.
    cid = radar_common.resolve_company(conn, "Burrows Vet Tech")
    ids["ACK"] = insert({
        "first_seen": day("3 Nov"), "last_seen": day("3 Nov"), "source": "linkedin-alert",
        "company": "Burrows Vet Tech", "title": "Software Manager, veterinary devices",
        "location": "Horsham", "salary_rate": "£650/day", "thread_type": "inbound",
        "source_url": "https://www.linkedin.com/jobs/view/4300000001/",
        "cv_match": 66, "tier": "reading", "failed_gates": '["cv", "rate"]',
        "one_line_why": "Seen it, not pursuing.", "gate_sector": 0,
        "gate_sector_note": "veterinary devices, out of scope", "gate_cv": 0,
        "gate_cv_note": "66% is under the 70% pass mark", "gate_location": 1,
        "gate_location_note": "Horsham", "gate_rate": 0,
        "gate_rate_note": "£650/day is under the £700 floor", "rate_band": "below",
        "role_key": radar_common.role_key("Software Manager, veterinary devices", "Burrows Vet Tech"),
        "company_id": cid, "score_basis": "card", "prompt_version": "pre-record",
        "score_model": "unknown", "red_flags": "[]", "read_at": read_stamp,
        "acknowledged_at": day("10 Nov", 12, 0)})
    cid = radar_common.resolve_company(conn, "Meridian Renal")
    insert({"first_seen": day("14 Oct"), "last_seen": day("7 Nov"), "source": "linkedin-alert",
            "company": "Meridian Renal", "title": "Software Lead, dialysis systems",
            "location": "Leeds", "thread_type": "inbound", "cv_match": 75, "tier": "top",
            "role_key": radar_common.role_key("Software Lead, dialysis systems", "Meridian Renal"),
            "company_id": cid, "read_at": read_stamp, "closed_at": day("9 Nov", 11, 0),
            "closed_source": "manual", "red_flags": "[]"})

    # Hartwell has a page: an insight and a touch on file.
    hartwell = radar_common.resolve_company(conn, "Hartwell Infusion Ltd")
    conn.execute("INSERT INTO signals (url_hash, first_seen, source_id, company, headline,"
                 " relevance, company_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("sample-sig-1", day("11 Nov"), "job-advert", "Hartwell Infusion Ltd",
                  "Buying window. Hartwell Infusion Ltd is hiring", 90, hartwell))
    conn.execute("INSERT INTO signals (url_hash, first_seen, source_id, company, headline,"
                 " relevance, company_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("sample-sig-2", day("14 Oct"), "google-alerts", "Hartwell Infusion Ltd",
                  "Hartwell Infusion raises £32m Series C", 81, hartwell))
    conn.execute("INSERT INTO touches (company, touched_at, channel, note, next_action,"
                 " next_action_date, company_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("Hartwell Infusion Ltd", day("30 Oct"), "comment",
                  "Commented on the Q3 hiring post", "Connection note to the VP Quality",
                  day("15 Nov")[:10], hartwell))
    # Where each company is, from its advert's location, so the region pass
    # places it as enrichment would; and a remote advert is marked remote.
    abroad = {"amsterdam": "Netherlands", "leuven": "Belgium"}
    for cid, loc in conn.execute(
            "SELECT company_id, MIN(location) FROM opportunities"
            " WHERE company_id IS NOT NULL GROUP BY company_id").fetchall():
        place = str(loc or "").split("(")[0].split(",")[0].strip()
        city = None if place.lower() == "remote" else place
        country = abroad.get((city or "").lower(), "United Kingdom")
        conn.execute("UPDATE companies SET city = ?, country = ? WHERE id = ?",
                     (city, country, cid))
    conn.execute("UPDATE opportunities SET location_class = 'remote'"
                 " WHERE location LIKE 'Remote%'")
    radar_common.recompute_regions(conn, radar_common.load_config(), force=True)
    # The inbox ran on schedule: an hour before the seed, so the top bar reads
    # as the design drew it whenever the seed is run.
    ran = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO runs (ts, workflow, mode, items_in, items_new, outcome)"
                 " VALUES (?, 'inbox', 'live', 12, 4, 'ok')", (ran,))
    conn.commit()
    conn.close()
    return ids


def main(argv=None) -> int:
    global SHIFT
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--today", help="the day to treat as the design's 13 Nov, YYYY-MM-DD")
    args = ap.parse_args(argv)
    path = Path(args.db)
    if path.exists():
        print(f"{path} exists. The seed writes only a new database.", file=sys.stderr)
        return 2
    today = (datetime.strptime(args.today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
             if args.today else datetime.now(timezone.utc).replace(
                 hour=0, minute=0, second=0, microsecond=0))
    SHIFT = today - DESIGN_TODAY
    ids = seed(path)
    print(json.dumps({"db": str(path), "ids": ids}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
