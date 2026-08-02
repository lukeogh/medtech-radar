#!/usr/bin/env python
"""Write the announcement-day drafts when a signal clears the fast bar.

The system already says what to do the same day. This hands over the words,
so the job becomes editing rather than composing at the moment the phone
buzzes.

Rules that are not negotiable, and are tested:

- Nothing here sends, posts or connects. It writes two strings into the
  signal row. A human copies them or does not.
- Drafts are written once per signal. A second push of the same signal
  reuses what is stored rather than paying for it again.
- Only for the announcement-day step, the comment and the connection note.
  An artefact send or a week-three engagement gets no draft, because those
  are not first contact and the playbook words them differently.
- Every draft passes the doctrine check before it is stored. A pitch shape,
  a price, a named service or an ask is refused and nothing is stored,
  because a bad draft is worse than none, it will be sent.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import radar_common
import score_item

MAX_TOKENS = 800
NOTE_LIMIT = 300

# Shapes that mean selling. The playbook forbids an offer, a link or an ask
# in the first two touches, so these are failures rather than warnings.
FORBIDDEN = (
    "my service", "my services", "i can help", "happy to help with",
    "get in touch", "book a call", "let me know if you need",
    "gap assessment", "fixed-scope", "fixed scope", "day rate", "per day",
    "my rate", "consultancy", "consulting services", "engage me", "hire me",
    "proposal", "quote", "pricing", "package",
)
_PRICE = re.compile(r"[£$€]\s?\d|(\b\d{3,}\s?(gbp|eur|usd)\b)", re.I)


def wants_drafts(playbook_step: str) -> bool:
    """True only for the announcement-day comment and connection note."""
    text = (playbook_step or "").lower()
    if "comment" in text:
        return True
    return "connect" in text or "connection" in text


def doctrine_problems(comment: str, note: str) -> list:
    """Everything wrong with a pair of drafts. Empty list means send-worthy."""
    problems = []
    both = f"{comment}\n{note}".lower()
    for bad in FORBIDDEN:
        if bad in both:
            problems.append(f"contains {bad!r}, which reads as selling")
    if _PRICE.search(f"{comment}\n{note}"):
        problems.append("contains a price")
    if "http://" in both or "https://" in both:
        problems.append("contains a link, forbidden in the first two touches")
    if comment:
        low = comment.lower()
        for word in ("62304", "13485", "iso ", "iec ", "regulatory", "compliance"):
            if word in low:
                problems.append(f"the comment mentions {word.strip()!r}, "
                                "standards belong only in the note")
                break
        if "?" in comment:
            problems.append("the comment asks a question, which invites a reply about work")
    if note and len(note) > NOTE_LIMIT:
        problems.append(f"the note is {len(note)} characters, over the {NOTE_LIMIT} limit")
    return problems


def get_mock_fn():
    if not radar_common.mock_mode_active():
        return None
    test_dir = str(SCRIPT_DIR.parent / "test")
    if test_dir not in sys.path:
        sys.path.insert(0, test_dir)
    import mocks
    return getattr(mocks, "mock_drafter", None)


def generate_drafts(conn, url_hash: str, company: str, headline: str,
                    article_text: str, config: dict) -> dict:
    """Draft both, check them, store them. Never raises, never sends."""
    out = {"drafted": False, "note": None, "usage": {}, "source": None}
    try:
        row = conn.execute(
            "SELECT draft_comment, draft_note FROM signals WHERE url_hash = ?",
            (url_hash,)).fetchone()
        if row and (row["draft_comment"] or row["draft_note"]):
            out["note"] = "already drafted"
            return out

        source = "article" if (article_text or "").strip() else "headline"
        user = json.dumps({
            "company": company, "headline": headline,
            "article_text": (article_text or "")[:4000],
        }, ensure_ascii=False)
        text, usage = radar_common.claude_call(
            config.get("claude_model_score", "claude-sonnet-4-6"),
            score_item.load_prompt("drafter.md"), user,
            max_tokens=MAX_TOKENS, mock_fn=get_mock_fn())
        radar_common.add_usage(out["usage"], usage)
        try:
            parsed = radar_common.extract_json(text)
        except (ValueError, json.JSONDecodeError):
            out["note"] = "drafter returned unparseable output, nothing stored"
            return out

        comment = radar_common.sanitise_free_text(
            str(parsed.get("comment") or "").strip())
        note = radar_common.sanitise_free_text(
            str(parsed.get("connection_note") or "").strip())
        problems = doctrine_problems(comment, note)
        if problems:
            # Refuse rather than store. A draft that breaks the doctrine is
            # worse than no draft, because it is sitting there ready to send.
            out["note"] = "refused, " + "; ".join(problems[:2])
            return out
        if not comment and not note:
            out["note"] = "drafter returned nothing usable"
            return out

        conn.execute(
            "UPDATE signals SET draft_comment = ?, draft_note = ?,"
            " draft_source = ?, drafted_at = ? WHERE url_hash = ?",
            (comment or None, note or None, source, radar_common.now_iso(),
             url_hash))
        out.update({"drafted": True, "source": source})
        return out
    except Exception as err:                       # noqa: BLE001
        out["note"] = f"failed, {type(err).__name__}"
        return out
