"""Two invented roles added by hand, for the page snapshots: an Upwork contract
in dollars an hour, scored on its pasted advert by the mock scorer, and a
fixed price from a referral. Nothing here is real: every client, place and
advert is made up.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import hand_roles  # noqa: E402
import prepare_application  # noqa: E402
import radar_common as rc  # noqa: E402

UPWORK = {
    "link": "https://www.upwork.com/jobs/~01sample0000000001", "title": "Head of Software, medical device firmware",
    "advert": ("Head of Software for a medical device start-up.\n\n"
               "Lead a team of four firmware engineers through IEC 62304 and own the "
               "software development plan.\n\nRemote, with a monthly visit to the team."),
    "client": "", "location": "Remote", "type": "contract", "source": "upwork",
    "rate_min": "60", "rate_max": "90", "currency": "USD", "period": "hour",
}
FIXED = {
    "link": "", "title": "Software audit for a diagnostic device",
    "advert": ("A fixed price review of the software file for a diagnostic device "
               "before its notified body audit.\n\nFour weeks, Leeds or remote."),
    "client": "Larchmont Diagnostics", "location": "Leeds, UK", "type": "fixed-price",
    "source": "referral", "rate_min": "4000", "rate_max": "", "currency": "GBP", "period": "fixed",
}


def seed(db: Path, now: str) -> dict:
    """Both roles stored and scored through the real paths. {"upwork": id, "fixed": id}."""
    config = rc.load_config()
    conn = rc.get_db(db)
    try:
        ids = {}
        for key, body in (("upwork", UPWORK), ("fixed", FIXED)):
            out = hand_roles.add_role(conn, body, now, config)
            assert out.get("id"), out
            ids[key] = out["id"]
        conn.commit()
    finally:
        conn.close()
    for oid in ids.values():
        done = prepare_application.prepare(oid, db, config)
        assert done.get("scored"), done
    return ids
