#!/usr/bin/env python
"""The tailored CV: one copy of the master per role, kept in tailored_cvs.

A copy is made from the master CV the first time a role's Prepare application
page opens, with no model call, and from then on it is edited on that page.
Nothing here writes to the master, which lives in config/profile/ and is only
ever changed on the CV page. The copy is keyed on the role, so every advert in
a repost chain shares it: the role_key, or the row id for a row that has none.

This module only stores and reads. It takes no lock and decides nothing about
whether a role may be touched; prepare_application.py does that, under its
locks, and calls these from inside its write transactions. Every function that
writes leaves the commit to its caller.

What a copy holds:
- cv_text and cv_hash, the copy as it stands on the page after the last Rescore
  role, Save CV or Start again from master, in the editor's layout,
- saved_text, saved_hash, saved_name and saved_at, what Save CV stored, which
  Download builds the Word file from and Applied names,
- score, the latest tailored score as JSON, which is the copy's and never the
  role's: the role keeps the master CV's score, which the Jobs page shows.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import cv_doc  # noqa: E402
import cv_store  # noqa: E402
import radar_common as rc  # noqa: E402
import score_item  # noqa: E402

RECORD_VERSION = 1
GATES = rc.GATE_NAMES


def role_ref(row) -> str:
    """What a copy is keyed on: the role, or the row where there is no role key."""
    key = rc._row_field(row, "role_key")
    return f"key:{key}" if key else f"id:{int(rc._row_field(row, 'id'))}"


def copy_version(cv_hash: str | None) -> str:
    """The CV version a tailored score records, as a master score records a file."""
    return f"tailored-{cv_hash}"


def copy_source(cv_hash: str | None) -> str:
    """Where the scorer is told the CV text came from, for a tailored copy."""
    return f"tailored copy {cv_hash}"


# ------------------------------------------------------------- reading

def get(conn, ref: str) -> dict | None:
    """The copy for a role, or None. A database with no table yet has none."""
    try:
        row = conn.execute("SELECT * FROM tailored_cvs WHERE role_ref = ?",
                           (ref,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row is not None else None


def for_row(conn, row) -> dict | None:
    return get(conn, role_ref(row))


def read_score(copy: dict | None) -> dict | None:
    """The copy's latest tailored score, if one is stored and can be read."""
    raw = (copy or {}).get("score")
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if (not isinstance(record, dict) or record.get("v") != RECORD_VERSION
            or not isinstance((record.get("working") or {}).get("asks"), list)):
        return None
    return record


def master(config: dict) -> dict:
    """The master CV, as a copy of it would start. {"doc", "text", "version"}.

    The text is the master in the editor's layout, which is the text itself
    for a master saved on the CV page and the imported reading of any other.
    A master that cannot be read is a CvError, so a page can say so.
    """
    try:
        text, _source = score_item.load_cv_text(config)
        version = score_item.get_cv_version(config)
    except SystemExit as err:   # the fixture refusal, before any call
        raise cv_doc.CvError(str(err)) from None
    except OSError as err:
        raise cv_doc.CvError(f"The master CV could not be read. {err}") from None
    doc = cv_doc.load_doc(text)
    return {"doc": doc, "text": cv_doc.render(doc), "version": version}


def master_working(row) -> dict | None:
    """The per-ask working of the role's own score, when it is a full
    description score whose working belongs to it. The rule
    prepare_application.load_working applies, read here without it."""
    raw = rc._row_field(row, "score_working")
    if not raw or rc._row_field(row, "score_basis") != rc.SCORE_BASIS_SPEC:
        return None
    try:
        working = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if (not isinstance(working, dict) or not isinstance(working.get("asks"), list)
            or working.get("scored_at") != rc._row_field(row, "scored_at")):
        return None
    return working


def master_scored_pair(row, description_hash: str | None,
                       cv_hash: str | None) -> bool:
    """Is the role's own score one of exactly this description and this CV
    text, so that a copy that is the master unchanged has a score already."""
    working = master_working(row)
    return bool(working and description_hash and cv_hash
                and rc._row_field(row, "description_hash") == description_hash
                and working.get("cv_hash") == cv_hash)


def chain_ids(conn, row) -> list[int]:
    """Every row of the role: the row, and its reposts when it has a key."""
    key = rc._row_field(row, "role_key")
    rid = int(rc._row_field(row, "id"))
    if not key:
        return [rid]
    return [r[0] for r in conn.execute(
        "SELECT id FROM opportunities WHERE id = ? OR role_key = ?", (rid, key))]


def history_pair(conn, row, description_hash: str | None,
                 cv_hash: str | None) -> dict | None:
    """The newest tailored score of this role for exactly this description
    and this copy, from the history, or None."""
    if not description_hash or not cv_hash:
        return None
    ids = chain_ids(conn, row)
    found = conn.execute(
        "SELECT * FROM score_history WHERE event = ? AND description_hash = ?"
        " AND cv_hash = ? AND opportunity_id IN ("
        + ",".join("?" * len(ids)) + ") ORDER BY id DESC LIMIT 1",
        (rc.TAILORED_EVENT, description_hash, cv_hash, *ids)).fetchone()
    return dict(found) if found is not None else None


# ------------------------------------------------------------- writing

def create(conn, ref: str, row_id: int, text: str, master_version: str | None,
           now: str | None = None) -> bool:
    """Make the copy, unless the role has one. True when it was made."""
    now = now or rc.now_iso()
    cur = conn.execute(
        "INSERT OR IGNORE INTO tailored_cvs (role_ref, opportunity_id,"
        " created_at, master_version, cv_text, cv_hash, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (ref, row_id, now, master_version, text, cv_store.cv_hash(text), now))
    return cur.rowcount == 1


def store_text(conn, ref: str, text: str, now: str | None = None) -> bool:
    """Make the copy's text this. True when it changed."""
    cur = conn.execute(
        "UPDATE tailored_cvs SET cv_text = ?, cv_hash = ?, updated_at = ?"
        " WHERE role_ref = ? AND (cv_text IS NULL OR cv_text <> ?)",
        (text, cv_store.cv_hash(text), now or rc.now_iso(), ref, text))
    return cur.rowcount == 1


def restart(conn, ref: str, text: str, master_version: str | None,
            now: str | None = None) -> None:
    """Make the copy the master again, as Start again from master does. The
    saved copy and the scores stay, they are records of what happened."""
    conn.execute(
        "UPDATE tailored_cvs SET cv_text = ?, cv_hash = ?, updated_at = ?,"
        " master_version = ? WHERE role_ref = ?",
        (text, cv_store.cv_hash(text), now or rc.now_iso(), master_version, ref))


def save(conn, ref: str, text: str, name: str, now: str | None = None) -> None:
    """Store the copy as saved, under its name, in place of the save before."""
    now = now or rc.now_iso()
    conn.execute(
        "UPDATE tailored_cvs SET cv_text = ?, cv_hash = ?, updated_at = ?,"
        " saved_text = ?, saved_hash = ?, saved_name = ?, saved_at = ?"
        " WHERE role_ref = ?",
        (text, cv_store.cv_hash(text), now, text, cv_store.cv_hash(text),
         name, now, ref))


def set_score(conn, ref: str, record: dict) -> None:
    conn.execute("UPDATE tailored_cvs SET score = ? WHERE role_ref = ?",
                 (json.dumps(record, ensure_ascii=False), ref))


# ------------------------------------------------------ the score record

def _record_from_scored(scored: dict, working: dict) -> dict:
    """The fields of a tailored score, from what score_opportunity returned."""
    try:
        failed = json.loads(scored.get("failed_gates") or "[]")
    except (TypeError, ValueError):
        failed = []
    return {
        "scored_at": scored.get("scored_at"),
        "prompt_version": scored.get("prompt_version"),
        "score_model": scored.get("score_model"),
        "cv_match": scored.get("cv_match"),
        "tier": scored.get("tier"),
        "failed_gates": failed if isinstance(failed, list) else [],
        "filter_reason": scored.get("filter_reason"),
        "gates": {g: scored.get(f"gate_{g}") for g in GATES},
        "notes": {g: scored.get(f"gate_{g}_note") for g in GATES},
        "one_line_why": scored.get("one_line_why"),
        "question_text": scored.get("question_text"),
        "working": working,
    }


def _record_from_history(found: dict) -> dict:
    """The fields of a tailored score, from the history row that kept it."""
    try:
        gates = json.loads(found.get("gates") or "{}")
    except (TypeError, ValueError):
        gates = {}
    try:
        working = json.loads(found.get("working") or "null")
    except (TypeError, ValueError):
        working = None
    gates = gates if isinstance(gates, dict) else {}
    return {
        "scored_at": found.get("scored_at"),
        "prompt_version": found.get("prompt_version"),
        "score_model": found.get("score_model"),
        "cv_match": found.get("cv_match"),
        "tier": found.get("tier"),
        "failed_gates": [g for g in GATES if gates.get(g) == 0],
        "filter_reason": found.get("filter_reason"),
        "gates": {g: gates.get(g) for g in GATES},
        "notes": {},
        "one_line_why": found.get("one_line_why"),
        "question_text": None,
        "working": working if isinstance(working, dict) else {"asks": []},
    }


def before_of(row, copy: dict | None, description_hash: str | None) -> dict | None:
    """The score the copy was shown with before this one, for the comparison.

    The copy's own last score, if it was made on this description, else the
    role's own score, which is the master CV's, with its asks only where it is
    a full description score made on this description.
    """
    prev = read_score(copy)
    if prev and prev.get("description_hash") == description_hash:
        return {"source": "tailored", "basis": rc.SCORE_BASIS_SPEC,
                "cv_match": prev.get("cv_match"), "tier": prev.get("tier"),
                "scored_at": prev.get("scored_at"), "cv_hash": prev.get("cv_hash"),
                "asks": (prev.get("working") or {}).get("asks") or []}
    if rc._row_field(row, "tier") is None and rc._row_field(row, "cv_match") is None:
        return None
    working = master_working(row)
    on_this = (working is not None and description_hash is not None
               and rc._row_field(row, "description_hash") == description_hash)
    return {"source": "master", "basis": rc._row_field(row, "score_basis"),
            "cv_match": rc._row_field(row, "cv_match"),
            "tier": rc._row_field(row, "tier"),
            "scored_at": rc._row_field(row, "scored_at"),
            "cv_hash": working.get("cv_hash") if on_this else None,
            "asks": working.get("asks") if on_this else None}


def make_record(row, fields: dict, description_hash: str, cv_hash: str,
                before: dict | None, reused: bool = False) -> dict:
    """The tailored score as it is stored on the copy, with what it replaced
    and which asks changed against it."""
    after = (fields.get("working") or {}).get("asks") or []
    compared = before is not None and before.get("asks") is not None
    return {
        "v": RECORD_VERSION,
        "opportunity_id": int(rc._row_field(row, "id")),
        "description_hash": description_hash,
        "cv_hash": cv_hash,
        **fields,
        "before": ({k: v for k, v in before.items() if k != "asks"}
                   if before else None),
        "changes": cv_doc.ask_changes(before["asks"], after) if compared else None,
        "reused": reused,
    }


def history_state(record: dict) -> dict:
    """The twelve fields the history keeps, for a tailored score."""
    gates = record.get("gates") or {}
    return {
        "scored_at": record.get("scored_at"),
        "score_basis": rc.SCORE_BASIS_SPEC,
        "prompt_version": record.get("prompt_version"),
        "score_model": record.get("score_model"),
        "cv_version": copy_version(record.get("cv_hash")),
        "cv_match": record.get("cv_match"),
        "gates": json.dumps({g: gates.get(g) for g in GATES}, sort_keys=True),
        "tier": record.get("tier"),
        "filter_reason": record.get("filter_reason"),
        "one_line_why": record.get("one_line_why"),
        "description_hash": record.get("description_hash"),
        "working": json.dumps(record.get("working"), ensure_ascii=False),
    }


def from_scored(row, copy, scored: dict, working: dict, description_hash: str,
                cv_hash: str) -> dict:
    return make_record(row, _record_from_scored(scored, working),
                       description_hash, cv_hash,
                       before_of(row, copy, description_hash))


def from_history(row, copy, found: dict, description_hash: str,
                 cv_hash: str) -> dict:
    return make_record(row, _record_from_history(found), description_hash,
                       cv_hash, before_of(row, copy, description_hash),
                       reused=True)


# ------------------------------------------------- what the page shows

def view(copy: dict | None, text: str | None = None) -> dict | None:
    """The copy as the page and its script take it.

    text, when given, is shown in place of the copy's own, which is how an
    applied role shows the copy it was saved with. lines are cv_store.cv_lines
    of the text, the very lines the scorer numbers.
    """
    if not copy:
        return None
    shown = (copy.get("cv_text") or "") if text is None else text
    saved = None
    if copy.get("saved_text"):
        saved = {"name": copy.get("saved_name"), "at": copy.get("saved_at"),
                 "same": copy.get("saved_hash") == cv_store.cv_hash(shown)}
    return {"text": shown, "hash": cv_store.cv_hash(shown),
            "doc": cv_doc.load_doc(shown), "lines": cv_store.cv_lines(shown),
            "master_version": copy.get("master_version"),
            "created_at": copy.get("created_at"),
            "updated_at": copy.get("updated_at"), "saved": saved}


def standing(row, copy: dict | None, cv_hash: str | None) -> dict:
    """Which tailored score belongs beside the copy the page shows.

    {"kind": ..., "record": record or None}. current is the copy's score, made
    on this description and on this very text. earlier is one made on this
    description and an earlier text of the copy. master means the copy is the
    master CV unchanged, as the role's own score was made on it, so that score
    stands for it. elsewhere is a score the copy has, made on another advert's
    description. none is no score.
    """
    desc = rc.description_version(rc._row_field(row, "description_text"))
    record = read_score(copy)
    if record and desc and record.get("description_hash") == desc:
        kind = "current" if record.get("cv_hash") == cv_hash else "earlier"
        return {"kind": kind, "record": record}
    if master_scored_pair(row, desc, cv_hash):
        return {"kind": "master", "record": None}
    if record:
        return {"kind": "elsewhere", "record": record}
    return {"kind": "none", "record": None}
