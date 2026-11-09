"""Re-run the rate gate and the tier over open rows. No model call.

The two floors replaced one converted floor. Every open row that stored a
pay figure is gated again in code, a day rate against the day floor, an
hourly rate times eight against the day floor, an annual salary against
the salary floor, and its tier is derived again from the stored gates.
A row with no stored figure keeps the gate the scorer gave it and still
has its tier derived again, because the converted-salary exception that
kept salary fails out of One question away is gone.

Open means not closed, dismissed or expired. Acknowledged rows are open.
Rows with no tier were never gated and are left for rescore.py.

  python scripts/regate_rates.py            dry run, counts and a sample
  python scripts/regate_rates.py --write    write the new gates and tiers
  --db PATH                                 alternate database path
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import radar_common as rc  # noqa: E402
import score_item  # noqa: E402

_GATE_COLS = ("gate_rate", "gate_rate_note", "rate_stated", "rate_value",
              "rate_basis", "rate_band", "tier", "failed_gates",
              "filter_reason", "question_text")


def _flag(value):
    return None if value is None else bool(value)


def judge_row(row: dict, floors: dict, config: dict | None = None) -> tuple[dict, dict]:
    """The rate gate and the tier for one row, from its stored columns.

    Returns (new, pay): new holds a value for every column in _GATE_COLS,
    pay is gate_pay's verdict, gate None when the row states no usable
    figure. Shared with the page checker, which calls it on a row that
    has just been given a figure off the page. No model call.
    """
    gates = {"sector": _flag(row.get("gate_sector")),
             "cv": _flag(row.get("gate_cv")),
             "location": _flag(row.get("gate_location")),
             "rate": _flag(row.get("gate_rate"))}
    new = {c: row.get(c) for c in _GATE_COLS}
    pay = rc.gate_pay(row.get("pay_currency"), row.get("pay_period"),
                      row.get("pay_min"), row.get("pay_max"), floors, config)
    if pay["gate"] is not None:
        gates["rate"] = pay["gate"]
        new.update(gate_rate=int(pay["gate"]),
                   gate_rate_note=score_item.rate_gate_note(pay),
                   rate_stated=1, rate_value=pay["figure"],
                   rate_basis=pay["basis"], rate_band=pay["band"])
    cv = row.get("cv_match")
    is_real = bool(gates["sector"]) or (cv is not None and cv >= score_item.CV_FLOOR_FOR_READING)
    tier_info = score_item.derive_tier(gates, cv, is_real)
    new["tier"] = tier_info["tier"]
    new["failed_gates"] = json.dumps(tier_info["failed_gates"])
    new["filter_reason"] = tier_info["filter_reason"]
    if tier_info["tier"] == "question":
        new["question_text"] = (row.get("question_text")
                                or score_item.question_for(gates, {}))
    else:
        new["question_text"] = None
    return new, pay


def regate(conn, config: dict, floors: dict, write: bool = False) -> dict:
    rows = conn.execute(
        "SELECT * FROM opportunities WHERE tier IS NOT NULL"
        " AND closed_at IS NULL AND dismissed_at IS NULL"
        " AND expired_at IS NULL ORDER BY id").fetchall()
    moves: dict[str, int] = {}
    gate_flips = {"pass to fail": 0, "fail to pass": 0}
    judged = 0
    sample = []
    updates = []
    for row in rows:
        row = dict(row)
        had = _flag(row.get("gate_rate"))
        new, pay = judge_row(row, floors, config)
        if pay["gate"] is not None:
            judged += 1
            if had is True and not pay["gate"]:
                gate_flips["pass to fail"] += 1
            elif had is False and pay["gate"]:
                gate_flips["fail to pass"] += 1
        if new["tier"] != row["tier"]:
            key = f"{row['tier']} to {new['tier']}"
            moves[key] = moves.get(key, 0) + 1
            if len(sample) < 10:
                sample.append({"id": row["id"], "title": row.get("title"),
                               "company": row.get("company"),
                               "pay": row.get("salary_rate"),
                               "from": row["tier"], "to": new["tier"]})
        if any(new[c] != row.get(c) for c in _GATE_COLS):
            updates.append((*(new[c] for c in _GATE_COLS), row["id"]))
    if write and updates:
        conn.executemany(
            "UPDATE opportunities SET " + ", ".join(f"{c} = ?" for c in _GATE_COLS)
            + " WHERE id = ?", updates)
        conn.commit()
        rc.log_run(conn, "regate", mode="live", items_in=len(rows),
                   items_new=sum(moves.values()),
                   note=f"floors day {floors['day']:g} salary {floors['salary']:g}, "
                        + (", ".join(f"{n} {k}" for k, n in sorted(moves.items()))
                           or "no tier moved"))
    return {"mode": "write" if write else "dry-run",
            "floors": floors, "open_rows": len(rows),
            "judged_in_code": judged, "rows_changed": len(updates),
            "gate_flips": gate_flips, "tier_moves": dict(sorted(moves.items())),
            "tier_moves_total": sum(moves.values()), "sample": sample}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true",
                    help="write the new gates and tiers, otherwise report only")
    ap.add_argument("--db", metavar="PATH", help="alternate database path")
    args = ap.parse_args(argv)
    rc.load_env()
    config = rc.load_config()
    floors = rc.read_rate_floors(config)  # loud failure before anything else
    conn = rc.get_db(Path(args.db).resolve()) if args.db else rc.get_db()
    print(json.dumps(regate(conn, config, floors, write=args.write), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
