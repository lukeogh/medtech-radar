#!/usr/bin/env python
"""The CV page, at /cv: the master CV as an A4 sheet of light paper, edited in
place section by section, with what acts on it around it.

From the CV final design (design/round-1 branch, design/cv/final, 15 Nov 2026)
and its decisions of 15 Nov: one master CV with dated versions (cv_store),
the sheet light paper inside the dark page (page_frame's .sheet-paper), a
change to a floor, the CV pass mark or the Filtered floor re-sorts the roles
already scored in code, for free, and only a location or sector change offers
a paid rescore, its count and cost first.

What is here:

- render_page, the page: the sheet with its toolbar (Add section, Rearrange,
  Design, Undo, Redo, the save state), the master and its facts, the History,
  Preferences and Word import tabs, the keys, and the notice for the roles
  still scored on an earlier version of the CV with Rescore affected.
- save_preferences, the four scoring lines of the preferences file and the
  re-sort that follows (regate_rates.regate with the CV gate judged again).
- save_section, the location and sector prose, and the offer it returns.
- start_rescore and rescore_status, Rescore affected: one confirmed run over
  every role a change reaches, in a thread of the server, on the card scorer.
- rescore_cost, the cost said before anything is spent, from the cost note
  and the token use the runs table holds.
- import_report, what a Word import reads and how it differs, before it is
  trusted.

serve_dashboard.py keeps thin calls to these and its routes. The editor on the
sheet is cv_page's, shared with Prepare application: one element per CV line,
.cvl[data-n], numbered as cv_store.cv_lines numbers the text, inside the
.cv-part wrappers the editor adds on both pages.

Nothing on the page spends money on its own. A rescore runs only from its
confirm, which states the count and the cost, and the route refuses a run
whose count has grown past the one confirmed.
"""

from __future__ import annotations

import difflib
import html
import json
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import radar_common  # noqa: E402
import build_dashboard  # noqa: E402
import cv_doc  # noqa: E402
import cv_page  # noqa: E402
import cv_store  # noqa: E402
import page_frame  # noqa: E402
import regate_rates  # noqa: E402
import rescore  # noqa: E402
import score_item  # noqa: E402

TIER_WORDS = {"top": "Top", "question": "One question away",
              "reading": "Reading", "filtered": "Filtered"}

# A card score, from docs/cost-note.md: about $0.011 with the prompt cache
# warm and $0.018 cold, the quoted reasons included. In US cents, as the cost
# note and the bill give them.
CARD_SCORE_CENTS = (1.1, 1.8)
# The most runs of stored token use the estimate looks back over.
COST_RUNS_LOOKED_AT = 20


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


# ------------------------------------------------------------- the words

def version_when(name: str | None) -> str:
    """When a master CV version was saved, from its dated name, or from the
    file's own date for a cv.txt or cv.pdf. Empty when there is no telling."""
    when = version_time(name)
    return f"{when.day} {when:%b %Y} at {when:%H:%M} UTC" if when else ""


def version_time(name: str | None) -> datetime | None:
    found = re.match(r"^cv-(\d{8})-(\d{6})(?:-\d+)?\.md$", name or "")
    if found:
        try:
            return datetime.strptime(found.group(1) + found.group(2),
                                     "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    path = cv_store.PROFILE_DIR / (name or "")
    if name and path.is_file():
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return None


def short_when(name: str | None) -> str:
    """13 Nov 08:52 UTC, or the name itself when it carries no date."""
    when = version_time(name)
    return f"{when.day} {when:%b} {when:%H:%M} UTC" if when else (name or "")


def day_of(name: str | None) -> str:
    when = version_time(name)
    return f"{when.day} {when:%b}" if when else (name or "an older CV")


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def cents_words(low: float, high: float) -> str:
    """about 42 to 69 cents; about $1.10 to $1.80 from a dollar up."""
    lo, hi = int(low), int(-(-high // 1))
    if hi < 1:
        return "under a cent"
    if hi >= 100:
        return f"about ${lo / 100:.2f} to ${hi / 100:.2f}"
    if lo == hi:
        return f"about {hi} cents"
    return f"about {max(lo, 0)} to {hi} cents"


# What the prompt cache costs beside the input price: a token read back from
# it is billed at a tenth, a token written to it at 1.25 times.
CACHE_READ_PRICE = 0.1
CACHE_WRITE_PRICE = 1.25


def dollars_of(usage: dict, model: str | None) -> float:
    """What a run's tokens cost, at the prices Home uses for the week, with the
    tokens written to the prompt cache at 1.25 times the input price."""
    p_in, p_out = build_dashboard.PRICES_PER_MTOK.get(model or "",
                                                      build_dashboard.PRICE_FALLBACK)
    return (((usage.get("input_tokens") or 0) * p_in
             + (usage.get("output_tokens") or 0) * p_out
             + (usage.get("cache_read_tokens") or 0) * p_in * CACHE_READ_PRICE
             + (usage.get("cache_write_tokens") or 0) * p_in * CACHE_WRITE_PRICE)
            / 1_000_000)


def rescore_cost(conn, n: int, config: dict | None = None) -> dict:
    """What rescoring n roles on their advert cards would cost, in US cents.

    The cost note's range for a card score, 1.1 cents with the prompt cache
    warm to 1.8 cold, widened to take in what the card rescores on file have
    actually cost a role: the runs table's live rescore runs, their tokens
    priced as Home prices the week, over the roles they reached. Returns
    {"low", "high", "words", "per_role_seen", "runs_seen"}.
    """
    low, high = n * CARD_SCORE_CENTS[0], n * CARD_SCORE_CENTS[1]
    seen = None
    runs = 0
    if conn is not None and n:
        try:
            rows = conn.execute(
                "SELECT model, items_in, input_tokens, output_tokens, cache_read_tokens,"
                " cache_write_tokens"
                " FROM runs WHERE workflow = 'rescore' AND mode = 'live'"
                " AND items_in > 0 AND input_tokens > 0"
                " ORDER BY id DESC LIMIT ?", (COST_RUNS_LOOKED_AT,)).fetchall()
        except sqlite3.Error:
            rows = []
        items = sum(r[1] for r in rows)
        if items:
            dollars = sum(dollars_of({"input_tokens": r[2], "output_tokens": r[3],
                                      "cache_read_tokens": r[4],
                                      "cache_write_tokens": r[5]}, r[0]) for r in rows)
            seen = dollars * 100 / items
            runs = len(rows)
            low, high = min(low, n * seen), max(high, n * seen)
    return {"low": low, "high": high, "words": cents_words(low, high),
            "per_role_seen": seen, "runs_seen": runs}


# ----------------------------------------------------------- reading the store

def _ro(db_path: Path | None):
    if db_path is None or not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM opportunities LIMIT 1")
        return conn
    except sqlite3.Error:
        return None


def affected_summary(conn, config: dict, scope: str) -> dict | None:
    """affected() and its cost, with the earlier versions the roles were
    scored on, for the notice and the confirm. None without a database."""
    if conn is None:
        return None
    try:
        found = rescore.affected(conn, config, scope)
    except sqlite3.Error:
        return None
    groups: dict = {}
    if found["ids"]:
        marks = ",".join("?" * len(found["ids"]))
        for row in conn.execute(
                "SELECT COALESCE(cv_version, '') AS v, COUNT(*) AS n FROM opportunities"
                f" WHERE id IN ({marks}) GROUP BY v ORDER BY v DESC", found["ids"]):
            groups[row["v"]] = row["n"]
    cost = rescore_cost(conn, len(found["ids"]), config)
    return {"scope": scope, "count": len(found["ids"]), "ids_hash": found["ids_hash"],
            "spec": found["spec"],
            "applied": found["applied"], "acknowledged": found["acknowledged"],
            "cv_version": found["cv_version"], "cost": cost,
            "groups": [{"version": v, "count": n, "day": day_of(v) if v else "no recorded CV"}
                       for v, n in groups.items()]}


def version_counts(conn) -> dict:
    """Open, scored roles not applied for, by the CV version their score names."""
    if conn is None:
        return {}
    try:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT cv_version, COUNT(*) FROM opportunities WHERE tier IS NOT NULL"
            " AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL"
            " AND cv_version IS NOT NULL AND " + radar_common.NOT_APPLIED_SQL
            + " GROUP BY cv_version")}
    except sqlite3.Error:
        return {}


def line_changes(old: list, new: list) -> dict:
    """How two versions' lines differ, blank lines aside: added, changed and
    removed, each a count, and the first few as (kind, line number, text)."""
    a = [line for line in old]
    b = [line for line in new]
    out = {"added": 0, "changed": 0, "removed": 0, "items": []}
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        olds = [x for x in a[i1:i2] if x]
        news = [x for x in b[j1:j2] if x]
        paired = min(len(olds), len(news)) if tag == "replace" else 0
        out["changed"] += paired
        out["added"] += len(news) - paired
        out["removed"] += len(olds) - paired
        for k in range(j1, j2):
            if b[k] and len(out["items"]) < 12:
                out["items"].append(("changed" if tag == "replace" and k - j1 < paired
                                     else "added", k + 1, b[k]))
        if tag in ("delete", "replace"):
            for x in olds[paired:]:
                if len(out["items"]) < 12:
                    out["items"].append(("removed", None, x))
    return out


def changes_words(c: dict) -> str:
    parts = []
    if c["added"]:
        parts.append(plural(c["added"], "line") + " added")
    if c["changed"]:
        parts.append(plural(c["changed"], "line") + " changed")
    if c["removed"]:
        parts.append(plural(c["removed"], "line") + " removed")
    return ", ".join(parts) or "No lines changed"


def _laid(text: str) -> list:
    try:
        return cv_store.cv_lines(cv_doc.render(cv_doc.load_doc(text)))
    except cv_doc.CvError:
        return cv_store.cv_lines(text)


def history_rows(active: str | None) -> list[dict]:
    """Every saved version, newest first, with its number and what changed
    from the one before it."""
    names = cv_store.history()
    rows = []
    texts = {}
    for name in names:
        try:
            texts[name] = (cv_store.PROFILE_DIR / name).read_text(encoding="utf-8")
        except OSError:
            texts[name] = None
    for i, name in enumerate(names):
        number = len(names) - i
        before = names[i + 1] if i + 1 < len(names) else None
        text = texts.get(name)
        if text is None:
            what = "Could not be read"
        elif before is None or texts.get(before) is None:
            what = f"First version, {plural(len([x for x in cv_store.cv_lines(text) if x]), 'line')}"
        else:
            what = changes_words(line_changes(_laid(texts[before]), _laid(text)))
        rows.append({"name": name, "number": number, "when": short_when(name),
                     "what": what, "active": name == active,
                     "laid_out": text is not None and cv_doc.is_layout(text)})
    return rows


# ------------------------------------------------------------- the preferences

def pref_values(config: dict) -> dict:
    scoring = radar_common.read_scoring_prefs(config)
    live = radar_common.live_prefs_path(config)
    changed = None
    if live.exists():
        when = datetime.fromtimestamp(live.stat().st_mtime, timezone.utc)
        changed = f"{when.day} {when:%b}"
    return {**scoring, "location": radar_common.read_pref_section("location", config),
            "sector": radar_common.read_pref_section("sector", config),
            "changed": changed, "path": _rel(live)}


def _rel(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(radar_common.REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def _floors_and_marks(config: dict, values: dict) -> tuple[dict, dict]:
    now = radar_common.read_scoring_prefs(config)
    merged = {k: float(values[k]) if k in values else now.get(k)
              for k in radar_common.SCORING_PREFS}
    if merged["day_rate_floor_gbp"] is None or merged["salary_floor_gbp"] is None:
        raise ValueError("The preferences file has no day_rate_floor_gbp or "
                         "salary_floor_gbp line to judge rates against. Set both.")
    floors = {"day": merged["day_rate_floor_gbp"], "salary": merged["salary_floor_gbp"]}
    marks = {"pass": merged["cv_pass_score"] if merged["cv_pass_score"] is not None
             else radar_common.DEFAULT_CV_PASS_SCORE,
             "floor": merged["cv_floor_score"] if merged["cv_floor_score"] is not None
             else radar_common.DEFAULT_CV_FLOOR_SCORE}
    marks = {k: int(v) if float(v) == int(v) else v for k, v in marks.items()}
    return floors, marks


def _moves_words(moves: dict) -> str:
    parts = []
    for key, n in sorted(moves.items(), key=lambda kv: -kv[1]):
        a, _to, b = key.partition(" to ")
        parts.append(f"{n} from {TIER_WORDS.get(a, a)} to {TIER_WORDS.get(b, b)}")
    return ", ".join(parts)


def save_preferences(body, config: dict, db_path: Path) -> dict:
    """Save the scoring lines and re-sort the roles already scored, in code.

    body is {"values": {key: number}, "dry_run": bool}. A dry run writes
    nothing, the file included, and says what saving would move. Otherwise the
    lines are written (radar_common.write_scoring_prefs, every other byte of
    the file kept), then every open, scored role that is not applied for is
    judged again by regate_rates.regate with the CV gate judged again too,
    each move kept in score_history first, writer cv_preferences, and the run
    logged as a regate. No model is asked anything.

    The Rate gate is code's on every role re-sorted: a figure on file passes
    or fails, no figure is not stated. Roles with no figure where the stored
    pay text holds a number are counted (rate_unclear), because they read not
    stated only for want of a figure Radar can judge. Roles never gated at all
    are counted too (ungated). No
    run on this page reaches the second: rescore.py --regate scores them, by
    hand on the box, and the note says so, with their cost.

    A database too busy to re-sort (another writer holding the lock past the
    busy timeout) leaves the file saved and the roles as they were, with
    resorted False, and the page keeps its Re-sort action until one goes in.
    """
    if not isinstance(body, dict):
        return {"ok": False, "invalid": True, "note": "Send the values to save."}
    dry = body.get("dry_run") is True
    try:
        values = radar_common.clean_scoring_prefs(body.get("values"), config)
        floors, marks = _floors_and_marks(config, values)
    except ValueError as err:
        return {"ok": False, "invalid": True, "note": str(err)}
    if not dry:
        try:
            radar_common.write_scoring_prefs(values, config)
        except (ValueError, OSError) as err:
            return {"ok": False, "note": str(err)}
    try:
        conn = radar_common.get_db(Path(db_path))
    except sqlite3.Error as err:
        return {"ok": not dry, "saved": not dry, "values": values, "resorted": False,
                "note": ("Saved, but the" if not dry else "The")
                        + f" roles could not be re-sorted: {err}. "
                        + ("No role moved. Re-sort now tries again." if not dry else "")}
    try:
        res = regate_rates.regate(conn, config, floors, write=not dry,
                                  thresholds=marks, cv_gate_again=True,
                                  writer="cv_preferences")
        # The roles rescore.py --regate would score: no tier, a score from
        # before the gates, open, not acknowledged, not applied for.
        ungated = conn.execute(
            "SELECT COUNT(*) FROM opportunities WHERE tier IS NULL"
            " AND (cv_match IS NOT NULL OR combined IS NOT NULL)"
            " AND acknowledged_at IS NULL"
            " AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL"
            " AND " + radar_common.NOT_APPLIED_SQL).fetchone()[0]
        applied = conn.execute(
            "SELECT COUNT(*) FROM opportunities WHERE tier IS NOT NULL"
            " AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL"
            " AND " + radar_common.APPLIED_SQL).fetchone()[0]
        ungated_cost = rescore_cost(conn, ungated) if ungated else None
    except sqlite3.OperationalError as err:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"ok": not dry, "saved": not dry, "values": values, "resorted": False,
                "note": ("Saved, but the" if not dry else "The")
                        + f" roles could not be re-sorted, the database is busy ({err}). "
                        + ("No role moved. Re-sort now tries again." if not dry else "")}
    finally:
        conn.close()
    moved = res["tier_moves_total"]
    words = []
    if dry:
        words.append(f"Saving would move {plural(moved, 'role')}"
                     + (f": {_moves_words(res['tier_moves'])}." if moved else "."))
    else:
        words.append("Saved. " + (f"{plural(moved, 'role')} moved: "
                                  f"{_moves_words(res['tier_moves'])}." if moved
                                  else "No role moved."))
    if res["rate_unclear"]:
        one = res["rate_unclear"] == 1
        words.append(f"{plural(res['rate_unclear'], 'role')} may state a rate Radar "
                     f"holds no figure for, so Rate reads not stated on {'it' if one else 'them'}.")
    if ungated:
        them = "it" if ungated == 1 else "them"
        words.append(f"{plural(ungated, 'role')} scored before the gates "
                     f"{'has' if ungated == 1 else 'have'} no tier, so no re-sort "
                     f"reaches {them} and nothing on this page rescores {them}: "
                     "python scripts/rescore.py --regate, run by hand on the box, "
                     f"scores {them} on the model, {ungated_cost['words']}.")
    if applied:
        words.append(f"{plural(applied, 'role')} you applied for "
                     f"{'keeps its' if applied == 1 else 'keep their'} tier.")
    return {"ok": True, "dry_run": dry, "saved": not dry, "resorted": not dry,
            "values": values, "moved": moved, "moves": res["tier_moves"],
            "judged": res["open_rows"], "rate_unclear": res["rate_unclear"],
            "ungated": ungated, "applied_left": applied,
            "history_rows_written": res["history_rows_written"],
            "note": " ".join(words)}


def save_section(body, config: dict, db_path: Path) -> dict:
    """Save the location or sector prose and offer the paid rescore it needs.

    The scorer judges the Location and Sector gates from these words, so
    nothing stored says how it would judge a role now: nothing is re-sorted.
    The answer carries the offer, every open role with its count and cost,
    and nothing runs until the owner confirms it.
    """
    if not isinstance(body, dict) or body.get("key") not in radar_common.PREF_SECTIONS:
        return {"ok": False, "invalid": True, "note": "key must be location or sector."}
    key = body["key"]
    try:
        text = radar_common.clean_pref_section(key, body.get("text"))
        radar_common.write_pref_section(key, text, config)
    except (ValueError, OSError) as err:
        return {"ok": False, "invalid": isinstance(err, ValueError), "note": str(err)}
    conn = _ro(Path(db_path))
    try:
        offer = affected_summary(conn, config, "open")
    finally:
        if conn is not None:
            conn.close()
    gate = "Location" if key == "location" else "Sector"
    note = (f"Saved. Roles scored from now on are judged on the new words. Radar "
            f"cannot tell which roles already scored this changes at the {gate} "
            "gate without asking the model again.")
    return {"ok": True, "key": key, "text": text, "offer": offer, "note": note}


def save_design(body) -> dict:
    try:
        design = cv_store.save_design(body)
    except cv_store.UploadError as err:
        return {"ok": False, "invalid": True, "note": str(err)}
    except OSError as err:
        return {"ok": False, "note": f"The design could not be saved: {err}"}
    return {"ok": True, "design": design}


# ------------------------------------------------------- Rescore affected

RUN: dict = {"state": "idle"}
_RUN_LOCK = threading.Lock()


def rescore_status() -> dict:
    with _RUN_LOCK:
        return {k: v for k, v in RUN.items() if k != "thread"}


def start_rescore(body, config: dict, db_path: Path, background_lock) -> dict:
    """Start one confirmed Rescore affected run, in a thread of the server.

    body is {"confirm": true, "scope": "stale" or "open", "expect": n,
    "ids_hash": h}, n the count the confirm stated and h the hash of the roles
    it was of (rescore.ids_hash). The run reaches exactly those roles:
    rescore.rescore_affected refuses a count that has grown, or another set of
    roles. Its cost so far is kept on the status as it goes. It shares
    background_lock with Check now, so two runs never overlap. The answer
    comes at once; rescore_status says how far it has got, and the page asks
    every second or two.
    """
    if not (isinstance(body, dict) and body.get("confirm") is True):
        return {"ok": False, "invalid": True,
                "note": "Rescoring spends real tokens. Send {\"confirm\": true} "
                        "with the scope and the count you were shown."}
    scope, expect = body.get("scope"), body.get("expect")
    confirmed = body.get("ids_hash")
    if scope not in rescore.AFFECTED_SCOPES or not isinstance(expect, int) \
            or isinstance(expect, bool) or not 0 <= expect <= 5000 \
            or not (isinstance(confirmed, str) and re.fullmatch(r"[0-9a-f]{16}", confirmed)):
        return {"ok": False, "invalid": True,
                "note": "scope must be stale or open, expect the count you were shown, "
                        "and ids_hash the roles it was of."}
    with _RUN_LOCK:
        if RUN.get("state") == "running":
            return {"ok": False, "outcome": "busy", "note": "A rescore is already running."}
        if not background_lock.acquire(blocking=False):
            return {"ok": False, "outcome": "busy",
                    "note": "Another background run is in progress. Try again in a minute."}
        RUN.clear()
        RUN.update(state="running", scope=scope, count=expect, done=0, failed=0,
                   left=0, unstored=0, started=radar_common.now_iso(), cost_cents=0.0,
                   mode="mock" if radar_common.mock_mode_active() else "live")
    usage: dict = {}
    tally: dict = {}
    model = config.get("claude_model_score")

    def progress(done, failed, left, _oid, _what):
        with _RUN_LOCK:
            RUN.update(done=done, failed=failed, left=left,
                       unstored=tally.get("unstored", 0),
                       cost_cents=dollars_of(usage, model) * 100)

    def work():
        conn = None
        try:
            conn = radar_common.get_db(Path(db_path))
            res = rescore.rescore_affected(conn, config, usage, scope, expect, progress,
                                           confirmed_hash=confirmed, tally=tally)
            spent = dollars_of(usage, model) * 100
            with _RUN_LOCK:
                if res.get("ok"):
                    RUN.update(state="done", result=res, cost_cents=spent,
                               done=res["rescored"], failed=res["failed"], left=res["left"],
                               unstored=res["unstored"], finished=radar_common.now_iso())
                else:
                    RUN.update(state="refused", result=res, note=res.get("note"),
                               finished=radar_common.now_iso())
        except Exception as err:  # noqa: BLE001  a failed run says so on the page
            with _RUN_LOCK:
                stored = RUN.get("done", 0)
                RUN.update(state="failed",
                           note=f"The run stopped: {type(err).__name__}: {err}. "
                                f"{plural(stored, 'role')} {'was' if stored == 1 else 'were'} "
                                "rescored and stored before it stopped; the rest keep "
                                "their earlier scores.",
                           cost_cents=dollars_of(usage, model) * 100,
                           finished=radar_common.now_iso())
        finally:
            if conn is not None:
                conn.close()
            background_lock.release()

    thread = threading.Thread(target=work, name="cv-rescore", daemon=True)
    with _RUN_LOCK:
        RUN["thread"] = thread
    thread.start()
    return {"ok": True, "outcome": "started", **rescore_status()}


# ---------------------------------------------------------- the Word import

def import_report(markdown: str, active_text: str | None) -> dict:
    """What an import read, laid out as the editor lays a CV out, before it
    is trusted: how many lines, the guesses the layout made (headings read as
    a standard section, bullet marks), the lines that differ from the file,
    and how it differs from the version that is active."""
    raw = [cv_doc.clean(x) for x in cv_store.cv_lines(markdown)]
    raw = [x for x in raw if x]
    try:
        cv_doc.check_raw(markdown)
        doc = cv_doc.import_text(markdown)
        laid_text = cv_doc.render(doc)
        cv_doc.validate(doc)
        problem = None
    except cv_doc.CvError as err:
        laid_text, problem = None, str(err)
    guesses = []
    for line in raw:
        heading = cv_doc._heading_of(line)
        if heading and heading[1]:
            standard = cv_doc.section_title(heading[1])
            if standard and cv_doc._word_key(heading[1]) != cv_doc._word_key(standard):
                guesses.append({"from": heading[1], "to": standard})
    marks = sum(1 for x in raw if cv_doc._ANY_BULLET.match(x) and not x.startswith("- "))
    out = {"lines_read": len(raw), "guesses": guesses[:12], "bullet_marks": marks,
           "problem": problem, "laid_text": laid_text}
    if laid_text is not None:
        laid = cv_store.cv_lines(laid_text)
        keys = [cv_doc.line_key(x) for x in laid if x]
        rawkeys = [cv_doc.line_key(x) for x in raw]
        sm = difflib.SequenceMatcher(a=rawkeys, b=keys, autojunk=False)
        out["differ"] = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2
                            in sm.get_opcodes() if tag != "equal")
        out["lines"] = len(laid)
        if active_text is not None:
            c = line_changes(_laid(active_text), laid)
            out["against_active"] = {k: c[k] for k in ("added", "changed", "removed")}
            out["against_words"] = changes_words(c)
    return out


# ---------------------------------------------------------------- the page

def _icon(name: str, size: int = 16) -> str:
    paths = {
        "plus": '<path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.6"/>',
        "sort": '<path d="M5 3v10M2.5 10.5 5 13l2.5-2.5M11 13V3M8.5 5.5 11 3l2.5 2.5" stroke="currentColor" stroke-width="1.4" fill="none"/>',
        "pen": '<path d="M3 13l1-3 6.5-6.5 2 2L6 12z" stroke="currentColor" stroke-width="1.4" fill="none"/>',
        "undo": '<path d="M6 4 3 7l3 3M3.5 7H10a3 3 0 0 1 0 6H8" stroke="currentColor" stroke-width="1.4" fill="none"/>',
        "redo": '<path d="M10 4l3 3-3 3M12.5 7H6a3 3 0 0 0 0 6h2" stroke="currentColor" stroke-width="1.4" fill="none"/>',
        "ok": '<circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="M5.2 8.2 7 10l3.8-4" stroke="currentColor" stroke-width="1.5" fill="none"/>',
        "warn": '<path d="M8 2.2 14.2 13H1.8z" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="M8 6.4v3.2" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="11.4" r=".8" fill="currentColor"/>',
        "bad": '<circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="M5.8 5.8l4.4 4.4m0-4.4-4.4 4.4" stroke="currentColor" stroke-width="1.5"/>',
        "info": '<circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="M8 7.2v4" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="4.9" r=".8" fill="currentColor"/>',
        "tick": '<path d="M3.5 8.5 6.5 11.5 12.5 4.5" stroke="currentColor" stroke-width="1.8" fill="none"/>',
        "dot": '<circle cx="8" cy="8" r="3.2" fill="currentColor"/>',
    }
    return (f'<svg viewBox="0 0 16 16" width="{size}" height="{size}" aria-hidden="true" '
            f'focusable="false">{paths[name]}</svg>')


# The page has no single-key shortcuts (the owner's call, 16 Nov): Ctrl S saves
# a version, Ctrl Z and Ctrl Shift Z undo and redo, and no key is hinted.
ADD_CHOICES = ("Selected projects", "Talks and writing", "Languages", "Publications")


def _toolbar_html(editable: bool) -> str:
    dis = "" if editable else ' aria-disabled="true"'
    return f"""<div class="toolbar" role="toolbar" aria-label="CV" id="cv-toolbar">
<span class="wide-only">
<button type="button" class="btn btn-ghost" id="tb-add" aria-haspopup="menu" aria-expanded="false" aria-controls="add-menu"{dis}>{_icon('plus')}Add section</button>
<button type="button" class="btn btn-ghost" id="tb-rearrange" aria-haspopup="dialog"{dis}>{_icon('sort')}Rearrange</button>
<button type="button" class="btn btn-ghost" id="tb-design" aria-haspopup="dialog">{_icon('pen')}Design</button>
<span class="sep" aria-hidden="true"></span>
</span>
<button type="button" class="switch" role="switch" aria-checked="true" id="tb-numbers"><span class="track" aria-hidden="true"></span><span class="sw-word">Line numbers</span></button>
<button type="button" class="btn btn-ghost btn-icon" id="tb-undo" aria-label="Undo" aria-keyshortcuts="Control+Z" aria-disabled="true">{_icon('undo')}</button>
<button type="button" class="btn btn-ghost btn-icon" id="tb-redo" aria-label="Redo" aria-keyshortcuts="Control+Shift+Z" aria-disabled="true">{_icon('redo')}</button>
<span class="grow" aria-hidden="true"></span>
<span class="save-state is-ok" id="save-state" role="status"></span>
<span class="over-go" id="over-note" hidden></span>
<button type="button" class="btn btn-ghost" id="tb-discard" hidden>Discard</button>
<button type="button" class="btn btn-secondary" id="tb-reload" hidden>Reload</button>
</div>"""


def _history_html(rows: list, viewing: str | None, compare: dict | None,
                  counts: dict) -> str:
    items = []
    for r in rows:
        name = r["name"]
        roles = counts.get(name, 0)
        still = (f'{plural(roles, "open role")} still scored on it'
                 if roles and not r["active"] else "")
        tag = ('<span class="tag tag-ok">' + _icon("tick", 12) + "Active</span>" if r["active"]
               else '<span class="tag tag-accent">Viewing, read only</span>' if name == viewing
               else "")
        acts = "" if r["active"] else (
            f'<a class="btn btn-ghost" href="/cv?view={esc(name)}#history" '
            f'aria-label="View version {r["number"]}">View</a>'
            f'<a class="btn btn-ghost" href="/cv?compare={esc(name)}#history" '
            f'aria-label="Compare version {r["number"]} with the active version">Compare</a>')
        cls = " is-viewing" if name == viewing else ""
        vfile = page_frame.divided([f"Version {r['number']}", esc(name), still])
        items.append(f"""<li class="vrow{cls}"><div><p class="vwhen">{esc(r['when'])}</p>
<p class="vwhat">{esc(r['what'])}</p>
<p class="vfile">{vfile}</p></div>
<div class="vacts">{tag}{acts}</div></li>""")
    compare_html = ""
    if compare:
        lines = "".join(
            f'<li><span class="dk">{esc("Line " + str(n) if n else "Gone")}</span>'
            f'<span>{esc({"added": "Added: ", "changed": "Changed: ", "removed": "Removed: "}[k])}'
            f'&ldquo;{esc(t)}&rdquo;</span></li>' for k, n, t in compare["items"])
        compare_html = f"""<div class="dialog compare" id="compare" tabindex="-1">
<h4>Version {compare['number']} ({esc(compare['when'])}) against the active version</h4>
<p class="dlg-body">{esc(compare['words'])}.</p>
<ul class="diffs">{lines or '<li>Nothing differs.</li>'}</ul>
<p class="dlg-acts"><a class="btn btn-secondary" href="/cv#history">Close</a>
<a class="btn btn-ghost" href="/cv?view={esc(compare['name'])}#history">View version {compare['number']}</a></p></div>"""
    return (f"""{compare_html}
<ul class="vlist">{''.join(items) or '<li class="vrow"><p class="vwhat">No saved versions yet.</p></li>'}</ul>""")


def _num(v) -> str:
    return "" if v is None else radar_common._plain_number(v)


def _import_html(active_number: int) -> str:
    return f"""<div class="drop" id="import-drop">
<button type="button" class="btn btn-secondary" id="import-choose">Choose a file</button>
<input type="file" id="cv-file" class="visually-hidden" tabindex="-1" aria-hidden="true" accept=".md,.markdown,.txt,.docx,.pdf">
<p class="help">Word, Markdown, text or PDF, up to 5 MB</p>
</div>
<div id="import-reading" hidden><p class="help"><span id="import-name"></span> Reading the file.</p>
<div class="bar" role="progressbar" aria-label="Reading the file"><span style="width:50%"></span></div>
<button type="button" class="btn btn-ghost" id="import-cancel">Cancel</button></div>
<div id="import-failed" class="notice notice-bad" hidden role="alert"><div class="notice-body"><span class="notice-mark">{_icon('bad')}</span><p class="notice-text" id="import-failed-text"></p></div></div>
<div id="cv-stage" hidden>
<div class="notice notice-caution" role="status"><div class="notice-body"><span class="notice-mark">{_icon('warn')}</span><p class="notice-text" id="cv-note"></p></div></div>
<ul class="diffs" id="import-guesses"></ul>
<p class="help" id="import-against"></p>
<pre class="preview" id="cv-preview" tabindex="0" aria-label="The file as the editor will lay it out"></pre>
<p class="form-foot"><button type="button" class="btn btn-primary" id="cv-confirm">Save as version {active_number + 1}</button>
<button type="button" class="btn btn-ghost" id="cv-discard">Discard</button></p>
</div>"""


def _rescore_blocks(stale: dict | None, active: str | None, run: dict) -> str:
    """The earlier-CV notice, the shared confirm and the run's progress."""
    notice = ""
    if stale and stale["count"]:
        groups = stale["groups"]
        reads = "; their scores still read " + " and ".join(
            f"{g['count']} on the {g['day']} version" if g["version"].startswith("cv-")
            else f"{g['count']} on {g['version'] or 'no recorded CV'}" for g in groups[:3])
        n = stale["count"]
        notice = f"""<section class="notice notice-caution" id="stale-notice" aria-labelledby="stale-h">
<div class="notice-body"><span class="notice-mark">{_icon('warn')}</span>
<div class="notice-text"><p><strong id="stale-h">{plural(n, 'role')} {'was' if n == 1 else 'were'} scored on an earlier version of this CV.</strong>
You saved {esc(active or 'the master')} at {esc(short_when(active))}{esc(reads)}.</p></div></div>
<div class="notice-acts"><button type="button" class="btn btn-secondary" data-rescore="stale" aria-expanded="false" aria-controls="rescore-confirm">Rescore {plural(n, 'role')}, {esc(stale['cost']['words'])}</button></div>
</section>"""
    return notice + """<section class="notice notice-caution" id="rescore-confirm" hidden tabindex="-1" aria-labelledby="rescore-confirm-text">
<div class="notice-body"><span class="notice-mark">""" + _icon("warn") + """</span><div class="notice-text" id="rescore-confirm-text"></div></div>
<div class="notice-acts"><button type="button" class="btn btn-primary" id="rescore-go"></button><button type="button" class="btn btn-ghost" id="rescore-cancel">Cancel</button></div>
</section>
<section class="notice" id="rescore-run" hidden tabindex="-1" aria-labelledby="rescore-run-text">
<div class="notice-body"><span class="notice-mark" id="rescore-mark"></span><div class="notice-text"><p id="rescore-run-text"></p>
<div class="bar" id="rescore-bar" role="progressbar" aria-label="Rescore progress" aria-valuemin="0" aria-valuemax="1" aria-valuenow="0"><span style="width:0%"></span></div></div></div>
</section>
<p class="visually-hidden" id="rescore-live" role="status" aria-live="polite"></p>"""


def render_page(config: dict, db_path: Path | None, jobs: dict | None = None,
                query: dict | None = None) -> bytes:
    """The CV page as bytes. query may name a version to view (view) or to
    compare with the active one (compare). Read only, like every page."""
    query = query or {}
    active = cv_store.active_cv_name() or score_item.get_cv_version(config)
    marker = cv_store.active_cv_name()
    hist = history_rows(marker)
    names = [r["name"] for r in hist]
    viewing = (query.get("view") or [None])[0]
    viewing = viewing if viewing in names and viewing != marker else None
    compare_name = (query.get("compare") or [None])[0]
    compare_name = compare_name if compare_name in names and compare_name != marker else None
    design = cv_store.read_design()
    problem = None
    shown = None
    editor = ""
    master_text = laid_text = None
    try:
        master_text, _source = score_item.load_cv_text(config)
        doc = cv_doc.load_doc(master_text)
        laid_text = cv_doc.render(doc)
        shown = laid_text
        if viewing:
            shown = cv_doc.render(cv_doc.load_doc(
                (cv_store.PROFILE_DIR / viewing).read_text(encoding="utf-8")))
        editor = cv_page.editor_html(shown, {
            "mode": "master", "editable": not viewing, "version": marker,
            "sheet": True, "design": design})
    except (SystemExit, OSError, cv_doc.CvError) as err:
        problem = str(err)
    conn = _ro(db_path)
    try:
        stale = affected_summary(conn, config, "stale") if master_text is not None else None
        open_scope = affected_summary(conn, config, "open")
        counts = version_counts(conn)
    finally:
        if conn is not None:
            conn.close()
    compare = None
    if compare_name and laid_text is not None:
        try:
            old = (cv_store.PROFILE_DIR / compare_name).read_text(encoding="utf-8")
            c = line_changes(_laid(old), cv_store.cv_lines(laid_text))
            row = next(r for r in hist if r["name"] == compare_name)
            compare = {"name": compare_name, "number": row["number"], "when": row["when"],
                       "words": changes_words(c), "items": c["items"]}
        except OSError:
            compare = None
    prefs = pref_values(config)
    pages = (cv_doc.estimate_pages(cv_store.cv_lines(laid_text), design)
             if laid_text is not None else [])
    active_row = next((r for r in hist if r["active"]), None)
    active_number = active_row["number"] if active_row else 0
    page_count = len(pages)
    run = rescore_status()
    laid_out = (master_text is not None
                and laid_text == "\n".join(cv_store.cv_lines(master_text)) + "\n")
    data = {
        "laidOut": laid_out or problem is not None,
        "active": marker, "activeNumber": active_number, "viewing": viewing,
        "viewText": shown if viewing and problem is None else None,
        "stale": stale, "open": open_scope, "run": run,
        "design": design, "savedAt": short_when(marker) if marker else "",
        "prefs": {k: prefs.get(k) for k in radar_common.SCORING_PREFS},
        "live": prefs["live"],
    }
    banners = []
    if problem is not None:
        banners.append(("bad", "The master CV could not be read. " + problem))
    if viewing:
        vrow = next(r for r in hist if r["name"] == viewing)
        banners.append(("info", f"Viewing version {vrow['number']}, saved {vrow['when']}, read only."))
    banner_html = "".join(
        f'<div class="notice notice-{k}" role="note"><div class="notice-body">'
        f'<span class="notice-mark">{_icon({"bad": "bad", "info": "info"}[k])}</span>'
        f'<p class="notice-text">{esc(t)}</p></div>'
        + ('<div class="notice-acts"><a class="btn btn-secondary" href="/cv">Back to version '
           f'{active_number}</a><button type="button" class="btn btn-ghost" id="view-restore">'
           'Start a new version from this one</button></div>' if k == "info" else "")
        + "</div>" for k, t in banners)
    # One caution line, only while the master is not in the editor's layout.
    if problem is None and not laid_out:
        banner_html += ('<p class="cv-caution" role="note">Not saved in Radar\'s layout yet. Prepare '
                        'application can\'t make a copy until it is.</p>')
    # A master not yet in the editor's layout always has something to save.
    needs_layout = problem is None and not laid_out and not viewing
    pages_pill = (f'<span class="pill pages-pill{" is-over" if page_count > 2 else ""}" id="pages-pill">'
                  f'{esc(plural(page_count, "page"))}, limit is 2</span>' if page_count else
                  '<span class="pill pages-pill" id="pages-pill" hidden></span>')
    head = (f'<header class="cv-head">{page_frame.chip("cv", "cv")}<h1>CV</h1>'
            '<span class="pill pill-ok">The scorer reads this version</span>'
            f'{pages_pill}'
            '<button type="button" class="btn btn-rose" id="tb-save" data-cv-act="master" '
            'aria-keyshortcuts="Control+S"'
            f'{"" if needs_layout else " hidden"}>Save version</button></header>')

    def rail_sec(key: str, title: str, body: str, count: str = "") -> str:
        return (f'<section class="cv-sec" id="{key}-sec" aria-labelledby="{key}-h"><header class="sec-head">'
                f'{page_frame.chip("cv" if key == "versions" else "list", "cv")}'
                f'<h2 class="sec-title" id="{key}-h">{esc(title)}</h2>{count}</header>'
                f'<div class="card g-cv glow-faint cv-card">{body}</div></section>')
    rail = (f'<aside class="cv-rail" aria-label="Versions and import">'
            + rail_sec("versions", "Versions",
                       f'<div id="history">{_history_html(hist, viewing, compare, counts)}</div>',
                       f'<span class="sec-count">{len(hist)}</span>')
            + rail_sec("import", "Import", f'<div id="import">{_import_html(active_number)}</div>')
            + '</aside>')
    work = f"""<div class="cv-main"><div class="canvas">
<h2 class="visually-hidden" id="sheet-h">The CV sheet</h2>
{_toolbar_html(problem is None and not viewing)}
<p class="cv-note" id="cv-say" aria-live="polite"></p>
<article class="sheet sheet-paper" id="cv-sheet" aria-label="The CV, {esc(plural(page_count, 'page'))} of A4">{editor or '<p class="cvl-t">No CV to show.</p>'}</article>
</div></div>"""
    add_menu = ('<ul class="menu" role="menu" id="add-menu" aria-label="Add section" hidden>'
                '<li class="mhead" role="presentation" id="add-after">Add after the section in focus</li>'
                + "".join(f'<li role="none"><button type="button" role="menuitem" tabindex="-1" '
                          f'data-add-section="{esc(t)}">{esc(t)}</button></li>' for t in ADD_CHOICES)
                + '<li role="separator"></li><li role="none"><button type="button" role="menuitem" '
                  'tabindex="-1" data-add-section="">A section with your own heading</button></li></ul>')
    dialogs = f"""<dialog class="dialog" id="rearrange-dlg" aria-labelledby="rearrange-h" aria-describedby="rearrange-help">
<h3 id="rearrange-h">Rearrange sections</h3>
<p class="dlg-body" id="rearrange-help">Pick a section up with Space, move it with the Up and Down arrows and put it down with Space. Escape puts it back where it was. Move up and Move down do the same with a click.</p>
<ul class="rlist" id="rearrange-list"></ul>
<p class="live" id="rearrange-live" aria-live="assertive"></p>
<div class="dlg-acts"><button type="button" class="btn btn-primary" id="rearrange-done">Done</button><button type="button" class="btn btn-ghost" id="rearrange-cancel">Cancel</button></div>
</dialog>
<dialog class="dialog" id="design-dlg" aria-labelledby="design-h">
<h3 id="design-h">Design</h3>
<div class="opts" id="design-opts"></div>
<p class="live" id="design-pages" aria-live="polite"></p>
<div class="dlg-acts"><button type="button" class="btn btn-primary" id="design-apply">Apply</button><button type="button" class="btn btn-ghost" id="design-standard">Back to standard</button><button type="button" class="btn btn-ghost" id="design-cancel">Cancel</button></div>
</dialog>"""
    page = f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CV, MedTech Radar</title>
<link rel="icon" href="{build_dashboard.FAVICON}">
<style>{page_frame.FRAME_CSS}{page_frame.GROUPS_CSS}{page_frame.TITLES_CSS}{cv_page.CV_CSS}{CV_SCREEN_CSS}</style>
</head>
<body class="cvp-body">
{page_frame.appbar_html("cv", jobs)}
<main class="cvp" id="main" data-page="{esc(json.dumps(data, ensure_ascii=False))}">
{head}
{banner_html}
{_rescore_blocks(stale, marker, run)}
<div class="cv-layout">
{work}
{rail}
</div>
{add_menu}
{dialogs}
<p class="visually-hidden" id="cv-live" aria-live="assertive"></p>
</main>
<script>{build_dashboard.SCRIPT}{page_frame.FRAME_SCRIPT}{cv_page.CV_MODEL_JS}{cv_page.CV_EDITOR_JS}{CV_SCREEN_JS}</script>
</body>
</html>"""
    return page.encode("utf-8")


CV_SCREEN_CSS = r"""
:root{--dur-fast:120ms;--ease:cubic-bezier(.2,0,.1,1);--radius-sm:2px;--space-4:4px;--space-6:6px;--space-8:8px;--space-10:10px;--space-12:12px;--space-16:16px;--text-2xs:11px;--text-xs:12px;--text-sm:13px;--text-lg:20px;--tracking-label:.04em;--weight-strong:600}
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--canvas);scroll-padding-top:72px}
body.cvp-body{margin:0;background:var(--canvas);color:var(--text-1);font:400 15px/24px var(--font-sans);-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
.cvp a{color:var(--accent)}
.cvp a:hover{color:var(--accent-hover)}
.cvp h1,.cvp h2,.cvp h3,.cvp h4,.cvp p,.cvp ol,.cvp ul,.cvp dl,.cvp dd{margin:0}
.cvp ol,.cvp ul{padding:0;list-style:none}
.cvp button,.cvp input,.cvp select,.cvp textarea{font:inherit}
/* The shared content box, page_frame's --frame-max and --frame-pad. */
.cvp{display:block;padding:24px var(--frame-pad) 48px;max-width:var(--frame-max);margin:0 auto}
/* The title row (owner, 18 Nov): the CV's rose chip, CV, the pills, and the
   page's one solid button at the right. */
.cv-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin:0 0 16px}
.cv-head h1{font:600 24px/32px var(--font-sans)}
.cv-head #tb-save{margin-left:auto}
.pill{display:inline-flex;align-items:center;height:24px;padding:0 10px;border:1px solid #2a3238;border-radius:12px;background:#1a2025;font-size:13px;line-height:20px;font-weight:500;color:var(--text-2);white-space:nowrap}
.pill-ok{border-color:rgba(116,211,155,.45);background:var(--positive-tint);color:var(--positive)}
.pill.is-over{border-color:rgba(240,196,106,.5);background:var(--caution-tint);color:var(--caution)}
.btn-rose,.cvp a.btn-rose{background:#e88fb4;border-color:#e88fb4;color:#2a0f1c;font-weight:600}
.btn-rose:hover{background:#efa9c6;border-color:#efa9c6;color:#2a0f1c}
.cv-caution{margin:0 0 16px;padding:10px 16px;border:1px solid var(--caution);border-radius:10px;background:var(--caution-tint);color:var(--text-1);font-size:14px;line-height:20px}
.card{min-width:0;border:1px solid var(--border-subtle);border-radius:14px;background-color:var(--surface)}
.mono{font-family:var(--font-mono);font-size:13px}
.muted{color:var(--text-3)}
.small{font-size:12px;line-height:16px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:32px;padding:0 12px;border:1px solid transparent;border-radius:var(--radius-2);font:500 14px/20px var(--font-sans);white-space:nowrap;text-decoration:none;cursor:pointer;background:transparent;color:var(--text-2)}
.cvp a.btn{color:var(--text-2)}
.btn-primary,.cvp a.btn-primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.btn-primary:hover{background:var(--accent-hover);border-color:var(--accent-hover);color:var(--on-accent)}
.btn-secondary,.cvp a.btn-secondary{background:var(--surface);border-color:var(--border-strong);color:var(--text-1)}
.btn-secondary:hover{background:var(--row-hover);color:var(--text-1)}
.btn-ghost:hover,.cvp a.btn-ghost:hover{color:var(--text-1);background:var(--row-hover)}
.btn[aria-disabled="true"],.btn:disabled{color:var(--text-3);border-color:var(--border-subtle);cursor:default;background:transparent}
.btn svg{flex:none}
.kbd{display:inline-flex;align-items:center;justify-content:center;min-width:18px;height:18px;padding:0 4px;border:1px solid var(--border-strong);border-radius:var(--radius-2);font:500 11px/16px var(--font-mono);color:var(--text-2);background:var(--canvas)}
.btn-primary .kbd{border-color:var(--on-accent);color:var(--on-accent);background:transparent}
.tag{display:inline-flex;align-items:center;gap:4px;min-height:20px;padding:0 8px;border-radius:var(--radius-2);font:500 12px/16px var(--font-sans);white-space:nowrap}
.tag-ok{background:var(--positive-tint);color:var(--positive)}
.tag-accent{background:var(--accent-tint);color:var(--accent)}
.tag-caution{background:var(--caution-tint);color:var(--caution)}
.link{color:var(--accent);text-underline-offset:2px}
.notice{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;padding:10px 16px;border:1px solid var(--border-subtle);border-radius:10px;background:var(--surface);margin:0 0 16px}
.notice-caution{border-color:var(--caution)}
.notice-info{border-color:var(--neutral)}
.notice-ok{border-color:var(--positive)}
.notice-bad{border-color:var(--negative)}
.notice-body{display:flex;gap:12px;align-items:flex-start;flex:1 1 40ch;min-width:0;font-size:14px;line-height:20px}
.notice-mark{flex:none;margin-top:2px;display:inline-flex}
.notice-caution .notice-mark{color:var(--caution)}
.notice-info .notice-mark{color:var(--neutral)}
.notice-ok .notice-mark{color:var(--positive)}
.notice-bad .notice-mark{color:var(--negative)}
.notice-text strong{font-weight:600;color:var(--text-1)}
.notice-text p+p{margin-top:4px}
.notice-acts{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.notice:focus{outline:none}
.notice:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
/* The sheet and a rail of Versions and Import: 360px from 1024px, 400px from
   1800px, stacked below (owner, 18 Nov). */
.cv-layout{display:grid;gap:32px;align-items:start;grid-template-columns:minmax(0,1fr)}
@media (min-width:1024px){.cv-layout{grid-template-columns:minmax(0,1fr) 360px}}
@media (min-width:1800px){.cv-layout{grid-template-columns:minmax(0,1fr) 400px}}
.cv-main,.cv-rail{min-width:0}
.cv-sec + .cv-sec{margin-top:24px}
.cv-card{padding:16px}
.over-go{display:inline-flex;align-items:center;padding:0 6px;font-size:14px;line-height:20px}
.cvp .panel{background:var(--surface);border:1px solid var(--border-subtle);border-radius:var(--radius-4);padding:16px;margin:0}
.cvp .panel h2{font:600 16px/24px var(--font-sans)}
.canvas{display:flex;flex-direction:column;align-items:center;min-width:0}
.canvas > .toolbar,.canvas > .cv-note,.canvas > .over-label{width:794px;max-width:100%}
.wide-only{display:inline-flex;align-items:center;gap:2px}
@media (max-width:767.98px){.toolbar .wide-only{display:none}}
.toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:2px;padding:6px;background:var(--surface);border:1px solid var(--border-strong);border-radius:var(--radius-4);margin-bottom:12px;position:sticky;top:0;z-index:5;box-shadow:0 4px 12px rgba(0,0,0,.35)}
.toolbar .sep{width:1px;align-self:stretch;margin:4px 2px;background:var(--border-subtle)}
.toolbar .grow{flex:1 1 auto}
.toolbar .btn{padding:0 8px;gap:6px}
.toolbar .btn-icon{width:32px;padding:0}
.save-state{display:inline-flex;align-items:center;gap:6px;font-size:13px;line-height:20px;color:var(--text-2);padding:0 6px;white-space:nowrap}
.save-state.is-ok svg{color:var(--positive)}
.save-state.is-warn{color:var(--caution)}
.save-state.is-bad{color:var(--negative)}
.switch{display:inline-flex;align-items:center;gap:8px;min-height:32px;padding:0 8px;border-radius:var(--radius-2);border:1px solid transparent;background:transparent;color:var(--text-1);font:500 14px/20px var(--font-sans);cursor:pointer;white-space:nowrap}
.switch .track{width:28px;height:16px;border-radius:8px;border:1px solid var(--border-strong);background:var(--canvas);position:relative;flex:none}
.switch .track::after{content:"";position:absolute;top:2px;left:2px;width:10px;height:10px;border-radius:50%;background:var(--text-3)}
.switch[aria-checked="true"] .track{background:var(--accent-tint);border-color:var(--accent)}
.switch[aria-checked="true"] .track::after{left:14px;background:var(--accent)}
.switch .sw-word{font-size:12px;color:var(--text-2)}
.cv-note{font-size:13px;line-height:20px;color:var(--text-2);min-height:0;margin:0 0 8px}
.cv-note:empty{display:none}
.cv-note.bad{color:var(--negative)}
/* the sheet, light paper (page_frame .sheet-paper), A4 in proportion */
.sheet{position:relative;width:794px;max-width:100%;min-height:1123px;padding:var(--sheet-pad-top,68px) var(--sheet-pad-side,76px) 64px;border-radius:var(--radius-2);font:400 calc(14px * var(--cv-scale,1))/1.43 var(--cv-font,Arial),"Helvetica Neue",Helvetica,sans-serif}
.sheet .cv-lines{margin:0}
.sheet .cvl{display:grid;grid-template-columns:1em minmax(0,1fr) auto;gap:0 4px;position:relative;padding:0;border-radius:0}
.sheet .cvl-n{position:absolute;left:-56px;width:44px;text-align:right;font:400 11px/20px var(--font-mono);color:var(--sheet-ink-3)}
.sheet.nums-off .cvl-n{display:none}
.sheet .cvl-mark{grid-column:1;font:inherit;color:var(--sheet-ink-2)}
.sheet .cvl-t{grid-column:2;font:inherit;line-height:inherit;color:var(--sheet-ink-1)}
.sheet .cvl-ops{grid-column:3}
.sheet .cvl-plain .cvl-mark,.sheet .cvl-h1 .cvl-mark,.sheet .cvl-h2 .cvl-mark,.sheet .cvl-h3 .cvl-mark{display:none}
.sheet .cvl-plain .cvl-t,.sheet .cvl-h1 .cvl-t,.sheet .cvl-h2 .cvl-t,.sheet .cvl-h3 .cvl-t{grid-column:1 / 3}
.sheet .cvl-h1 .cvl-t{font:700 calc(24px * var(--cv-scale,1))/1.33 var(--cv-font,Arial),sans-serif;letter-spacing:0;text-transform:none}
.sheet .cvl-h1 .cvl-n{line-height:32px}
.sheet .cvl-h2{margin-top:calc(10px * var(--cv-gap,1))}
.sheet .cvl-h2 .cvl-t{font:700 calc(17px * var(--cv-scale,1))/1.4 var(--cv-font,Arial),sans-serif;letter-spacing:0;text-transform:none;color:var(--sheet-ink-1)}
.sheet .cvl-h3{margin-top:calc(6px * var(--cv-gap,1))}
.sheet .cvl-h3 .cvl-t{font-weight:700}
.sheet .cvl-blank{min-height:0;height:calc(4px * var(--cv-gap,1))}
.sheet .cvl-blank .cvl-n{display:none}
.sheet .cvl-head .cvl-t,.sheet .cv-head .cvl-plain .cvl-t{color:var(--sheet-ink-2)}
.sheet textarea.cvl-t{border:1px solid var(--sheet-rule-strong);background:var(--sheet-sunken);color:var(--sheet-ink-1);padding:0 5px;margin:1px -6px;min-height:0}
.sheet textarea.cvl-t:focus{outline:2px solid var(--sheet-focus);outline-offset:1px;border-color:var(--sheet-rule-strong);background:var(--sheet-surface)}
.cv-part{position:relative;border-radius:2px}
.cv-part[tabindex]{cursor:text}
.cv-part[tabindex]:hover{outline:1px dashed var(--sheet-rule-strong);outline-offset:6px}
.cv-part:focus{outline:none}
.cv-part:focus-visible{outline:2px solid var(--sheet-focus);outline-offset:6px;box-shadow:none}
.cv-part.is-editing{outline:2px solid var(--sheet-accent);outline-offset:8px;cursor:auto}
.cv-part-edit{position:absolute;right:-8px;top:2px;z-index:2;font-size:13px;min-height:28px;padding:0 10px}
@media (min-width:768px){.cv-part:not(.is-editing) .cv-part-edit{display:none}}
.cv-part.is-editing .cv-part-edit{top:-40px;right:-10px}
.sheet .cv-add{margin:2px 0 6px 1em}
.sheet .cvl-ops button,.sheet .cv-add button{min-height:24px;color:var(--sheet-ink-2);background:var(--sheet-surface);border-color:var(--sheet-rule-strong)}
@media (pointer:coarse){.sheet .cvl-ops button,.sheet .cv-add button,.cv-part-edit{min-height:44px;min-width:44px}}
.cv-break{display:flex;align-items:center;gap:12px;margin:24px calc(-1 * var(--sheet-pad-side,76px)) 20px;padding:6px var(--sheet-pad-side,76px);background:var(--canvas);border-top:1px solid var(--border-subtle);border-bottom:1px solid var(--border-subtle);color:var(--text-3);font:400 12px/16px var(--font-sans)}
.cv-break:focus{outline:2px solid var(--focus);outline-offset:-2px}
.cv-break.is-limit{color:var(--caution);font-weight:600}
.cv-break.is-limit::before,.cv-break.is-limit::after{content:"";flex:1;border-top:2px dashed var(--caution)}
.sheet .cvl.cv-over::before{content:"";position:absolute;left:-62px;top:0;bottom:0;width:3px;background:var(--sheet-caution)}
@media (max-width:767.98px){.sheet .cvl.cv-over::before{left:-38px}}
.sheet-foot{display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px 12px;width:794px;max-width:100%;margin:8px 0 24px;color:var(--text-3);font-size:12px;line-height:16px}
.over-label{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 12px;color:var(--caution);font:600 14px/20px var(--font-sans)}
@media (min-width:768px) and (max-width:841.98px){.sheet{zoom:.9}}
@media (max-width:767.98px){
  .cvp{padding:16px var(--frame-pad) 40px}
  .canvas{align-items:stretch}
  .canvas > .toolbar{width:100%}
  .sheet{width:100%;min-height:0;padding:20px 14px 20px 40px;--sheet-pad-side:14px}
  .sheet .cvl-n{left:-36px;width:28px;font-size:10px}
  .sheet .cvl-h2 .cvl-t{padding-right:72px}
  .sheet-foot{width:100%}
  .toolbar .btn-icon{width:44px;min-height:44px}
  .cv-break{margin-left:-40px;margin-right:-14px;padding-left:40px}
  .ph-row .ph-meta{margin-left:0}
}
.masters h2{display:flex;align-items:baseline;justify-content:space-between;gap:8px;flex-wrap:wrap}
.tabpanel{padding:16px}
.tabpanel:focus{outline:none}
.tabpanel:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
.tabpanel h3{font:600 15px/24px var(--font-sans)}
.tabpanel h4{font:600 14px/20px var(--font-sans)}
.tabpanel .tp-note{color:var(--text-2);font-size:14px;line-height:20px;margin:2px 0 12px}
.vlist{display:flex;flex-direction:column;border-top:1px solid var(--border-subtle)}
.vrow{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 12px;padding:10px 0;border-bottom:1px solid var(--border-subtle)}
.vrow .vwhen{font:500 14px/20px var(--font-sans);color:var(--text-1)}
.vrow .vwhat{color:var(--text-2);font-size:13px;line-height:20px}
.vrow .vfile{--dv-space:8px;color:var(--text-3);font:400 12px/16px var(--font-mono);overflow-wrap:anywhere}
.vrow .vacts{display:flex;gap:4px;align-items:flex-start;flex-wrap:wrap;justify-content:flex-end}
.vrow.is-viewing{background:var(--accent-tint);outline:1px solid var(--accent);border-radius:8px;padding-left:10px;padding-right:10px}
@media (max-width:767.98px){.vrow{grid-template-columns:minmax(0,1fr)}.vrow .vacts{justify-content:flex-start}}
.compare{margin:0 0 12px}
.field{display:flex;flex-direction:column;gap:4px;padding:12px 0;border:0;border-bottom:1px solid var(--border-subtle);margin:0;min-width:0}
.field:last-of-type{border-bottom:0}
.field > label,.field > legend{font:600 14px/20px var(--font-sans);color:var(--text-1);padding:0}
.field .help,.help{color:var(--text-2);font-size:13px;line-height:20px}
.field .gate{color:var(--text-3);font-size:12px;line-height:16px}
.inrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.input{min-height:32px;padding:0 10px;border:1px solid var(--border-strong);border-radius:var(--radius-2);background:var(--canvas);color:var(--text-1);font:400 14px/20px var(--font-sans);min-width:0}
.input.num{width:120px}
.input.short{width:64px}
.input.wide{width:100%}
textarea.input{padding:6px 10px;resize:vertical}
.input.changed{border-color:var(--caution);box-shadow:inset 3px 0 0 var(--caution)}
.input[aria-invalid="true"]{border-color:var(--negative);box-shadow:inset 3px 0 0 var(--negative)}
.affix{color:var(--text-2);font-size:14px}
.check{display:inline-flex;align-items:center;gap:8px;font-size:14px;line-height:20px;color:var(--text-1);min-height:32px;cursor:pointer}
.check input{width:16px;height:16px;margin:0;accent-color:var(--accent)}
.form-foot{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;margin-top:12px;padding-top:12px;border-top:1px solid var(--border-subtle)}
.say{font-size:13px;line-height:20px;color:var(--text-2);margin-top:8px}
.say:empty{display:none}
.say.bad{color:var(--negative)}
.offer{margin-top:12px}
.drop{display:flex;flex-direction:column;align-items:flex-start;gap:8px;padding:16px;border:1px dashed var(--border-strong);border-radius:var(--radius-2);background:var(--canvas)}
.drop:focus-within{outline:2px solid var(--focus);outline-offset:2px}
.preview{max-height:320px;overflow:auto;margin:8px 0 0;padding:10px 12px;border:1px solid var(--border-subtle);border-radius:var(--radius-2);background:var(--canvas);font:400 12px/18px var(--font-mono);color:var(--text-2);white-space:pre-wrap;overflow-wrap:anywhere}
.diffs{display:flex;flex-direction:column;gap:6px;margin:8px 0}
.diffs li{display:grid;grid-template-columns:auto minmax(0,1fr);gap:8px;font-size:13px;line-height:20px;color:var(--text-2)}
.diffs .dk{font:600 12px/20px var(--font-sans);color:var(--text-1);white-space:nowrap}
.bar{height:8px;border-radius:4px;background:var(--canvas);border:1px solid var(--border-strong);overflow:hidden;margin:8px 0 4px;max-width:420px}
.bar span{display:block;height:100%;background:var(--accent)}
.dialog{background:var(--surface);color:var(--text-1);border:1px solid var(--border-strong);border-radius:var(--radius-4);box-shadow:var(--shadow-overlay);padding:16px;max-width:min(560px,calc(100vw - 32px))}
dialog.dialog::backdrop{background:rgba(0,0,0,.6)}
.dialog h3,.dialog h4{font:600 16px/24px var(--font-sans)}
.dialog .dlg-body{color:var(--text-2);font-size:14px;line-height:20px;margin-top:6px}
.dialog .dlg-acts{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap}
.menu{position:absolute;z-index:30;background:var(--surface);border:1px solid var(--border-strong);border-radius:var(--radius-4);box-shadow:var(--shadow-overlay);padding:4px;width:300px;max-width:calc(100vw - 32px)}
.menu [role="menuitem"]{display:flex;width:100%;min-height:36px;align-items:center;padding:0 10px;border:0;background:transparent;color:var(--text-1);font:400 14px/20px var(--font-sans);border-radius:2px;text-align:left;cursor:pointer}
.menu [role="menuitem"]:focus{background:var(--row-hover);outline:2px solid var(--focus);outline-offset:-2px}
@media (pointer:coarse){.menu [role="menuitem"]{min-height:44px}}
.menu .mhead{padding:6px 10px 2px;color:var(--text-3);font-size:12px;line-height:16px}
.menu [role="separator"]{border-top:1px solid var(--border-subtle);margin:4px 0}
.rlist{display:flex;flex-direction:column;gap:6px;margin-top:12px}
.rrow{display:grid;grid-template-columns:20px minmax(0,1fr) auto;gap:10px;align-items:center;padding:8px 10px;border:1px solid var(--border-subtle);border-radius:var(--radius-2);background:var(--canvas)}
.rrow:focus{outline:2px solid var(--focus);outline-offset:2px}
.rrow .rname{font:500 14px/20px var(--font-sans)}
.rrow .rmeta{font-size:12px;line-height:16px;color:var(--text-3)}
.rrow .racts{display:flex;gap:4px;flex-wrap:wrap}
.rrow.is-picked{border-color:var(--accent);background:var(--accent-tint);margin-left:8px}
.rrow .grip{color:var(--text-3)}
.live{margin-top:10px;padding:8px 10px;border-radius:var(--radius-2);background:var(--neutral-tint);color:var(--text-1);font-size:13px;line-height:20px}
.live:empty{display:none}
.opts{display:grid;grid-template-columns:110px minmax(0,1fr);gap:10px 12px;align-items:start;margin-top:12px}
.opts > .ol{font:600 14px/32px var(--font-sans)}
@media (max-width:520px){.opts{grid-template-columns:minmax(0,1fr)}.opts > .ol{line-height:20px}}
.seg{display:inline-flex;border:1px solid var(--border-strong);border-radius:var(--radius-2);overflow:hidden;flex-wrap:wrap}
.seg button{min-height:32px;padding:0 12px;border:0;border-right:1px solid var(--border-subtle);background:var(--canvas);color:var(--text-2);font:500 13px/20px var(--font-sans);cursor:pointer}
.seg button:last-child{border-right:0}
.seg button:focus-visible{outline-offset:-3px}
.seg button[aria-checked="true"]{background:var(--accent-tint);color:var(--accent);font-weight:600}
.seg button[aria-checked="true"]::before{content:"\2713\00a0"}
@media (pointer:coarse){.btn,.seg button,.switch,.tab{min-height:44px}}
@media (prefers-reduced-motion:reduce){:root{--dur:0ms}}
@media (max-width:767.98px){.cvp .reads{display:none}.sheet .cv-part[tabindex]{padding-right:60px}.sheet .cv-part.is-editing{padding-right:0}}
"""

# The page's script: the toolbar, the dialogs, the tabs, the keys, the
# Preferences tab, Rescore affected and the Word import. The editor itself is
# cv_page.CV_EDITOR_JS, which this works through window.RadarCvEditor.
CV_SCREEN_JS = r"""
(function () {
  var main = document.getElementById('main');
  if (!main) return;
  var D;
  try { D = JSON.parse(main.getAttribute('data-page') || ''); } catch (err) { return; }
  var E = window.RadarCvEditor || null;
  function $(id) { return document.getElementById(id); }
  function all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function typing(t) {
    return !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || ''));
  }
  function store(key, value) {
    try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); }
    catch (e) { return null; }
    return null;
  }
  function shown(el) {
    if (!el || el.hidden) return false;
    if (el.closest && el.closest('[hidden]')) return false;
    if (typeof el.offsetParent !== 'undefined' && el.offsetParent === null
        && typeof window.getComputedStyle === 'function'
        && window.getComputedStyle(el).position !== 'fixed') return false;
    return true;
  }
  function post(url, body, headers) {
    return fetch(url, { method: 'POST', body: body,
                        headers: headers || { 'Content-Type': 'application/json' } })
      .then(function (r) {
        return r.json().then(function (j) { return { status: r.status, j: j || {} }; },
                             function () { return { status: r.status, j: { ok: false, note: 'The server did not answer in a way the page understands.' } }; });
      });
  }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : (many || one + 's')); }
  function announce(text) {
    var live = $('cv-live');
    if (!live) return;
    live.textContent = '';
    live.textContent = text;
  }
  var ICON = {
    tick: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false"><circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="M5.2 8.2 7 10l3.8-4" stroke="currentColor" stroke-width="1.5" fill="none"/></svg>',
    dot: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false"><circle cx="8" cy="8" r="3.2" fill="currentColor"/></svg>',
    bad: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false"><circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="M5.8 5.8l4.4 4.4m0-4.4-4.4 4.4" stroke="currentColor" stroke-width="1.5"/></svg>',
    warn: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false"><path d="M8 2.2 14.2 13H1.8z" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="M8 6.4v3.2" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="11.4" r=".8" fill="currentColor"/></svg>',
    spin: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false"><path d="M8 2a6 6 0 1 1-6 6" stroke="currentColor" stroke-width="1.5" fill="none"/></svg>'
  };

  // ------------------------------------------------------- the sheet
  var sheetEl = $('cv-sheet');
  var SPACE = { compact: 0.5, standard: 1, relaxed: 1.5 };
  var design = D.design || { font: 'Arial', size: 10.5, spacing: 'standard', margins: 20 };
  function applyDesign(d) {
    design = d;
    if (!sheetEl || !sheetEl.style || !sheetEl.style.setProperty) return;
    sheetEl.style.setProperty('--cv-font', d.font);
    sheetEl.style.setProperty('--cv-scale', String(d.size / 10.5));
    sheetEl.style.setProperty('--cv-gap', String(SPACE[d.spacing] || 1));
    sheetEl.style.setProperty('--sheet-pad-side', Math.round(d.margins * 3.78) + 'px');
    sheetEl.style.setProperty('--sheet-pad-top', Math.round((d.margins - 2) * 3.78) + 'px');
  }
  applyDesign(design);
  var numbersBtn = $('tb-numbers');
  function setNumbers(on, keep) {
    if (sheetEl) sheetEl.classList.toggle('nums-off', !on);
    if (numbersBtn) numbersBtn.setAttribute('aria-checked', String(on));
    var sw = $('design-numbers');
    if (sw) sw.setAttribute('aria-checked', String(on));
    if (keep) store('radar-cv-numbers', on ? 'on' : 'off');
  }
  setNumbers(store('radar-cv-numbers') !== 'off', false);
  if (numbersBtn) numbersBtn.addEventListener('click', function () {
    setNumbers(numbersBtn.getAttribute('aria-checked') !== 'true', true);
  });

  // ------------------------------------------- the toolbar, one Tab stop
  var toolbar = $('cv-toolbar');
  function tbItems() {
    return toolbar ? all('button', toolbar).filter(shown) : [];
  }
  function rove(items, current) {
    items.forEach(function (b) { b.setAttribute('tabindex', b === current ? '0' : '-1'); });
  }
  if (toolbar) {
    var first = tbItems()[0];
    rove(tbItems(), first);
    toolbar.addEventListener('focusin', function (e) {
      if (e.target && e.target.tagName === 'BUTTON') rove(tbItems(), e.target);
    });
    toolbar.addEventListener('keydown', function (e) {
      var items = tbItems();
      var i = items.indexOf(document.activeElement);
      if (i < 0) return;
      var j = null;
      if (e.key === 'ArrowRight') j = (i + 1) % items.length;
      else if (e.key === 'ArrowLeft') j = (i - 1 + items.length) % items.length;
      else if (e.key === 'Home') j = 0;
      else if (e.key === 'End') j = items.length - 1;
      if (j === null) return;
      e.preventDefault();
      rove(items, items[j]);
      items[j].focus();
    });
  }
  function enabled(b) { return b && !b.disabled && b.getAttribute('aria-disabled') !== 'true'; }

  // -------------------------------------------------------- save state
  var stateEl = $('save-state');
  var saveBtn = $('tb-save');
  var discardBtn = $('tb-discard');
  var reloadBtn = $('tb-reload');
  var undoBtn = $('tb-undo');
  var redoBtn = $('tb-redo');
  var failed = false;
  var lastState = null;
  function setState(kind, html) {
    if (!stateEl) return;
    stateEl.className = 'save-state is-' + kind;
    stateEl.innerHTML = html;
  }
  function needsLayout(st) { return !D.laidOut && !D.viewing && st.editable !== false; }
  function refresh(st) {
    lastState = st;
    if (undoBtn) undoBtn.setAttribute('aria-disabled', String(!st.canUndo));
    if (redoBtn) redoBtn.setAttribute('aria-disabled', String(!st.canRedo));
    if (!failed) {
      if (st.edited) {
        setState('warn', ICON.dot + '<span>' + (st.unsaved ? plural(st.unsaved, 'unsaved line')
          : 'Unsaved changes') + '</span>');
      } else if (D.viewing) {
        setState('ok', '<span>Read only</span>');
      } else if (needsLayout(st)) {
        setState('warn', ICON.dot + '<span>Not in Radar\'s layout</span>');
      } else {
        setState('ok', ICON.tick + '<span>' + (D.savedAt ? 'Saved ' + D.savedAt : 'Nothing to save') + '</span>');
      }
    }
    if (saveBtn) saveBtn.hidden = !(st.edited || needsLayout(st)) || failed;
    if (discardBtn) discardBtn.hidden = !st.edited || failed;
    if (toolbar) rove(tbItems(), tbItems().indexOf(document.activeElement) >= 0
      ? document.activeElement : tbItems()[0]);
    var pages = st.pages || [];
    var foot = $('page-foot');
    if (foot && pages.length) {
      foot.textContent = plural(pages.length, 'page') + ' of A4, lines 1 to ' + pages[pages.length - 1].last;
    }
    if (sheetEl) sheetEl.setAttribute('aria-label', 'The CV, ' + plural(pages.length, 'page') + ' of A4');
    var over = $('over-note');
    if (over) {
      if (pages.length > 2) {
        over.hidden = false;
        over.innerHTML = '<a class="link" href="#page-3" id="go-page-3">Go to page 3</a>';
      } else {
        over.hidden = true;
        over.textContent = '';
      }
    }
    var pill = $('pages-pill');
    if (pill && pages.length) {
      pill.hidden = false;
      pill.textContent = plural(pages.length, 'page') + ', limit is 2';
      pill.classList.toggle('is-over', pages.length > 2);
    }
  }
  if (E) {
    E.onchange = refresh;
    E.onfail = function (j) {
      failed = true;
      setState('bad', ICON.bad + '<span>Not saved</span>');
      if (saveBtn) saveBtn.hidden = true;
      if (discardBtn) discardBtn.hidden = true;
      if (reloadBtn) reloadBtn.hidden = !/changed since this page was opened/.test(j.note || '');
      if (toolbar) rove(tbItems(), tbItems()[0]);
    };
    refresh(E.state());
  } else if (toolbar) {
    all('button', toolbar).forEach(function (b) { b.setAttribute('aria-disabled', 'true'); });
  }
  if (saveBtn) saveBtn.addEventListener('click', function () {
    failed = false;
    setState('ok', ICON.spin + '<span>Saving as a new version</span>');
  });
  if (discardBtn) discardBtn.addEventListener('click', function () {
    if (!E) return;
    var n = (lastState && lastState.unsaved) || 0;
    if (!window.confirm('Discard ' + plural(n || 1, 'unsaved line') + '? The CV goes back to version '
        + D.activeNumber + ' as it was saved.')) return;
    E.discard();
    announce('The unsaved changes were discarded.');
    var first = tbItems()[0];
    if (first) first.focus();
  });
  if (reloadBtn) reloadBtn.addEventListener('click', function () { window.location.reload(); });
  if (undoBtn) undoBtn.addEventListener('click', function () { if (E && enabled(undoBtn)) E.undo(); });
  if (redoBtn) redoBtn.addEventListener('click', function () { if (E && enabled(redoBtn)) E.redo(); });
  document.addEventListener('click', function (e) {
    var go = e.target && e.target.closest ? e.target.closest('#go-page-3') : null;
    if (!go) return;
    var page = $('page-3');
    if (!page) return;
    e.preventDefault();
    page.focus();
    if (page.scrollIntoView) page.scrollIntoView({ block: 'center' });
  });

  // ------------------------------------------------- Add section, a menu
  var addBtn = $('tb-add');
  var menu = $('add-menu');
  var lastSection = null;
  if (sheetEl) sheetEl.addEventListener('focusin', function () {
    if (E) { var f = E.focused(); if (f !== null) lastSection = f; }
  });
  function menuItems() { return menu ? all('[role="menuitem"]', menu) : []; }
  function closeMenu(back) {
    if (!menu || menu.hidden) return;
    menu.hidden = true;
    if (addBtn) addBtn.setAttribute('aria-expanded', 'false');
    if (back && addBtn) addBtn.focus();
  }
  function openMenu() {
    if (!menu || !E || !enabled(addBtn)) return;
    var secs = E.sections();
    var after = lastSection === null ? secs.length - 1 : lastSection;
    var head = $('add-after');
    if (head) head.textContent = after < 0 ? 'Add after the name and headline'
      : 'Add after ' + secs[after].title;
    menu.setAttribute('data-after', String(after));
    menu.hidden = false;
    addBtn.setAttribute('aria-expanded', 'true');
    if (addBtn.getBoundingClientRect && menu.style) {
      var r = addBtn.getBoundingClientRect();
      menu.style.left = Math.max(8, r.left + (window.scrollX || 0)) + 'px';
      menu.style.top = (r.bottom + (window.scrollY || 0) + 4) + 'px';
    }
    var items = menuItems();
    if (items[0]) items[0].focus();
  }
  if (addBtn) addBtn.addEventListener('click', function () {
    if (menu && !menu.hidden) closeMenu(true); else openMenu();
  });
  if (menu) {
    menu.addEventListener('keydown', function (e) {
      var items = menuItems();
      var i = items.indexOf(document.activeElement);
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        var j = e.key === 'ArrowDown' ? (i + 1) % items.length : (i - 1 + items.length) % items.length;
        items[j].focus();
      } else if (e.key === 'Home' || e.key === 'End') {
        e.preventDefault();
        items[e.key === 'Home' ? 0 : items.length - 1].focus();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        closeMenu(true);
      } else if (e.key === 'Tab') {
        closeMenu(false);
      }
    });
    menu.addEventListener('click', function (e) {
      var item = e.target.closest ? e.target.closest('[data-add-section]') : null;
      if (!item || !E) return;
      var after = Number(menu.getAttribute('data-after'));
      closeMenu(false);
      var title = item.getAttribute('data-add-section');
      var at = E.addSection(title, after);
      if (at !== null) {
        lastSection = at;
        announce((title || 'A new section') + ' added, open for editing.');
      }
    });
    document.addEventListener('click', function (e) {
      if (!menu.hidden && !menu.contains(e.target) && e.target !== addBtn
          && !(addBtn && addBtn.contains(e.target))) closeMenu(false);
    });
  }

  // ------------------------------------------------- dialogs
  function openDialog(dlg, opener) {
    if (!dlg) return;
    dlg._opener = opener || document.activeElement;
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
  }
  function closeDialog(dlg) {
    if (!dlg) return;
    if (dlg.close) dlg.close(); else { dlg.removeAttribute('open'); finished(dlg); }
  }
  function finished(dlg) {
    var back = dlg._opener;
    dlg._opener = null;
    if (back && back.focus) back.focus();
  }
  all('dialog.dialog').forEach(function (dlg) {
    dlg.addEventListener('close', function () { finished(dlg); });
  });
  function dialogOpen() {
    return all('dialog.dialog').some(function (d) { return d.open || d.hasAttribute('open'); });
  }

  // ---- Rearrange, a keyboard dialog
  var rDlg = $('rearrange-dlg');
  var rList = $('rearrange-list');
  var rLive = $('rearrange-live');
  var rBtn = $('tb-rearrange');
  var order = [];
  var picked = null;
  var pickedFrom = null;
  var secs = [];
  function rDraw(focusAt) {
    rList.textContent = '';
    order.forEach(function (oldIndex, pos) {
      var s = secs[oldIndex];
      var li = document.createElement('li');
      li.className = 'rrow' + (picked === pos ? ' is-picked' : '');
      li.setAttribute('tabindex', '0');
      li.setAttribute('data-pos', String(pos));
      var where = s.first ? 'lines ' + s.first + ' to ' + s.last : 'empty, not in the Word file';
      if (oldIndex !== pos) where += ', was position ' + (oldIndex + 1);
      li.setAttribute('aria-label', s.title + ', position ' + (pos + 1) + ' of ' + order.length
        + ', ' + where + (picked === pos ? ', picked up' : ''));
      var grip = document.createElement('span');
      grip.className = 'grip';
      grip.setAttribute('aria-hidden', 'true');
      grip.textContent = '⠿';
      li.appendChild(grip);
      var name = document.createElement('span');
      var t = document.createElement('span');
      t.className = 'rname';
      t.textContent = s.title;
      var m = document.createElement('span');
      m.className = 'rmeta';
      m.textContent = ' ' + where;
      name.appendChild(t);
      name.appendChild(m);
      li.appendChild(name);
      var acts = document.createElement('span');
      acts.className = 'racts';
      [['up', 'Move up'], ['down', 'Move down']].forEach(function (a) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn btn-ghost';
        b.setAttribute('data-move', a[0]);
        b.setAttribute('tabindex', '-1');
        b.setAttribute('aria-label', a[1] + ', ' + s.title);
        b.textContent = a[1];
        acts.appendChild(b);
      });
      li.appendChild(acts);
      rList.appendChild(li);
    });
    if (typeof focusAt === 'number') {
      var row = rList.querySelector('[data-pos="' + focusAt + '"]');
      if (row) row.focus();
    }
  }
  function rMove(pos, to) {
    if (to < 0 || to >= order.length) return pos;
    var v = order.splice(pos, 1)[0];
    order.splice(to, 0, v);
    return to;
  }
  function rSay(text) { if (rLive) rLive.textContent = text; }
  if (rBtn) rBtn.addEventListener('click', function () {
    if (!E || !enabled(rBtn) || !rDlg) return;
    secs = E.sections();
    order = secs.map(function (_s, i) { return i; });
    picked = null;
    rSay('');
    openDialog(rDlg, rBtn);
    rDraw(0);
  });
  if (rList) {
    rList.addEventListener('keydown', function (e) {
      var row = e.target.closest ? e.target.closest('.rrow') : null;
      if (!row) return;
      var pos = Number(row.getAttribute('data-pos'));
      var title = secs[order[pos]].title;
      if (e.key === ' ' || e.key === 'Spacebar') {
        e.preventDefault();
        if (picked === null) {
          picked = pos; pickedFrom = pos;
          rDraw(pos);
          rSay(title + ', picked up. Position ' + (pos + 1) + ' of ' + order.length
            + '. Up and Down to move, Space to put it down, Escape to cancel.');
        } else {
          picked = null;
          rDraw(pos);
          rSay(title + ', put down at position ' + (pos + 1) + ' of ' + order.length + '.');
        }
      } else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        e.preventDefault();
        var to = e.key === 'ArrowUp' ? pos - 1 : pos + 1;
        if (picked !== null) {
          var now = rMove(pos, to);
          picked = now;
          rDraw(now);
          rSay(title + ', position ' + (now + 1) + ' of ' + order.length + '.');
        } else if (to >= 0 && to < order.length) {
          rDraw(to);
        }
      } else if (e.key === 'Escape' && picked !== null) {
        e.preventDefault();
        var back = rMove(picked, pickedFrom);
        picked = null;
        rDraw(back);
        rSay(title + ', put back at position ' + (back + 1) + '.');
      }
    });
    rList.addEventListener('click', function (e) {
      var b = e.target.closest ? e.target.closest('[data-move]') : null;
      if (!b) return;
      var row = b.closest('.rrow');
      var pos = Number(row.getAttribute('data-pos'));
      var now = rMove(pos, b.getAttribute('data-move') === 'up' ? pos - 1 : pos + 1);
      picked = null;
      rDraw(now);
      rSay(secs[order[now]].title + ', position ' + (now + 1) + ' of ' + order.length + '.');
    });
  }
  if (rDlg) rDlg.addEventListener('cancel', function (e) {
    if (picked !== null) e.preventDefault();
  });
  var rDone = $('rearrange-done');
  if (rDone) rDone.addEventListener('click', function () {
    if (E) E.order(order.slice());
    closeDialog(rDlg);
    announce('Sections rearranged. Save version keeps the new order.');
  });
  var rCancel = $('rearrange-cancel');
  if (rCancel) rCancel.addEventListener('click', function () { closeDialog(rDlg); });

  // ---- Design
  var dDlg = $('design-dlg');
  var dBtn = $('tb-design');
  var dOpts = $('design-opts');
  var pick = null;
  var GROUPS = [
    ['font', 'Font', [['Arial', 'Arial'], ['Calibri', 'Calibri'], ['Georgia', 'Georgia']]],
    ['size', 'Text size', [[10, '10 pt'], [10.5, '10.5 pt'], [11, '11 pt']]],
    ['spacing', 'Spacing', [['compact', 'Compact'], ['standard', 'Standard'], ['relaxed', 'Relaxed']]],
    ['margins', 'Margins', [[15, '15 mm'], [20, '20 mm'], [25, '25 mm']]]
  ];
  function dDraw() {
    dOpts.textContent = '';
    GROUPS.forEach(function (g) {
      var label = document.createElement('span');
      label.className = 'ol';
      label.id = 'dl-' + g[0];
      label.textContent = g[1];
      dOpts.appendChild(label);
      var seg = document.createElement('span');
      seg.className = 'seg';
      seg.setAttribute('role', 'radiogroup');
      seg.setAttribute('aria-labelledby', 'dl-' + g[0]);
      seg.setAttribute('data-group', g[0]);
      g[2].forEach(function (o) {
        var b = document.createElement('button');
        b.type = 'button';
        b.setAttribute('role', 'radio');
        var on = pick[g[0]] === o[0];
        b.setAttribute('aria-checked', String(on));
        b.setAttribute('tabindex', on ? '0' : '-1');
        b.setAttribute('data-value', JSON.stringify(o[0]));
        b.textContent = o[1];
        seg.appendChild(b);
      });
      dOpts.appendChild(seg);
    });
    var label2 = document.createElement('span');
    label2.className = 'ol';
    label2.textContent = 'On screen';
    dOpts.appendChild(label2);
    var sw = document.createElement('button');
    sw.type = 'button';
    sw.className = 'switch';
    sw.id = 'design-numbers';
    sw.setAttribute('role', 'switch');
    sw.setAttribute('aria-checked', String(!(sheetEl && sheetEl.classList.contains('nums-off'))));
    sw.innerHTML = '<span class="track" aria-hidden="true"></span><span>Line numbers</span>'
      + '<span class="sw-word">in the margin here, never in the Word file</span>';
    dOpts.appendChild(sw);
    var pages = E ? E.pagesFor(pick) : [];
    var line = $('design-pages');
    if (line) line.textContent = 'With these settings: ' + plural(pages.length, 'page')
      + (pages.length > 2 ? ', over the two-page limit.' : '.') + ' An estimate: Word decides the breaks.';
  }
  function dChoose(groupKey, value, focus) {
    pick[groupKey] = value;
    dDraw();
    if (focus) {
      var seg = dOpts.querySelector('[data-group="' + groupKey + '"]');
      var b = seg ? seg.querySelector('[aria-checked="true"]') : null;
      if (b) b.focus();
    }
  }
  if (dBtn) dBtn.addEventListener('click', function () {
    if (!dDlg) return;
    pick = { font: design.font, size: design.size, spacing: design.spacing, margins: design.margins };
    openDialog(dDlg, dBtn);
    dDraw();
    var first = dOpts.querySelector('[aria-checked="true"]');
    if (first) first.focus();
  });
  if (dOpts) {
    dOpts.addEventListener('click', function (e) {
      var sw = e.target.closest ? e.target.closest('#design-numbers') : null;
      if (sw) { setNumbers(sw.getAttribute('aria-checked') !== 'true', true); return; }
      var b = e.target.closest ? e.target.closest('[role="radio"]') : null;
      if (!b) return;
      dChoose(b.parentNode.getAttribute('data-group'), JSON.parse(b.getAttribute('data-value')), true);
    });
    dOpts.addEventListener('keydown', function (e) {
      var b = e.target.closest ? e.target.closest('[role="radio"]') : null;
      if (!b) return;
      var keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
      if (!keys[e.key]) return;
      e.preventDefault();
      var seg = b.parentNode;
      var radios = all('[role="radio"]', seg);
      var i = radios.indexOf(b);
      var next = radios[(i + keys[e.key] + radios.length) % radios.length];
      dChoose(seg.getAttribute('data-group'), JSON.parse(next.getAttribute('data-value')), true);
    });
  }
  var dApply = $('design-apply');
  if (dApply) dApply.addEventListener('click', function () {
    dApply.disabled = true;
    post('/cv/design', JSON.stringify(pick)).then(function (r) {
      dApply.disabled = false;
      if (!r.j.ok) { $('design-pages').textContent = r.j.note || 'The design was not saved.'; return; }
      applyDesign(r.j.design);
      if (E) E.setDesign(r.j.design);
      closeDialog(dDlg);
      announce('Design saved. The sheet and the Word file use it from now on.');
    }).catch(function () {
      dApply.disabled = false;
      $('design-pages').textContent = 'The server did not answer. Nothing was saved.';
    });
  });
  var dStd = $('design-standard');
  if (dStd) dStd.addEventListener('click', function () {
    pick = { font: 'Arial', size: 10.5, spacing: 'standard', margins: 20 };
    dDraw();
  });
  var dCancel = $('design-cancel');
  if (dCancel) dCancel.addEventListener('click', function () { closeDialog(dDlg); });

  // ------------------------------------------------- the old tabs' addresses
  // Preferences is the Settings page now; History and the Word import are
  // the rail's Versions and Import.
  function fromHash() {
    var h = (typeof location !== 'undefined' && location.hash) || '';
    if (h === '#settings' || h === '#preferences') { location.replace('/settings'); return; }
    var at = { '#history': 'versions-sec', '#compare': 'compare', '#import': 'import-sec' }[h];
    var el = at ? ($(at) || $('versions-sec')) : null;
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'start' });
  }
  fromHash();
  window.addEventListener('hashchange', fromHash);

  // ------------------------------------------------- keys
  // Only the combinations the design draws, and no single-key shortcuts: Ctrl S
  // saves a version, from anywhere, a line being typed in included (the keys
  // panel says so), but does nothing while Rearrange or Design is open, whose
  // choices are not yet in the CV; Ctrl Z undoes and Ctrl Shift Z redoes the
  // unsaved edits outside a field, where a field's own undo is left to it. Alt
  // Shift and the arrows are the editor's, Left and Right the toolbar's and
  // the tabs'.
  document.addEventListener('keydown', function (e) {
    if (!e.ctrlKey || e.altKey || e.metaKey) return;
    var k = (e.key || '').toLowerCase();
    if (k === 's') {
      e.preventDefault();
      if (dialogOpen()) return;
      if (saveBtn && !saveBtn.hidden && !saveBtn.disabled) saveBtn.click();
      return;
    }
    if (k === 'z' && !typing(e.target) && E && !dialogOpen()) {
      e.preventDefault();
      if (e.shiftKey) E.redo(); else E.undo();
    }
  });

  // ------------------------------------------------- Rescore affected
  var confirmBox = $('rescore-confirm');
  var confirmText = $('rescore-confirm-text');
  var goBtn = $('rescore-go');
  var runBox = $('rescore-run');
  var runText = $('rescore-run-text');
  var runBar = $('rescore-bar');
  var confirming = null;
  var trigger = null;
  function openConfirm(summary, from) {
    if (!summary || !confirmBox) return;
    confirming = summary;
    trigger = from || null;
    all('[data-rescore]').forEach(function (b) { b.setAttribute('aria-expanded', String(b === from)); });
    var n = summary.count;
    var kept = [];
    if (summary.spec) kept.push(plural(summary.spec, 'role') + ' scored on ' + (summary.spec === 1 ? 'its' : 'their') + ' full job description');
    if (summary.applied) kept.push(plural(summary.applied, 'role') + ' you have applied for');
    var keptWords = kept.length ? ' ' + kept.join(' and ') + ' keep' + (summary.spec + summary.applied === 1 ? 's its score' : ' their scores') + ' and ' + (summary.spec + summary.applied === 1 ? 'is' : 'are') + ' not counted.' : '';
    var lead = summary.scope === 'stale'
      ? 'Rescore ' + plural(n, 'role') + ' on version ' + D.activeNumber + ' of the CV?'
      : 'Rescore all ' + plural(n, 'open role') + ' on the preferences as they are now?';
    confirmText.innerHTML = '';
    var p = document.createElement('p');
    var strong = document.createElement('strong');
    strong.textContent = lead;
    p.appendChild(strong);
    p.appendChild(document.createTextNode(' That is ' + plural(n, 'paid call') + ', ' + summary.cost.words
      + ', in one run. Each role is scored on its advert card, as it was before.' + keptWords
      + ' Acknowledged roles are left alone.'));
    confirmText.appendChild(p);
    if (n > 0) {
      goBtn.hidden = false;
      goBtn.textContent = 'Rescore ' + plural(n, 'role') + ', ' + summary.cost.words;
    } else {
      goBtn.hidden = true;
    }
    confirmBox.hidden = false;
    confirmBox.focus();
  }
  function closeConfirm(back) {
    if (!confirmBox) return;
    confirmBox.hidden = true;
    all('[data-rescore]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
    if (back && trigger && trigger.focus) trigger.focus();
    confirming = null;
  }
  document.addEventListener('click', function (e) {
    var b = e.target && e.target.closest ? e.target.closest('[data-rescore]') : null;
    if (!b) return;
    var scope = b.getAttribute('data-rescore');
    openConfirm(scope === 'stale' ? D.stale : (offered || D.open), b);
  });
  var cancelBtn = $('rescore-cancel');
  if (cancelBtn) cancelBtn.addEventListener('click', function () { closeConfirm(true); });
  if (confirmBox) confirmBox.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { e.preventDefault(); closeConfirm(true); }
  });
  // The run's words go into #rescore-live too, a status region that is on the
  // page, empty, from the start, so the first of them is read out: the box
  // itself is unhidden and filled in one step. Only a change of state or of
  // the roles done is written there, not every poll.
  var runLive = $('rescore-live');
  var lastSaid = '';
  function sayRun(words) {
    if (!runLive || words === lastSaid) return;
    lastSaid = words;
    runLive.textContent = words;
  }
  function centsWords(c) {
    var n = Math.round(c || 0);
    return n < 1 ? 'under a cent' : (n === 1 ? '1 cent' : n + ' cents');
  }
  function showRun(st) {
    if (!runBox) return;
    runBox.hidden = false;
    var mark = $('rescore-mark');
    var n = st.count || 0;
    var spent = Math.round(st.cost_cents || 0);
    var bar = runBar ? runBar.querySelector('span') : null;
    if (runBar) { runBar.setAttribute('aria-valuemax', String(n || 1)); runBar.setAttribute('aria-valuenow', String(st.done || 0)); }
    var through = (st.done || 0) + (st.failed || 0) + (st.left || 0) + (st.unstored || 0);
    if (bar && bar.style) bar.style.width = (n ? Math.round(100 * through / n) : 100) + '%';
    if (st.state === 'running') {
      runBox.className = 'notice notice-info';
      if (mark) mark.innerHTML = ICON.spin;
      var at = Math.min(n, through + 1);
      runText.textContent = 'Rescoring ' + at + ' of ' + n + '. '
        + (st.mode === 'mock' ? 'On the test mocks, nothing paid so far. '
          : centsWords(st.cost_cents).replace(/^u/, 'U') + ' so far. ')
        + 'It carries on if you leave this page; Jobs updates each role as it is done.'
        + (st.unstored ? ' ' + plural(st.unstored, 'role') + ' scored but not stored so far: the database was busy.' : '');
      if (runBar) runBar.hidden = false;
      sayRun(through === 0 ? 'Rescore started: ' + plural(n, 'role') + '.' : runText.textContent);
    } else if (st.state === 'done') {
      var r = st.result || {};
      runBox.className = 'notice notice-ok';
      if (mark) mark.innerHTML = ICON.tick;
      var paid = r.rescored + r.failed + (r.unstored || 0);
      var words = 'Rescored ' + r.rescored + ' of ' + r.count + ', ' + (r.mode === 'mock'
        ? 'on the test mocks, so nothing was paid.'
        : plural(paid, 'paid call') + ', ' + (spent < 1 ? 'under a cent' : spent + ' cents') + '.');
      var movedN = (r.rose || 0) + (r.fell || 0);
      words += movedN ? ' ' + plural(movedN, 'role') + ' moved tier: ' + r.rose + ' rose, ' + r.fell + ' fell.'
        : ' No role moved tier.';
      if (r.failed) words += ' ' + plural(r.failed, 'role') + ' ' + (r.failed === 1 ? 'was' : 'were')
        + ' not rescored: the reply could not be used, so ' + (r.failed === 1 ? 'it keeps its' : 'they keep their') + ' earlier score.';
      if (r.unstored) words += ' ' + plural(r.unstored, 'role') + ' ' + (r.unstored === 1 ? 'was' : 'were')
        + ' scored and paid for but not stored: the database stayed busy, so '
        + (r.unstored === 1 ? 'it keeps its' : 'they keep their') + ' earlier score and '
        + (r.unstored === 1 ? 'is' : 'are') + ' still flagged.';
      if (r.left) words += ' ' + plural(r.left, 'role') + ' applied for or scored on the full description meanwhile ' + (r.left === 1 ? 'was' : 'were') + ' left alone.';
      runText.textContent = words + ' Reload the page to see what is still flagged.';
      if (runBar) runBar.hidden = true;
      sayRun(runText.textContent);
    } else if (st.state === 'refused' || st.state === 'failed') {
      runBox.className = 'notice notice-bad';
      if (mark) mark.innerHTML = ICON.bad;
      runText.textContent = (st.note || 'The run did not start.')
        + (st.state === 'failed' && st.cost_cents ? ' It had cost ' + centsWords(st.cost_cents) + '.' : '')
        + ' Reload the page to see the count as it is now.';
      if (runBar) runBar.hidden = true;
      sayRun(runText.textContent);
    }
  }
  var polling = false;
  function poll() {
    if (polling) return;
    polling = true;
    fetch('/cv/rescore/status').then(function (r) { return r.json(); }).then(function (st) {
      polling = false;
      showRun(st);
      if (st.state === 'running') setTimeout(poll, 1500);
    }).catch(function () { polling = false; setTimeout(poll, 4000); });
  }
  if (goBtn) goBtn.addEventListener('click', function () {
    if (!confirming || goBtn.disabled) return;
    var summary = confirming;
    goBtn.disabled = true;
    post('/cv/rescore', JSON.stringify({ confirm: true, scope: summary.scope, expect: summary.count,
                                         ids_hash: summary.ids_hash }))
      .then(function (r) {
        goBtn.disabled = false;
        closeConfirm(false);
        if (!r.j.ok) { showRun({ state: 'refused', note: r.j.note }); runBox.focus(); return; }
        showRun(r.j);
        runBox.focus();
        poll();
      }).catch(function () {
        goBtn.disabled = false;
        showRun({ state: 'failed', note: 'The server did not answer, so nothing was started.' });
      });
  });
  if (D.run && D.run.state === 'running') { showRun(D.run); poll(); }
  else if (D.run && D.run.state === 'done' && D.run.finished
           && Date.now() - Date.parse(D.run.finished) < 600000) showRun(D.run);

  // ------------------------------------------------- Preferences
  var form = $('prefs-form');
  var say = $('prefs-say');
  var KEYS = ['day_rate_floor_gbp', 'salary_floor_gbp', 'cv_pass_score', 'cv_floor_score'];
  function field(k) { return $('pref-' + k); }
  function changedValues() {
    var out = {};
    KEYS.forEach(function (k) {
      var f = field(k);
      if (f && String(f.value).trim() !== String(f.getAttribute('data-was') || '')) out[k] = String(f.value).trim();
    });
    return out;
  }
  function markChanged() {
    KEYS.forEach(function (k) {
      var f = field(k);
      if (!f) return;
      var was = f.getAttribute('data-was') || '';
      var on = String(f.value).trim() !== was;
      f.classList.toggle('changed', on);
      var tag = f.parentNode.querySelector('.was-' + k);
      if (on && !tag) {
        tag = document.createElement('span');
        tag.className = 'tag tag-caution was-' + k;
        f.parentNode.appendChild(tag);
      }
      if (tag) {
        tag.hidden = !on;
        tag.textContent = 'Changed from ' + (was || 'nothing');
      }
    });
  }
  function sayIn(el, text, bad) {
    if (!el) return;
    el.textContent = text || '';
    el.className = 'say' + (bad ? ' bad' : '');
  }
  var previewTimer = null;
  var previewSeq = 0;
  function preview() {
    var values = changedValues();
    if (!Object.keys(values).length) { sayIn(say, ''); return; }
    var seq = ++previewSeq;
    post('/cv/preferences', JSON.stringify({ values: values, dry_run: true })).then(function (r) {
      if (seq !== previewSeq) return;
      sayIn(say, r.j.note || '', !r.j.ok);
    }).catch(function () {});
  }
  if (form) {
    form.addEventListener('input', function (e) {
      if (KEYS.indexOf((e.target.id || '').replace('pref-', '')) < 0) return;
      e.target.removeAttribute('aria-invalid');
      markChanged();
      if (previewTimer) clearTimeout(previewTimer);
      previewTimer = setTimeout(preview, 500);
    });
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      submitPrefs();
    });
  }
  function submitPrefs() {
    {
      var values = changedValues();
      var bad = KEYS.filter(function (k) {
        return k in values && !/^\s*£?\s*[0-9][0-9,]*(\.[0-9]+)?\s*$/.test(values[k]);
      });
      if (bad.length) {
        var f = field(bad[0]);
        f.setAttribute('aria-invalid', 'true');
        f.focus();
        sayIn(say, 'That is not a plain number. Type digits only, such as 750.', true);
        return;
      }
      if (!Object.keys(values).length) { sayIn(say, 'Nothing has changed, so there is nothing to save.'); return; }
      var btn = $('prefs-save');
      btn.disabled = true;
      sayIn(say, 'Saving and re-sorting the roles already scored.');
      post('/cv/preferences', JSON.stringify({ values: values })).then(function (r) {
        btn.disabled = false;
        sayIn(say, r.j.note || (r.j.ok ? 'Saved.' : 'That did not save.'), !r.j.ok);
        if (!r.j.ok) {
          var at = KEYS.filter(function (k) { return k in values; })[0];
          if (r.j.invalid && at) { field(at).setAttribute('aria-invalid', 'true'); field(at).focus(); }
          return;
        }
        if (r.j.saved && r.j.resorted === false) {
          // Saved, but the roles were not re-sorted (the database was busy).
          // The fields stay marked as changed from what the roles were
          // sorted on, so the next press saves the same values and re-sorts.
          var again = document.createElement('button');
          again.type = 'button';
          again.className = 'btn btn-secondary';
          again.id = 'prefs-resort';
          again.textContent = 'Re-sort now';
          say.textContent = '';
          say.appendChild(document.createTextNode((r.j.note || 'Saved, but not re-sorted.') + ' '));
          say.appendChild(again);
          return;
        }
        if (r.j.saved) {
          Object.keys(r.j.values || {}).forEach(function (k) {
            var f = field(k);
            if (f) { f.value = r.j.values[k]; f.setAttribute('data-was', r.j.values[k]); }
          });
          markChanged();
        }
      }).catch(function () {
        btn.disabled = false;
        sayIn(say, 'The server did not answer, so nothing was saved.', true);
      });
    }
  }
  // Re-sort now, after a save the database was too busy to re-sort: the same
  // values again, which writes the same lines and re-sorts.
  if (say) say.addEventListener('click', function (e) {
    var b = e.target && e.target.closest ? e.target.closest('#prefs-resort') : null;
    if (!b || !form) return;
    var btn = $('prefs-save');
    if (btn) btn.focus();
    submitPrefs();
  });
  var offered = null;
  var sectionSay = $('section-say');
  document.addEventListener('click', function (e) {
    var b = e.target && e.target.closest ? e.target.closest('[data-section-save]') : null;
    if (b) {
      var key = b.getAttribute('data-section-save');
      var area = $('pref-' + key);
      if (String(area.value).trim() === String(area.getAttribute('data-was') || '').trim()) {
        sayIn(sectionSay, 'Nothing has changed, so there is nothing to save.');
        return;
      }
      b.disabled = true;
      post('/cv/preferences/section', JSON.stringify({ key: key, text: area.value })).then(function (r) {
        b.disabled = false;
        if (!r.j.ok) {
          area.setAttribute('aria-invalid', 'true');
          area.focus();
          sayIn(sectionSay, r.j.note || 'That did not save.', true);
          return;
        }
        area.removeAttribute('aria-invalid');
        area.setAttribute('data-was', r.j.text);
        offered = r.j.offer;
        sectionSay.className = 'say offer';
        sectionSay.textContent = r.j.note + ' ';
        if (offered && offered.count) {
          var go = document.createElement('button');
          go.type = 'button';
          go.className = 'btn btn-secondary';
          go.setAttribute('data-rescore', 'open');
          go.setAttribute('aria-expanded', 'false');
          go.setAttribute('aria-controls', 'rescore-confirm');
          go.textContent = 'Rescore all ' + plural(offered.count, 'open role') + ', ' + offered.cost.words;
          var leave = document.createElement('button');
          leave.type = 'button';
          leave.className = 'btn btn-ghost';
          leave.setAttribute('data-leave', '1');
          leave.textContent = 'Leave them';
          sectionSay.appendChild(go);
          sectionSay.appendChild(leave);
        }
      }).catch(function () {
        b.disabled = false;
        sayIn(sectionSay, 'The server did not answer, so nothing was saved.', true);
      });
      return;
    }
    var leaveBtn = e.target && e.target.closest ? e.target.closest('[data-leave]') : null;
    if (leaveBtn) {
      sayIn(sectionSay, 'Left as they are. Roles scored from now on read the new words.');
      offered = null;
    }
  });

  // ------------------------------------------------- the Word import
  var fileInput = $('cv-file');
  var chooseBtn = $('import-choose');
  if (chooseBtn && fileInput) chooseBtn.addEventListener('click', function () { fileInput.click(); });
  var token = null;
  var aborter = null;
  function importShow(which) {
    ['import-reading', 'import-failed', 'cv-stage'].forEach(function (id) {
      var el = $(id);
      if (el) el.hidden = id !== which;
    });
  }
  if (fileInput) fileInput.addEventListener('change', function () {
    var file = fileInput.files && fileInput.files[0];
    if (!file) return;
    $('import-name').textContent = file.name + (file.size ? ', ' + Math.max(1, Math.round(file.size / 1024)) + ' KB.' : '.');
    importShow('import-reading');
    aborter = typeof AbortController !== 'undefined' ? new AbortController() : null;
    fetch('/cv/preview', { method: 'POST', body: file, headers: { 'X-Filename': file.name },
                           signal: aborter ? aborter.signal : undefined })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        aborter = null;
        fileInput.value = '';
        if (!j.ok) {
          $('import-failed-text').textContent = file.name + ' was not imported. ' + (j.note || '') + ' Nothing changed.';
          importShow('import-failed');
          return;
        }
        token = j.token;
        var rep = j.report || {};
        var differ = rep.differ || 0;
        $('cv-note').textContent = 'Read ' + plural(rep.lines_read || 0, 'line') + ' from ' + file.name
          + (rep.laid_text ? ', with ' + plural(differ, 'difference') + ' from how the editor lays a CV out. Check them before you save.'
            : '. ' + (rep.problem || '') + ' Saving keeps it as it was read, and the page then asks you to lay it out.');
        var list = $('import-guesses');
        list.textContent = '';
        (rep.guesses || []).forEach(function (g) {
          var li = document.createElement('li');
          li.innerHTML = '<span class="dk"></span><span></span>';
          li.children[0].textContent = g.from;
          li.children[1].textContent = 'read as ' + g.to;
          list.appendChild(li);
        });
        if (rep.bullet_marks) {
          var li2 = document.createElement('li');
          li2.innerHTML = '<span class="dk"></span><span>read as bullets</span>';
          li2.children[0].textContent = plural(rep.bullet_marks, 'line') + ' with other marks';
          list.appendChild(li2);
        }
        $('import-against').textContent = rep.against_words
          ? 'Against version ' + D.activeNumber + ' of the master: ' + rep.against_words + '.' : '';
        $('cv-preview').textContent = rep.laid_text || j.markdown;
        importShow('cv-stage');
        $('cv-preview').focus();
      })
      .catch(function (err) {
        aborter = null;
        fileInput.value = '';
        if (err && err.name === 'AbortError') { importShow(null); return; }
        $('import-failed-text').textContent = file.name + ' was not imported: the server did not answer. Nothing changed.';
        importShow('import-failed');
      });
  });
  var importCancel = $('import-cancel');
  if (importCancel) importCancel.addEventListener('click', function () {
    if (aborter) aborter.abort();
    importShow(null);
  });
  var confirmImport = $('cv-confirm');
  if (confirmImport) confirmImport.addEventListener('click', function () {
    if (!token || confirmImport.disabled) return;
    confirmImport.disabled = true;
    post('/cv/confirm', JSON.stringify({ token: token, laid_out: true })).then(function (r) {
      if (!r.j.ok) { confirmImport.disabled = false; $('cv-note').textContent = r.j.note || 'It was not saved.'; return; }
      window.location.href = '/cv';
    }).catch(function () { confirmImport.disabled = false; });
  });
  var discardImport = $('cv-discard');
  if (discardImport) discardImport.addEventListener('click', function () {
    if (!token) return;
    post('/cv/discard', JSON.stringify({ token: token })).then(function () {
      token = null;
      importShow(null);
      if (chooseBtn) chooseBtn.focus();
    });
  });

  // ------------------------------------------- an older version, viewed
  var restore = $('view-restore');
  if (restore) restore.addEventListener('click', function () {
    if (!D.viewText || restore.disabled) return;
    if (!window.confirm('Save this older version as version ' + (D.activeNumber + 1)
        + ', the one the scorer reads? The versions in between stay in History.')) return;
    restore.disabled = true;
    post('/cv/master/save', JSON.stringify({ text: D.viewText, base: D.active })).then(function (r) {
      if (!r.j.ok) {
        restore.disabled = false;
        var n = $('cv-say');
        if (n) { n.textContent = r.j.note || 'It was not saved.'; n.className = 'cv-note bad'; }
        return;
      }
      window.location.href = '/cv';
    }).catch(function () { restore.disabled = false; });
  });
})();
"""
