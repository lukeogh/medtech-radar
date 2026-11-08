"""Engagement type from an advert's own words. Contract, permanent or unknown.

The scorer prompt asks the model for the same field on every new row. This
module is the deterministic half: the rules the prompt states, as regular
expressions, so the backlog can be set without a model call and so a test
can pin each rule. Never guess. No match means unknown.

Rules, in precedence order:
  fixed term, fixed-term, FTC         permanent, with "fixed term" kept as the note
  day rate, per day, /day, p/d        contract
  interim                             contract
  inside IR35, outside IR35, IR35     contract
  contract, contractor, contracting   contract
  permanent, perm                     permanent

Fixed term is checked first because "fixed term contract" contains the
word contract and the brief reads it as permanent.

Backfill, over the stored rows, no API call:
  python scripts/engagement.py --backfill            dry run, counts only
  python scripts/engagement.py --backfill --write    set engagement_type
  --db PATH                                          alternate database

The only advert text the database holds per row is the title, the
location and the verbatim pay string, plus the ir35 column the scorer
recorded off the advert. The backfill scans the title, the pay string
and the ir35 word. It does not scan the model's notes, because those are
commentary about the advert, not the advert.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import radar_common as rc  # noqa: E402

VALUES = ("contract", "permanent", "unknown")

_RULES = (
    (re.compile(r"\bfixed[\s-]?term\b|\bFTC\b", re.IGNORECASE), "permanent", "fixed term"),
    (re.compile(r"\bday[\s-]?rate\b|\bper day\b|/\s?day\b|\bp/?d\b|\ba day\b",
                re.IGNORECASE), "contract", "day rate"),
    (re.compile(r"\binterim\b", re.IGNORECASE), "contract", "interim"),
    (re.compile(r"\b(inside|outside)\s+IR35\b|\bIR35\b", re.IGNORECASE), "contract", "IR35"),
    (re.compile(r"\bcontract(?:or|ing)?\b", re.IGNORECASE), "contract", "contract"),
    (re.compile(r"\bpermanent\b|\bperm\b", re.IGNORECASE), "permanent", "permanent"),
)


def classify(text: str) -> tuple[str, str]:
    """(engagement_type, note). The note is the rule that fired, or empty.

    The note is the matched phrase as the rule names it, so a reader can
    see why a row reads contract or permanent and nothing is asserted that
    the text did not say.
    """
    text = text or ""
    for pattern, value, note in _RULES:
        match = pattern.search(text)
        if match:
            if note == "IR35":
                return value, match.group(0).strip()
            return value, note
    return "unknown", ""


def normalise(value) -> str:
    """Clamp a model's answer to the three allowed words."""
    word = str(value or "").strip().lower()
    return word if word in VALUES else "unknown"


def row_text(row) -> str:
    """The advert's own words on file for one row, nothing else."""
    parts = [row["title"] or "", row["salary_rate"] or ""]
    ir35 = (row["ir35"] or "").strip().lower()
    if ir35 in ("inside", "outside"):
        parts.append(f"{ir35} IR35")
    return " ".join(p for p in parts if p)


def backfill(conn, write: bool = False) -> dict:
    """Set engagement_type on every row that has none, from stored text."""
    rows = conn.execute(
        "SELECT id, title, salary_rate, ir35 FROM opportunities"
        " WHERE engagement_type IS NULL").fetchall()
    counts = {v: 0 for v in VALUES}
    notes: dict[str, int] = {}
    updates = []
    for row in rows:
        value, note = classify(row_text(row))
        counts[value] += 1
        if note:
            notes[note] = notes.get(note, 0) + 1
        updates.append((value, note or None, row["id"]))
    if write and updates:
        conn.executemany(
            "UPDATE opportunities SET engagement_type = ?, engagement_note = ?"
            " WHERE id = ? AND engagement_type IS NULL", updates)
        conn.commit()
        rc.log_run(conn, "engagement", mode="live", items_in=len(rows),
                   items_new=len(rows) - counts["unknown"],
                   note=", ".join(f"{n} {v}" for v, n in counts.items()))
    return {"mode": "write" if write else "dry-run", "examined": len(rows),
            "counts": counts, "matched_on": dict(sorted(
                notes.items(), key=lambda kv: -kv[1]))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill", action="store_true",
                    help="scan stored rows with no engagement_type")
    ap.add_argument("--write", action="store_true",
                    help="write the result, otherwise report only")
    ap.add_argument("--db", metavar="PATH", help="alternate database path")
    args = ap.parse_args(argv)
    if not args.backfill:
        ap.print_help()
        return 2
    conn = rc.get_db(Path(args.db).resolve()) if args.db else rc.get_db()
    print(json.dumps(backfill(conn, write=args.write), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
