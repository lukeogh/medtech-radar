"""The experience bank (owner, 18 Nov): one entry is one thing the owner did,
with the evidence that backs it. The pages are experience_page.py; Prepare
application reads the backed entries through matches_for_advert.

The rules, in one place:

- An entry is draft (To review), backed or parked. Every entry arrives as a
  draft, whether it was pasted or added by hand.
- Mark backed needs at least one evidence item or a named voucher, and a
  stage that is not discussed (backing_problem). The server holds the rule on
  every save, so a backed entry cannot be edited out of it either.
- Deleting an entry deletes its evidence in the same transaction.
- A paste is read in code, never by a model (parse_paste). Every block it can
  read is added as a draft; a block it cannot read adds nothing and is
  reported with its first line and why.
- A field over its cap (CAPS) is refused, never cut, so no words are lost
  without being seen.

Everything here takes a connection and leaves committing to the caller.
"""

from __future__ import annotations

import re
import sqlite3

STAGES = (("delivered", "Delivered"), ("drafted", "Drafted"), ("discussed", "Discussed"))
STATUSES = (("draft", "To review"), ("backed", "Backed"), ("parked", "Parked"))
LEVELS = (("can_show", "Can show"), ("describe_only", "Describe only"), ("public", "Public"))
STAGE_WORD = dict(STAGES)
LEVEL_WORD = dict(LEVELS)

# The longest each field may be, in characters. A paste or a save over one is
# refused with the field named.
CAPS = {"title": 200, "did": 2000, "result": 1000, "period": 100, "place": 200,
        "vouch": 200, "questions": 1000,
        "ev_name": 200, "ev_location": 300, "ev_shows": 500}
TAG_CAP = 60          # one tag
TAGS_MAX = 6          # tags on an entry
EVIDENCE_MAX = 20     # evidence items on an entry
PASTE_MAX = 200_000   # one paste, in characters

FIELD_WORD = {"title": "Title", "did": "What I did", "result": "Result", "period": "When",
              "place": "Where", "vouch": "Who can vouch", "questions": "Questions",
              "ev_name": "An evidence name", "ev_location": "Where an evidence item lives",
              "ev_shows": "What an evidence item shows"}

NEEDS_BACKING = "Add evidence or someone who can vouch."
DISCUSSED = "Discussed work can't be backed."

TEXT_FIELDS = ("title", "did", "result", "period", "place", "vouch", "questions")


def has_tables(conn) -> bool:
    return conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN"
                        " ('experience_entries', 'experience_evidence')").fetchone()[0] == 2


def tag_list(raw) -> list[str]:
    """Tags from a comma-separated line or a list: trimmed, empties dropped,
    repeats dropped whatever their case, in their order."""
    parts = raw if isinstance(raw, list) else str(raw or "").split(",")
    out: list[str] = []
    for part in parts:
        tag = " ".join(str(part).split())
        if tag and tag.casefold() not in (t.casefold() for t in out):
            out.append(tag)
    return out


def backing_problem(stage: str, evidence_count: int, vouch: str | None) -> str | None:
    """Why this entry cannot be marked backed, or None when it can."""
    if stage == "discussed":
        return DISCUSSED
    if evidence_count < 1 and not (vouch or "").strip():
        return NEEDS_BACKING
    return None


def _over(key: str, value: str) -> str | None:
    if len(value) > CAPS[key]:
        return f"{FIELD_WORD[key]} is longer than {CAPS[key]:,} characters."
    return None


def check_fields(fields: dict, tags: list, evidence: list) -> str | None:
    """The first reason these values cannot be stored, or None."""
    if not fields.get("title"):
        return "Add a title."
    for key in TEXT_FIELDS:
        problem = _over(key, fields.get(key) or "")
        if problem:
            return problem
    if len(tags) > TAGS_MAX:
        return f"More than {TAGS_MAX} tags."
    for tag in tags:
        if len(tag) > TAG_CAP:
            return f"A tag is longer than {TAG_CAP} characters."
    if len(evidence) > EVIDENCE_MAX:
        return f"More than {EVIDENCE_MAX} evidence items."
    for ev in evidence:
        for key in ("name", "location", "shows"):
            problem = _over("ev_" + key, ev.get(key) or "")
            if problem:
                return problem
    return None


def _text(value) -> str:
    """One stored value: trimmed, line ends made plain, and None, Nobody named
    and empty all stored as empty."""
    text = "\n".join(line.strip() for line in str(value or "").replace("\r\n", "\n")
                     .replace("\r", "\n").split("\n")).strip()
    return "" if _is_nothing(text) else text


def _is_nothing(text: str) -> bool:
    return text.casefold().rstrip(".") in ("none", "nobody named")


def _clean_evidence(items) -> list[dict] | None:
    """Evidence from a request: a list of {name, location, shows, level}.
    Rows with nothing in them are dropped. None when the shape is wrong."""
    if not isinstance(items, list):
        return None
    out = []
    for item in items:
        if not isinstance(item, dict):
            return None
        ev = {k: _text(item.get(k) if isinstance(item.get(k), str) else "")
              for k in ("name", "location", "shows")}
        if not any(ev.values()):
            continue
        level = item.get("level")
        ev["level"] = level if level in LEVEL_WORD else "describe_only"
        out.append(ev)
    return out


# ------------------------------------------------------------------ reads

def _entry(row) -> dict:
    d = dict(row)
    d["tag_list"] = tag_list(d.get("tags"))
    return d


def list_entries(conn) -> list[dict]:
    """Every entry with its evidence count, newest change first."""
    if not has_tables(conn):
        return []
    rows = conn.execute(
        "SELECT e.*, (SELECT COUNT(*) FROM experience_evidence v WHERE v.entry_id = e.id)"
        " AS evidence_count FROM experience_entries e ORDER BY e.updated_at DESC, e.id DESC")
    return [_entry(r) for r in rows]


def get_entry(conn, entry_id: int) -> dict | None:
    """One entry with its evidence in the order it was added, or None."""
    if not has_tables(conn):
        return None
    row = conn.execute("SELECT * FROM experience_entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        return None
    entry = _entry(row)
    entry["evidence"] = [dict(r) for r in conn.execute(
        "SELECT id, name, location, shows, level FROM experience_evidence"
        " WHERE entry_id = ? ORDER BY id", (entry_id,))]
    entry["evidence_count"] = len(entry["evidence"])
    return entry


# ------------------------------------------------------------------ writes

def _insert(conn, fields: dict, tags: list, evidence: list, status: str, now: str) -> int:
    cur = conn.execute(
        "INSERT INTO experience_entries (title, did, result, period, place, tags, stage,"
        " vouch, questions, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (fields["title"], fields["did"], fields["result"], fields["period"], fields["place"],
         ", ".join(tags), fields["stage"], fields["vouch"], fields["questions"], status, now, now))
    entry_id = cur.lastrowid
    _put_evidence(conn, entry_id, evidence)
    return entry_id


def _put_evidence(conn, entry_id: int, evidence: list) -> None:
    conn.executemany(
        "INSERT INTO experience_evidence (entry_id, name, location, shows, level) VALUES (?,?,?,?,?)",
        [(entry_id, e["name"], e["location"], e["shows"], e["level"]) for e in evidence])


def save_entry(conn, body: dict, now: str) -> dict:
    """Create (no id) or replace (id) one entry and its evidence from the page's
    form, optionally moving its status in the same write. A new entry is
    always a draft. Refuses, changing nothing, when a field is over its cap,
    the title is empty, or the entry would be backed without what backing
    needs."""
    if not isinstance(body, dict):
        return {"ok": False, "invalid": True, "note": "send the entry as JSON"}
    entry_id = body.get("id")
    if entry_id is not None and (not isinstance(entry_id, int) or isinstance(entry_id, bool)):
        return {"ok": False, "invalid": True, "note": "id must be an integer"}
    fields = {k: _text(body.get(k)) if isinstance(body.get(k), (str, type(None))) else None
              for k in TEXT_FIELDS}
    if any(v is None for v in fields.values()):
        return {"ok": False, "invalid": True, "note": "every field must be text"}
    stage = body.get("stage")
    if stage not in STAGE_WORD:
        return {"ok": False, "invalid": True, "note": "stage must be delivered, drafted or discussed"}
    fields["stage"] = stage
    raw_tags = body.get("tags")
    if not isinstance(raw_tags, (str, list, type(None))):
        return {"ok": False, "invalid": True, "note": "tags must be text"}
    tags = tag_list(raw_tags)
    evidence = _clean_evidence(body.get("evidence") if body.get("evidence") is not None else [])
    if evidence is None:
        return {"ok": False, "invalid": True, "note": "evidence must be a list of items"}
    status = body.get("status")
    if status is not None and status not in dict(STATUSES):
        return {"ok": False, "invalid": True, "note": "status must be draft, backed or parked"}
    problem = check_fields(fields, tags, evidence)
    if problem:
        return {"ok": False, "invalid": True, "note": problem}
    if entry_id is None:
        return {"ok": True, "id": _insert(conn, fields, tags, evidence, "draft", now),
                "created": True, "status": "draft"}
    row = conn.execute("SELECT status FROM experience_entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        return {"ok": False, "gone": True, "note": "That entry is not on file any more."}
    new_status = status or row["status"]
    if new_status == "backed":
        why = backing_problem(stage, len(evidence), fields["vouch"])
        if why:
            return {"ok": False, "invalid": True, "note": why, "backing": why}
    conn.execute(
        "UPDATE experience_entries SET title = ?, did = ?, result = ?, period = ?, place = ?,"
        " tags = ?, stage = ?, vouch = ?, questions = ?, status = ?, updated_at = ? WHERE id = ?",
        (fields["title"], fields["did"], fields["result"], fields["period"], fields["place"],
         ", ".join(tags), stage, fields["vouch"], fields["questions"], new_status, now, entry_id))
    conn.execute("DELETE FROM experience_evidence WHERE entry_id = ?", (entry_id,))
    _put_evidence(conn, entry_id, evidence)
    return {"ok": True, "id": entry_id, "created": False, "status": new_status}


def delete_entry(conn, entry_id: int) -> dict:
    """The entry and all its evidence, in the caller's one transaction."""
    gone = conn.execute("DELETE FROM experience_entries WHERE id = ?", (entry_id,)).rowcount
    if not gone:
        return {"ok": False, "gone": True, "note": "That entry is not on file any more."}
    conn.execute("DELETE FROM experience_evidence WHERE entry_id = ?", (entry_id,))
    return {"ok": True, "id": entry_id}


# ------------------------------------------------------------------ the paste

_MARKER = re.compile(r"^\s*=+\s*entry\s*=+\s*$", re.I)
_NOT_INCLUDED = re.compile(r"^[\s=#*\-]*not\s+included[\s:=#*\-]*$", re.I)
_LABELS = (("title", r"title"), ("did", r"what\s+i\s+did"), ("result", r"result"),
           ("period", r"when"), ("place", r"where"), ("tags", r"tags"), ("stage", r"stage"),
           ("evidence", r"evidence"), ("vouch", r"vouch"), ("questions", r"questions"))
_LABEL = re.compile(r"^\s*(?:(" + ")|(".join(p for _k, p in _LABELS) + r"))\s*:\s?(.*)$", re.I)
_KEYS = tuple(k for k, _p in _LABELS)
# Fields that keep their line breaks; the rest are one line.
_LONG = ("did", "result", "questions")
_LEVEL_WORDS = {"can show": "can_show", "describe only": "describe_only", "public": "public"}


def _label(line: str):
    m = _LABEL.match(line)
    if not m:
        return None
    for i, key in enumerate(_KEYS):
        if m.group(i + 1) is not None:
            return key, m.group(len(_KEYS) + 1)
    return None


def _blocks(text: str) -> list[list[str]]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i, line in enumerate(lines):
        if _NOT_INCLUDED.match(line):
            lines = lines[:i]
            break
    blocks: list[list[str]] = [[]]
    marked = False
    for line in lines:
        if _MARKER.match(line):
            blocks.append([])
            marked = True
        else:
            blocks[-1].append(line)
    # Words before the first marker are a block only when they hold a label
    # (an entry pasted without its marker), or when there is no marker at all.
    if marked and not any(_label(line) for line in blocks[0]):
        blocks = blocks[1:]
    return [b for b in blocks if any(line.strip() for line in b)]


def _evidence_line(line: str) -> dict | None:
    line = re.sub(r"^\s*[-*•]\s+", "", line).strip()
    if not line or _is_nothing(line):
        return None
    parts = [p.strip() for p in line.split("|")]
    parts += [""] * (4 - len(parts))
    name, location, shows = parts[0], parts[1], parts[2]
    level = " ".join("|".join(parts[3:]).replace("_", " ").split()).casefold().rstrip(".")
    if not (name or location or shows):
        return None
    return {"name": name, "location": location, "shows": shows,
            "level": _LEVEL_WORDS.get(level, "describe_only")}


def read_block(lines: list[str]) -> tuple[dict | None, str | None]:
    """(the entry, None) or (None, why it could not be read) for one block."""
    got: dict = {}
    evidence: list = []
    current = None
    for line in lines:
        found = _label(line)
        if found:
            current, rest = found
            if current == "evidence":
                ev = _evidence_line(rest)
                if ev:
                    evidence.append(ev)
            else:
                got.setdefault(current, []).append(rest)
            continue
        if current == "evidence":
            ev = _evidence_line(line)
            if ev:
                evidence.append(ev)
        elif current is not None:
            got[current].append(line)
    fields = {}
    for key in TEXT_FIELDS + ("tags", "stage"):
        parts = [p.strip() for p in got.get(key, []) if p.strip()]
        fields[key] = _text(("\n" if key in _LONG else " ").join(parts))
    stage = fields.pop("stage").casefold()
    fields["stage"] = stage if stage in STAGE_WORD else "drafted"
    tags = tag_list(fields.pop("tags"))
    if not fields["title"]:
        return None, "No title."
    problem = check_fields(fields, tags, evidence)
    if problem:
        return None, problem
    return {"fields": fields, "tags": tags, "evidence": evidence}, None


def _first_line(lines: list[str]) -> str:
    line = next((x.strip() for x in lines if x.strip()), "")
    return line if len(line) <= 120 else line[:119].rstrip() + "…"


def parse_paste(text: str) -> tuple[list[dict], list[dict]]:
    """(the entries read, the blocks that could not be: {first_line, why})."""
    read, refused = [], []
    for block in _blocks(str(text or "")):
        entry, why = read_block(block)
        if entry is None:
            refused.append({"first_line": _first_line(block), "why": why})
        else:
            read.append(entry)
    return read, refused


def add_pasted(conn, text, now: str) -> dict:
    """Every block that reads, added as a draft, in the caller's one transaction."""
    if not isinstance(text, str):
        return {"ok": False, "invalid": True, "note": "text must be the paste as text"}
    if len(text) > PASTE_MAX:
        return {"ok": False, "invalid": True,
                "note": f"That paste is longer than {PASTE_MAX:,} characters."}
    read, refused = parse_paste(text)
    ids = [_insert(conn, e["fields"], e["tags"], e["evidence"], "draft", now) for e in read]
    return {"ok": True, "added": len(ids), "ids": ids, "refused": refused}


# ------------------------------------------------------------------ Prepare

def _tag_pattern(tag: str):
    words = tag.split()
    return re.compile(r"(?<![0-9A-Za-z])" + r"\s+".join(re.escape(w) for w in words)
                      + r"(?![0-9A-Za-z])", re.I)


def matched_tags(tags: list, text: str) -> list[str]:
    """The tags found in text as a whole word or phrase, whatever the case."""
    return [t for t in tags if t.split() and _tag_pattern(t).search(text)]


def matches_for_advert(conn, text: str) -> list[dict]:
    """Backed entries with at least one tag in the advert, most matching tags
    first, then the most recently changed. Drafts and parked entries never."""
    if not text or not text.strip():
        return []
    try:
        if not has_tables(conn):
            return []
        rows = conn.execute("SELECT id, title, tags, updated_at FROM experience_entries"
                            " WHERE status = 'backed' ORDER BY updated_at DESC, id DESC").fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for row in rows:
        hit = matched_tags(tag_list(row["tags"]), text)
        if hit:
            out.append({"id": row["id"], "title": row["title"], "matched": hit})
    out.sort(key=lambda r: -len(r["matched"]))   # stable, so newest first within a count
    return out
