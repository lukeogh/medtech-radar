#!/usr/bin/env python
"""Acceptance checks for the Rate gate's three states and the tier worked out
in code (branch claude/rate-not-stated).

No model calls and no network. RADAR_MOCK is forced on, every reply a test
needs from a model is a stub, and radar_common.claude_call and push_ntfy are
replaced by functions that fail the run if anything reaches them outside a
stub. Every database is a throwaway under a temporary directory, and every
config points at the tracked preferences template.

What it checks:

- Rate has three states, set in code and never by the scorer: passes (a
  stated figure at or above its floor), fails (under it) and not stated (no
  figure on file, competitive included). A rate verdict or a question the
  reply carries anyway is not read.
- gate_state reads an older row whose scorer passed or failed a rate with no
  figure as not stated, so no page draws a tick or a cross for it.
- Each tier rule, and that the same four states always give the same tier,
  against a reference written from the brief over every combination.
- The question line asks only about the gate that fails, and a stored one
  that asks about pay is kept only where Rate fails.
- The re-sort is free (no model call, no push, no tokens in its run row) and
  repeatable (a second pass changes nothing and writes no history), and the
  roles the brief names land where it says.
- regate_rates.py --write refuses while a role has a number in its pay text
  but no figure on file, and --unclear-ok writes.
- The pages: the not stated mark's words, class and shape on Jobs and Home,
  the role pane's "Not stated", Prepare's mark, and no Passes or Fails for it.
- The scorer prompt asks for no rate verdict and no question, and its version
  is the new one.
"""

from __future__ import annotations

import argparse
import contextlib
import html
import io
import itertools
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

os.environ["RADAR_MOCK"] = "1"

REPO_ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts", "test"):
    if str(REPO_ROOT / sub) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / sub))

import radar_common as rc  # noqa: E402

TEMPLATE = REPO_ROOT / "config" / "preferences.template.md"
_real_load_config = rc.load_config
rc.load_config = lambda *a, **k: {**_real_load_config(*a, **k), "prefs_file": str(TEMPLATE)}

import jobs_page  # noqa: E402
import home_page  # noqa: E402
import prepare_page  # noqa: E402
import regate_rates  # noqa: E402
import score_item  # noqa: E402

CFG = rc.load_config()
FLOORS = {"day": 750.0, "salary": 100000.0}
TH = {"pass": 70, "floor": 40}
NS = rc.NOT_STATED
NEW_VERSION = "3c8e6c63"

failures: list[str] = []
reached: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


# Anything that would spend or send is a failure if it is reached.
def _no_model(*a, **k):
    if k.get("mock_fn") is not None:
        return k["mock_fn"](a[2] if len(a) > 2 else k.get("user_content")), {
            "model": "stub", "stop_reason": "end_turn"}
    reached.append("model")
    raise AssertionError("a model call was made")


def _no_push(*a, **k):
    reached.append("push")
    raise AssertionError("a push was sent")


rc.claude_call = _no_model
rc.push_ntfy = _no_push


# ------------------------------------------------------------- the scorer

def reply(**over) -> str:
    """A scorer reply. It carries a rate verdict and a question on purpose,
    the old shape, to show code reads neither."""
    base = {"company": "Acme Diagnostics", "role_title": "Head of Software",
            "location": "London", "gate_sector": True, "gate_sector_note": "IVD",
            "cv_match": 84, "gate_cv_note": "fits", "gate_location": True,
            "gate_location_note": "hybrid", "location_class": "hybrid",
            "gate_rate": False, "gate_rate_note": "below the floor, says the model",
            "rate_stated": True, "rate_value": 1, "rate_basis": "day-rate",
            "question_text": "What is the day rate on offer?",
            "one_line_why": "A fair fit.", "engagement_type": "permanent"}
    base.update(over)
    return json.dumps(base)


def score(pay: dict, **over) -> dict:
    opp = {"title": "Head of Software", "company": "Acme Diagnostics",
           "location": "London", "salary_rate": pay.get("text", ""), **{
               k: pay.get(k) for k in ("pay_currency", "pay_period", "pay_min", "pay_max")}}
    got, _usage, note = score_item.score_opportunity(
        opp, [], CFG, mock_fn=lambda _u: reply(**over), floors=FLOORS, thresholds=TH)
    assert got is not None, note
    return got


def scorer_checks() -> None:
    above = score({"pay_currency": "GBP", "pay_period": "day", "pay_min": 800, "pay_max": 850,
                   "text": "£800-£850 per day"})
    below = score({"pay_currency": "GBP", "pay_period": "year", "pay_min": 53474,
                   "pay_max": 53474, "text": "£53,474"})
    silent = score({})
    words = score({"text": "Competitive"})
    usd = score({"pay_currency": "USD", "pay_period": "year", "pay_min": 200000,
                 "pay_max": 200000, "text": "$200k"})
    check(above["gate_rate"] == 1 and above["rate_stated"] == 1
          and "at or above" in above["gate_rate_note"] and "judged in code" in above["gate_rate_note"],
          "a stated figure at or above its floor passes, in code, though the reply said fail")
    check(below["gate_rate"] == 0 and below["rate_stated"] == 1 and "below" in below["gate_rate_note"],
          "a stated figure under its floor fails, in code")
    check(all(r["gate_rate"] is None and r["rate_stated"] == 0 and r["rate_basis"] == "unstated"
              and r["rate_value"] is None and r["gate_rate_note"] == rc.RATE_NOT_STATED_NOTE
              for r in (silent, words)),
          "no figure and competitive with no figure are not stated (gate NULL, "
          "rate_stated 0, code's note), whatever the reply said")
    check(usd["gate_rate"] is None and usd["rate_stated"] == 0 and usd["rate_basis"] == "unstated"
          and usd["rate_value"] is None and usd["gate_rate_note"] == "$200,000 a year.",
          "a dollar figure with no exchange rate set is judged neither way, and its note is the "
          "rate as written, for Check the rate")
    check(silent["tier"] == "top" and silent["question_text"] is None
          and json.loads(silent["failed_gates"]) == [],
          "a role passing Sector, CV and Location with no rate stated is a Top prospect "
          "with no question, and Rate is not a failed gate")
    q = score({}, gate_location=False)
    check(q["tier"] == "question" and q["question_text"] == "Would they go remote",
          "Location failing with no rate stated is One question away, and code's question "
          "asks about location only, not the reply's rate question")
    r = score({"pay_currency": "GBP", "pay_period": "day", "pay_min": 600, "pay_max": 600})
    check(r["tier"] == "question" and r["question_text"] == "Would they move on the rate",
          "a rate under its floor alone is One question away, asking about the rate")
    both = score({"pay_currency": "GBP", "pay_period": "day", "pay_min": 600, "pay_max": 600},
                 gate_location=False)
    check(both["tier"] == "reading" and both["question_text"] is None,
          "Location and Rate both failing is Reading, with no question")
    reasons = json.loads(silent["score_reasons"] or "null") if silent.get("score_reasons") else None
    check(reasons is None or reasons.get("gates", {}).get("rate") is None,
          "the gates kept beside the reasons hold the rate as code judged it")


# ---------------------------------------------------------------- states

def state_checks() -> None:
    fig = {"pay_currency": "GBP", "pay_period": "day", "pay_min": 800, "pay_max": 800}
    rows = {
        "passes": ({"tier": "top", "gate_rate": 1, **fig}, rc.PASSES),
        "fails": ({"tier": "question", "gate_rate": 0, **fig}, rc.FAILS),
        "old pass, no figure": ({"tier": "top", "gate_rate": 1, "rate_stated": 0}, NS),
        "old fail, no figure": ({"tier": "reading", "gate_rate": 0}, NS),
        "re-sorted, no figure": ({"tier": "top", "gate_rate": None, "rate_stated": 0}, NS),
        "never scored": ({}, rc.NOT_JUDGED),
        "monthly figure": ({"tier": "reading", "gate_rate": 0, "pay_currency": "GBP",
                            "pay_period": "month", "pay_min": 7300, "pay_max": 7300}, NS),
    }
    for label, (row, want) in rows.items():
        got = rc.gate_state(row, "rate")
        check(got == want, f"gate_state reads {label} as {want} (got {got})")
    check(rc.gate_state({"tier": "top", "gate_location": None}, "location") == rc.NOT_JUDGED
          and rc.gate_state({"tier": "top", "gate_sector": 0}, "sector") == rc.FAILS,
          "Sector and Location cannot be not stated today, a missing verdict is not judged")
    check(set(rc.STATABLE_GATES) == {"rate"},
          "only Rate is in the table of gates that can be not stated, so Sector or "
          "Location join it with one entry")


# ------------------------------------------------------------------ tiers

def reference(g: dict, cv=84, real=True, floor=40) -> str:
    """The brief's rule, written apart from the code."""
    if cv is not None and cv < floor:
        return "filtered"
    if not real:
        return "filtered"
    p = lambda n: g[n] is True  # noqa: E731
    f = lambda n: g[n] is False  # noqa: E731
    if p("sector") and p("cv") and p("location") and (p("rate") or g["rate"] == NS):
        return "top"
    if p("sector") and p("cv") and (f("location") + f("rate")) == 1:
        return "question"
    return "reading"


def tier_checks() -> None:
    G = lambda **k: {"sector": True, "cv": True, "location": True, "rate": True, **k}  # noqa: E731
    cases = [
        ("all four pass", G(), "top"),
        ("Rate not stated, the rest pass", G(rate=NS), "top"),
        ("Location fails, Rate not stated", G(location=False, rate=NS), "question"),
        ("Rate fails, Location passes", G(rate=False), "question"),
        ("Location fails, Rate passes", G(location=False), "question"),
        ("Location and Rate both fail", G(location=False, rate=False), "reading"),
        ("Sector fails", G(sector=False, rate=NS), "reading"),
        ("CV fails", G(cv=False), "reading"),
        ("Sector fails, Rate passes", G(sector=False, location=False), "reading"),
        ("Location not judged", G(location=None, rate=NS), "reading"),
        ("Sector not judged", G(sector=None), "reading"),
    ]
    for label, g, want in cases:
        got = score_item.derive_tier(g, 84, True)["tier"]
        check(got == want, f"{label} gives {want} (got {got})")
    check(score_item.derive_tier(G(rate=NS), 30, True)["tier"] == "filtered"
          and score_item.derive_tier(G(rate=NS), 84, False)["tier"] == "filtered"
          and score_item.derive_tier(G(), 44, True, 45)["filter_reason"] == "cv match under 45",
          "Filtered is unchanged: a CV match under the floor, or not a real role")
    wrong = 0
    total = 0
    states = (True, False, None)
    for combo in itertools.product(states, states, states, (True, False, None, NS)):
        g = dict(zip(rc.GATE_NAMES, combo))
        for cv in (None, 10, 39, 40, 69, 70, 100):
            for real in (True, False):
                total += 1
                if score_item.derive_tier(g, cv, real)["tier"] != reference(g, cv, real):
                    wrong += 1
    check(wrong == 0, f"the tier matches the brief's rule on all {total} combinations "
                      f"of gate states, CV number and real role ({wrong} differ)")
    same = {score_item.derive_tier(G(location=False, rate=NS), 84, True)["tier"]
            for _ in range(3)}
    check(same == {"question"}, "the same four states always give the same tier")


def question_checks() -> None:
    G = lambda **k: {"sector": True, "cv": True, "location": True, "rate": True, **k}  # noqa: E731
    check(rc.question_for(G(location=False, rate=NS)) == "Would they go remote"
          and rc.question_for(G(rate=False)) == "Would they move on the rate"
          and rc.question_for(G(rate=NS)) == "",
          "code's question asks only about the gate that fails, never a rate not stated")
    asks_pay = "Is it open to remote working, and what is the day rate on offer?"
    loc_only = "Would they take three days a week in Cambridge?"
    check(rc.question_kept(asks_pay, G(location=False, rate=NS)) == "Would they go remote",
          "a stored question that also asks about pay, on a role whose rate is not stated, "
          "is replaced by code's")
    check(rc.question_kept(loc_only, G(location=False, rate=NS)) == loc_only,
          "a stored question about location alone is kept")
    check(rc.question_kept(loc_only, G(rate=False)) == "Would they move on the rate"
          and rc.question_kept("Would they move on the salary?", G(rate=False))
          == "Would they move on the salary?",
          "on a role whose rate fails, a stored question about pay is kept and one about "
          "something else is replaced")
    check(rc.asks_about_rate("accurate separate") is False
          and rc.asks_about_rate("what's the £ figure") is True,
          "accurate and separate are not pay words, the pound sign is")


# ---------------------------------------------------------------- re-sort

def seed(conn, **cols) -> int:
    row = {"url_hash": f"rate-{len(cols)}-{os.urandom(4).hex()}",
           "first_seen": "2026-11-01T09:00:00Z", "last_seen": "2026-11-01T09:00:00Z",
           "cv_match": 84, "gate_sector": 1, "gate_cv": 1, "gate_location": 1,
           "scored_at": "2026-11-02T09:00:00Z", **cols}
    conn.execute(f"INSERT INTO opportunities ({', '.join(row)}) VALUES"
                 f" ({', '.join('?' for _ in row)})", tuple(row.values()))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def row(conn, oid) -> dict:
    return dict(conn.execute("SELECT * FROM opportunities WHERE id = ?", (oid,)).fetchone())


def resort_checks(tmp: Path) -> None:
    db = tmp / "resort.sqlite"
    conn = rc.get_db(db)
    ids = {
        # Two London roles alike in their four facts, the scorer having passed
        # one's silent rate and failed the other's.
        "alike_pass": seed(conn, title="Senior Software Quality Engineer", company="Calder Medical",
                          gate_location=0, gate_rate=1, rate_stated=0, tier="question",
                          question_text="Is it open to hybrid working, and what is the day rate?"),
        "alike_fail": seed(conn, title="Software Safety Engineer", company="Hartley Search",
                           gate_location=0, gate_rate=0, rate_stated=0, tier="reading"),
        # Everything passes, and the scorer failed a silent rate and asked
        # about it.
        "all_pass": seed(conn, title="Technical Product Owner", company="Brightwell Imaging",
                        gate_rate=0, rate_stated=0, tier="question",
                        question_text="What is the day rate or salary on offer?"),
        # A stated salary under the floor, location failing: stays Reading.
        "under_floor": seed(conn, title="Diagnostics Lead", company="Device Agency", gate_location=0,
                     gate_rate=0, rate_stated=1, tier="reading", pay_currency="GBP",
                     pay_period="year", pay_min=47776, pay_max=53474,
                     salary_rate="£47,776 - £53,474 p.a."),
        # A stated salary over the floor, sector failing: stays Reading.
        "over_floor": seed(conn, title="Director of Software Engineering", company="Ostrava Software",
                      gate_sector=0, gate_location=0, gate_rate=1, rate_stated=1,
                      tier="reading", pay_currency="GBP", pay_period="year",
                      pay_min=115000, pay_max=115000, salary_rate="£115K/year"),
        # Applied for: keeps its tier whatever the rule says now.
        "applied": seed(conn, title="Applied Role", company="Elsewhere", gate_rate=0,
                        rate_stated=0, tier="question", applied_at="2026-11-03T09:00:00Z"),
    }
    calls_before = len(reached)
    dry = regate_rates.regate(conn, CFG, FLOORS, write=False, thresholds=TH)
    check(all(row(conn, i)["tier"] == t for i, t in
              ((ids["alike_pass"], "question"), (ids["alike_fail"], "reading"),
               (ids["all_pass"], "question"))),
          "a dry run writes nothing")
    check(dry["tiers_before"] == {"question": 2, "reading": 3}
          and dry["tiers_after"] == {"question": 2, "reading": 2, "top": 1}
          and dry["rate_before"] == {"fails": 3, "passes": 2}
          and dry["rate_after"] == {"fails": 1, "not stated": 3, "passes": 1},
          f"the dry run counts the tiers and the Rate gate before and after "
          f"({dry['tiers_before']} {dry['tiers_after']} {dry['rate_before']} {dry['rate_after']})")
    runs_before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    first = regate_rates.regate(conn, CFG, FLOORS, write=True, thresholds=TH)
    m, b, s = row(conn, ids["alike_pass"]), row(conn, ids["alike_fail"]), row(conn, ids["all_pass"])
    check(m["tier"] == "question" and b["tier"] == "question",
          "two roles alike in their four facts, one rate passed by the scorer and one failed, are both "
          "One question away now")
    check(all(r["gate_rate"] is None and r["rate_stated"] == 0
              and rc.gate_state(r, "rate") == NS for r in (m, b, s)),
          "their Rate gates are not stated, never a pass or a fail")
    check(m["question_text"] == "Would they go remote" and b["question_text"] == "Would they go remote",
          "their question asks about location only, the stored one that asked about pay replaced")
    check(s["tier"] == "top" and s["question_text"] is None,
          "a role passing the rest with its rate failed by the scorer and no figure is a Top prospect with no question")
    mh, ac = row(conn, ids["under_floor"]), row(conn, ids["over_floor"])
    check(mh["tier"] == "reading" and mh["gate_rate"] == 0 and "below" in mh["gate_rate_note"],
          "the stated salary under the floor with location failing stays in Reading with a Rate cross")
    check(ac["tier"] == "reading" and ac["gate_rate"] == 1,
          "the stated salary over the floor with sector failing stays in Reading with a Rate tick")
    check(row(conn, ids["applied"])["tier"] == "question" and row(conn, ids["applied"])["gate_rate"] == 0,
          "a role applied for is left alone")
    hist = conn.execute("SELECT COUNT(*) FROM score_history WHERE writer = 'regate_rates'").fetchone()[0]
    check(first["rows_changed"] == 5 and hist == first["history_rows_written"] and hist >= 4,
          f"each changed role keeps its old score in score_history first "
          f"({first['rows_changed']} changed, {hist} kept)")
    run = conn.execute("SELECT * FROM runs WHERE workflow = 'regate' ORDER BY id DESC LIMIT 1").fetchone()
    check(run is not None and not (run["input_tokens"] or 0) and not (run["output_tokens"] or 0),
          "the re-sort is one regate run with no tokens")
    second = regate_rates.regate(conn, CFG, FLOORS, write=True, thresholds=TH)
    hist2 = conn.execute("SELECT COUNT(*) FROM score_history WHERE writer = 'regate_rates'").fetchone()[0]
    check(second["rows_changed"] == 0 and second["tier_moves_total"] == 0 and hist2 == hist,
          "a second re-sort changes nothing and writes no history: it is repeatable")
    check(len(reached) == calls_before and conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
          == runs_before + 1,
          "no model call and no push was reached, and the second pass logged no run")
    conn.close()


def cli_checks(tmp: Path) -> None:
    db = tmp / "cli.sqlite"
    conn = rc.get_db(db)
    unclear = seed(conn, title="Software Test Manager", company="Somewhere", gate_sector=0,
                   gate_cv=0, gate_location=0, gate_rate=0, rate_stated=1, tier="reading",
                   salary_rate="up to £7.3K/month", pay_currency="GBP", pay_period="month",
                   pay_min=7300, pay_max=7300)
    silent = seed(conn, title="Lead", company="Quiet Co", gate_rate=0, rate_stated=0,
                  tier="question")
    conn.close()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = regate_rates.main(["--write", "--db", str(db)])
    conn = rc.get_db(db)
    check(code == 2 and row(conn, silent)["tier"] == "question"
          and "Nothing written" in err.getvalue() and '"rate_unclear": 1' in out.getvalue(),
          "--write refuses and writes nothing while a role has a number in its pay text "
          "but no figure on file, and lists it")
    conn.close()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        code = regate_rates.main(["--write", "--unclear-ok", "--db", str(db)])
    conn = rc.get_db(db)
    check(code == 0 and row(conn, silent)["tier"] == "top"
          and rc.gate_state(row(conn, unclear), "rate") == NS,
          "--unclear-ok writes, and the monthly figure no floor covers reads not stated")
    conn.close()


# ------------------------------------------------------------------ pages

def page_checks() -> None:
    o = {"id": 7, "tier": "top", "title": "Technical Product Owner", "company": "Acme",
         "gate_sector": 1, "gate_cv": 1, "gate_location": 1, "gate_rate": 0,
         "gate_rate_note": "rate unstated, that's your first question", "rate_stated": 0,
         "one_line_why": "A strong fit on the evidence.", "cv_match": 84,
         "question_text": "What is the day rate?"}
    row = jobs_page.row_html(o, "open", None, rc.now_iso())
    check('class="mk ' not in row and "Rate passes" not in row and "Rate fails" not in row
          and "Rate not stated" not in row,
          "the Jobs row carries no gate marks (owner, 17 Nov): the role pane says the rate")
    check('<symbol id="st-ask"' in jobs_page.SYMBOLS,
          "the not stated shape is a symbol of its own")
    check(jobs_page.lead_line(o) == "A strong fit on the evidence.",
          "a Top prospect shows no question, its stored one included")
    q = {**o, "tier": "question", "gate_location": 0,
         "question_text": "Is it remote, and what's the salary?"}
    check(jobs_page.lead_line(q) == "Would they go remote.",
          "a One question away row asks only about the gate that fails")
    pane = jobs_page.score_html(o)
    rate_part = pane[pane.index('gate-name">Rate'):]
    check("Not stated" in rate_part[:400] and "Passes" not in rate_part[:400]
          and "Fails" not in rate_part[:400] and 'gate-status is-cau"' in rate_part[:400]
          and ".is-cau{color:var(--caution)}" in jobs_page.JOBS_CSS,
          "the role pane's Rate gate reads Not stated, in amber, the caution colour, with "
          "no Passes or Fails (owner, 17 Nov)")
    check(rc.RATE_NOT_STATED_NOTE in html.unescape(rate_part[:600]),
          "and code's note beside it, not the older score's")
    from datetime import datetime, timezone
    item = {"kind": "role", "id": "role-1", "key": "role:top:k", "title": "T", "role": {"id": 1, **o}}
    body = home_page.role_row(item, {"now": datetime.now(timezone.utc), "show": "todo"})
    check('class="mk ' not in body and "Rate passes" not in body and "Rate fails" not in body,
          "Home's role row, the Jobs row, carries no gate marks either")
    check(prepare_page._gate_mark(rc.NOT_STATED) == '<span class="none">? Not stated</span>'
          and "Passes" not in prepare_page._gate_mark(rc.NOT_STATED),
          "Prepare's gates table marks a rate not stated with its own words")
    check(prepare_page._copy_state({**o}, {"gates": {"rate": 0}}, "rate") == rc.NOT_STATED,
          "a tailored copy of a role whose rate is not stated reads not stated too")
    for text in ("Rate not stated, that's your first question", rc.RATE_NOT_STATED_NOTE):
        check(not re.search(r"[—;!]", text), f"no em dash, semicolon or exclamation in {text!r}")


# ----------------------------------------------------------------- prompt

def prompt_checks() -> None:
    text = (REPO_ROOT / "prompts" / "scorer.md").read_text(encoding="utf-8")
    card, _m, spec = text.partition(score_item.SPEC_MARKER)
    check('"gate_rate"' not in card and '"question_text"' not in card
          and '"rate_stated"' not in card and "Unstated passes" not in card,
          "the scorer prompt asks for no rate verdict, no rate note and no question")
    check("Rate is judged in code" in card and "v2.1, 16 November 2026" in card,
          "it says Rate is code's, and its header carries the new version")
    check(rc.prompt_version() == NEW_VERSION,
          f"the prompt version is {NEW_VERSION}, stamped on every score from now on "
          f"(got {rc.prompt_version()})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="run_all.sh passes it; this runner builds its own")
    ap.parse_args(argv)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        for part in (scorer_checks, state_checks, tier_checks, question_checks,
                     page_checks, prompt_checks):
            part()
        resort_checks(Path(tmp))
        cli_checks(Path(tmp))
    check(not reached, f"nothing reached a model or a push ({reached})")
    print()
    if failures:
        print(f"FAIL. {len(failures)} rate checks failed.")
        return 1
    print("PASS. Every rate check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
