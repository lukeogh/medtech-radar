"""Invented experience bank entries for the page snapshots and the Prepare
checks. Nothing here is the owner's: every name, place and document is made up.

Nine entries, read through the real paste reader: six backed (all with a tag
the Hartwell advert in seed_jobs_sample names, so Prepare's fold has more than
five), two to review and one parked. Each draft and the parked entry also
carries a tag the advert names, so a check can prove they never show there.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import experience  # noqa: E402

SAMPLE = """=== ENTRY ===
Title: Rebuilt the IEC 62304 lifecycle for a class C pump
What I did: Wrote the software development plan and the procedures under it. Trained twelve engineers on them.
Result: Passed the notified body audit with no software findings.
When: 2023 to 2024
Where: Fenwick Devices
Tags: IEC 62304, ISO 14971, class C, embedded firmware
Stage: Delivered
Evidence: Audit report | Quality records | no software findings | Describe only
Evidence: Software development plan | Fenwick QMS | the plan as approved | Can show
Vouch: Former VP Quality
Questions: None
=== ENTRY ===
Title: Led ISO 14971 risk management for software hazards
What I did: Ran the hazard analysis and kept the risk file current through two releases.
Result: Not known
When: 2022 to 2024
Where: Fenwick Devices
Tags: ISO 14971, risk management
Stage: Delivered
Evidence: Risk management report | Quality records | software hazards closed | Describe only
Vouch: Nobody named
Questions: None
=== ENTRY ===
Title: Prepared the software part of an FDA 510(k)
What I did: Wrote the software documentation section and answered the reviewer's questions.
Result: Cleared first time.
When: 2021
Where: Larch Medical
Tags: FDA 510(k), IEC 62304
Stage: Delivered
Evidence: Clearance letter | Public FDA database | the clearance | Public
Vouch: Head of Regulatory
Questions: None
=== ENTRY ===
Title: Set up traceability in Polarion
What I did: Moved requirements and test records into Polarion and linked them end to end.
Result: Audit trace time went from days to an hour.
When: 2022
Where: Fenwick Devices
Tags: Polarion, traceability
Stage: Delivered
Evidence: Trace matrix export | Fenwick Polarion | the links | Describe only
Vouch: Nobody named
Questions: How long did the trace take before?
=== ENTRY ===
Title: Led a team of software test engineers
What I did: Hired and led a team of five test engineers.
Result: Not known
When: 2020 to 2024
Where: Fenwick Devices
Tags: team leadership, hardware-in-the-loop
Stage: Delivered
Vouch: Former Head of Engineering
Questions: None
=== ENTRY ===
Title: Introduced MISRA C checks in the build
What I did: Added static analysis to every build with a deviation record.
Result: Deviations fell by half in a year.
When: 2023
Where: Fenwick Devices
Tags: MISRA C, static analysis
Stage: Delivered
Evidence: Build dashboard | Fenwick CI | the trend | Describe only
Vouch: Nobody named
Questions: None
=== ENTRY ===
Title: Drafted a cybersecurity plan under IEC 81001-5-1
What I did: Wrote the first draft of the plan.
Result: Not known
When: 2024
Where: Fenwick Devices
Tags: IEC 81001-5-1, cybersecurity
Stage: Drafted
Evidence: Draft plan | Personal notes | the draft | Describe only
Vouch: Nobody named
Questions: Was the plan adopted?
=== ENTRY ===
Title: Talked through an ISO 13485 internal audit programme
What I did: Discussed an audit programme with the quality team.
Result: Not known
When: 2024
Where: Fenwick Devices
Tags: ISO 13485, internal audit
Stage: Discussed
Vouch: Nobody named
Questions: None
=== ENTRY ===
Title: Reviewed IEC 60601-1 test reports
What I did: Read and commented on the test house reports.
Result: Not known
When: 2019
Where: Larch Medical
Tags: IEC 60601-1
Stage: Delivered
Evidence: Review comments | Email | the comments | Describe only
Vouch: Nobody named
Questions: None
"""

# Which of the nine, in order, end where: backed, draft or parked.
FATE = ("backed", "backed", "backed", "backed", "backed", "backed", "draft", "draft", "parked")


def seed(conn, now: str) -> dict:
    """The nine entries, their statuses moved through the real save. Returns
    {"ids": [...], "backed": first backed id, "draft": first draft id}."""
    result = experience.add_pasted(conn, SAMPLE, now)
    assert result["added"] == len(FATE) and not result["refused"], result
    ids = result["ids"]
    for eid, fate in zip(ids, FATE):
        if fate == "draft":
            continue
        e = experience.get_entry(conn, eid)
        body = {k: e[k] for k in experience.TEXT_FIELDS}
        body.update(id=eid, stage=e["stage"], tags=e["tag_list"], evidence=e["evidence"], status=fate)
        moved = experience.save_entry(conn, body, now)
        assert moved["ok"], moved
    conn.commit()
    return {"ids": ids, "backed": ids[0], "draft": ids[6], "parked": ids[8]}
