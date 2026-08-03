#!/usr/bin/env python
"""Scoring core for MedTech Radar.

Shared by process_email.py, and by anything else that needs to extract or
score opportunities, so the logic lives in exactly one place. Responsibilities:

- Load the versioned prompts from prompts/.
- Load CV text (cv.txt, then cv.pdf via pypdf, then the test fixture with a
  loud warning) and the preferences file (live copy, then tracked template).
- Build the scorer system prompt as one stable cached block so batch scoring
  hits the prompt cache.
- Call Claude through radar_common.claude_call, demand strict JSON, retry once
  on a parse failure, then give up with a note.

Also runnable on its own for a spot check of one opportunity:
  python scripts/score_item.py --mock < opportunity.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import radar_common
import cv_store

REPO_ROOT = radar_common.REPO_ROOT
PROMPTS_DIR = REPO_ROOT / "prompts"
PROFILE_DIR = REPO_ROOT / "config" / "profile"

MAX_BODY_CHARS = 12000  # cost guardrail on extraction input
MAX_TOKENS = 1024       # per the build conventions, extraction and scoring


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


FIXTURE_REFUSAL = (
    "No usable CV in config/profile/ and live scoring against the test "
    "fixture is refused. A plausible score from the wrong document is worse "
    "than a loud failure. Put the CV at config/profile/cv.txt, or cv.pdf "
    "with pypdf installed. allow_fixture_profile true in config/radar.yaml "
    "overrides this for deliberate experiments only."
)


def _fixture_allowed(config: dict) -> bool:
    return (radar_common.mock_mode_active()
            or bool(config.get("allow_fixture_profile", False)))



# ---------------------------------------------------------------- gates

MOVABLE_GATES = ("location", "rate")   # one conversation can shift these
FIXED_GATES = ("sector", "cv")         # no question turns a software house into an IVD firm
CV_PASS = 70
CV_FLOOR_FOR_READING = 40


def derive_tier(gates: dict, rate_basis: str = "", cv_match=None,
                is_real_role: bool = True) -> dict:
    """Turn four gate results into a tier. Code decides this, never the model.

    The gates are not equal. Location and rate are movable, one conversation
    can shift a hybrid pattern or restate a recruiter's number. Sector and CV
    are fixed, no question turns a generic software house into an IVD company
    and the CV number is about Luke rather than them. One question away leans
    entirely on that split.
    """
    failed = [name for name in ("sector", "cv", "location", "rate")
              if gates.get(name) is False]
    out = {"tier": "reading", "failed_gates": failed, "filter_reason": None}

    if cv_match is not None and cv_match < CV_FLOOR_FOR_READING:
        out.update(tier="filtered", filter_reason="cv match under 40")
        return out
    if not is_real_role:
        out.update(tier="filtered", filter_reason="not a real leadership role")
        return out

    if not failed:
        # Nothing published blocks it. An unstated rate sits here carrying its
        # flag, because that is missing information rather than a blocker.
        out["tier"] = "top"
        return out

    if len(failed) == 1:
        only = failed[0]
        if only in MOVABLE_GATES:
            # A rate fail that came from a salary conversion does not belong
            # here. Permanent packages do not restate the way day rates do.
            if only == "rate" and rate_basis == "converted-salary":
                return out
            out["tier"] = "question"
            return out
    return out


def question_for(gates: dict, notes: dict) -> str:
    """The one question a conversation could settle, for the question tier."""
    if gates.get("location") is False:
        return "Would they go remote"
    if gates.get("rate") is False:
        return "Would they move on the rate"
    return ""


def assert_profile_ready(config: dict) -> None:
    """Fail before any API call when live scoring would use the fixture CV.

    A cv.pdf that exists but yields no text still gets caught by the same
    refusal inside load_cv_text, also before any API call.
    """
    if _fixture_allowed(config):
        return
    if cv_store.active_cv_name(PROFILE_DIR):
        return
    cv_txt = PROFILE_DIR / "cv.txt"
    cv_pdf = REPO_ROOT / config.get("cv_file", "config/profile/cv.pdf")
    if not cv_txt.exists() and not cv_pdf.exists():
        raise SystemExit(FIXTURE_REFUSAL)


def get_cv_version(config: dict) -> str:
    """The label of the CV a score would be made against, for stamping.

    Resolution order matches load_cv_text exactly. Uploaded versions via
    the active marker first, then the legacy cv.txt and cv.pdf, then the
    fixture, so the stamp on a row always names the document that shaped
    its score.
    """
    active = cv_store.active_cv_name(PROFILE_DIR)
    if active:
        return active
    if (PROFILE_DIR / "cv.txt").exists():
        return "cv.txt"
    cv_pdf = REPO_ROOT / config.get("cv_file", "config/profile/cv.pdf")
    if cv_pdf.exists():
        return cv_pdf.name
    return "fixture profile_snapshot.md"


def load_cv_text(config: dict) -> tuple[str, str]:
    """CV text plus a label saying where it came from.

    Order: the version the cv.active marker points at, then
    config/profile/cv.txt, then cv.pdf via pypdf if installed, then the
    test fixture. The fixture is for mock mode and deliberate experiments
    only. A live run that would fall through to it stops with a clear
    error instead, before any API call.
    """
    active = cv_store.active_cv_name(PROFILE_DIR)
    if active:
        return ((PROFILE_DIR / active).read_text(encoding="utf-8"),
                f"config/profile/{active}")

    cv_txt = PROFILE_DIR / "cv.txt"
    if cv_txt.exists():
        return cv_txt.read_text(encoding="utf-8"), "config/profile/cv.txt"

    cv_pdf = REPO_ROOT / config.get("cv_file", "config/profile/cv.pdf")
    if cv_pdf.exists():
        try:
            from pypdf import PdfReader
            text = "\n".join((page.extract_text() or "")
                             for page in PdfReader(str(cv_pdf)).pages)
            if text.strip():
                return text, config.get("cv_file", "config/profile/cv.pdf")
            print("WARNING. cv.pdf yielded no text. Provide config/profile/cv.txt instead.",
                  file=sys.stderr)
        except ImportError:
            print("WARNING. cv.pdf found but pypdf is not installed. "
                  "pip install pypdf or provide config/profile/cv.txt.", file=sys.stderr)

    if not _fixture_allowed(config):
        raise SystemExit(FIXTURE_REFUSAL)
    fixture = REPO_ROOT / "test" / "fixtures" / "profile_snapshot.md"
    print("WARNING. Using fixture profile, drop the real CV into config/profile/",
          file=sys.stderr)
    return fixture.read_text(encoding="utf-8"), "test/fixtures/profile_snapshot.md"


def load_prefs_text(config: dict) -> tuple[str, str]:
    """Preferences text plus a source label. Live copy first, template second."""
    prefs_rel = config.get("prefs_file", "config/profile/preferences.md")
    prefs = REPO_ROOT / prefs_rel
    if prefs.exists():
        return prefs.read_text(encoding="utf-8"), prefs_rel
    template = REPO_ROOT / "config" / "preferences.template.md"
    return template.read_text(encoding="utf-8"), "config/preferences.template.md"


def get_mock_fns():
    """(extract_fn, score_fn) in mock mode, (None, None) otherwise.

    The deterministic mocks live in test/mocks.py by convention. They are only
    imported when RADAR_MOCK is active, so production runs never touch test/.
    """
    if not radar_common.mock_mode_active():
        return None, None
    test_dir = str(REPO_ROOT / "test")
    if test_dir not in sys.path:
        sys.path.insert(0, test_dir)
    import mocks
    return mocks.mock_extractor, mocks.mock_scorer


def build_scorer_system(config: dict) -> list:
    """One stable system block, cache_control ephemeral, per the conventions."""
    rubric = load_prompt("scorer.md")
    cv_text, cv_src = load_cv_text(config)
    prefs_text, prefs_src = load_prefs_text(config)
    stable = (
        rubric
        + f"\n\n## CV TEXT. Capability evidence, source {cv_src}.\n\n" + cv_text
        + f"\n\n## PREFERENCES. Desire evidence, source {prefs_src}.\n\n" + prefs_text
    )
    return [{"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}]


def _call_for_json(model: str, system, user_content: str, mock_fn):
    """Call, parse, retry once with a firmer instruction, then give up.

    Returns (parsed_dict_or_None, usage, note_or_None).
    """
    total: dict = {}
    text, usage = radar_common.claude_call(model, system, user_content,
                                           max_tokens=MAX_TOKENS, mock_fn=mock_fn)
    radar_common.add_usage(total, usage)
    try:
        return radar_common.extract_json(text), total, None
    except (ValueError, json.JSONDecodeError):
        pass

    retry = user_content + "\n\nReturn only the JSON object, nothing else."
    text, usage = radar_common.claude_call(model, system, retry,
                                           max_tokens=MAX_TOKENS, mock_fn=mock_fn)
    radar_common.add_usage(total, usage)
    try:
        return radar_common.extract_json(text), total, None
    except (ValueError, json.JSONDecodeError):
        return None, total, "response was not valid JSON after one retry"


def extract_opportunities(email: dict, config: dict, mock_fn=None):
    """Split one alert email into opportunity dicts via the extractor prompt.

    Returns (opportunities, usage, note_or_None).
    """
    body = (email.get("body_text") or "").strip()
    if not body:
        body = radar_common.normalise_page_text(email.get("body_html") or "")
    payload = {
        "subject": email.get("subject", ""),
        "from": email.get("from", ""),
        "date": email.get("date", ""),
        "body_text": body[:MAX_BODY_CHARS],
    }
    user = ("Extract every job opportunity from this alert email.\n\n"
            + json.dumps(payload, ensure_ascii=False))
    parsed, usage, note = _call_for_json(config["claude_model_extract"],
                                         load_prompt("extractor.md"), user, mock_fn)
    if parsed is None:
        return [], usage, note
    opps = [o for o in parsed.get("opportunities", []) if isinstance(o, dict)]
    return opps, usage, None


def _pct(value):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, number))


def score_opportunity(opp: dict, system_blocks: list, config: dict, mock_fn=None):
    """Score one opportunity. Returns (normalised_dict_or_None, usage, note).

    The normalised dict uses the database column names. combined is
    round((cv_match + want_match) / 2), computed here, never by the model.
    """
    user = "Score this opportunity.\n\n" + json.dumps(opp, ensure_ascii=False)
    parsed, usage, note = _call_for_json(config["claude_model_score"],
                                         system_blocks, user, mock_fn)
    if parsed is None:
        return None, usage, note

    # Phase three. The gates are the score. cv_match_pct is still read for a
    # v1 response so an old fixture does not explode, but nothing new writes
    # combined, because the additive score is what guaranteed the bar could
    # never be cleared.
    cv = _pct(parsed.get("cv_match")) 
    if cv is None:
        cv = _pct(parsed.get("cv_match_pct"))
    want = _pct(parsed.get("want_match_pct"))
    combined = None

    red_flags = parsed.get("red_flags") or []
    if not isinstance(red_flags, list):
        red_flags = [str(red_flags)]
    thread_type = parsed.get("thread_type")
    if thread_type not in ("inbound", "signal"):
        thread_type = "inbound"

    sanitise = radar_common.sanitise_free_text
    scored = {
        "company": parsed.get("company") or opp.get("company", ""),
        "role_title": parsed.get("role_title") or opp.get("title", ""),
        "location": parsed.get("location") or opp.get("location", ""),
        "source_url": parsed.get("source_url") or opp.get("source_url", ""),
        "thread_type": thread_type,
        "cv_match": cv,
        "want_match": want,
        "combined": combined,
        "one_line_why": sanitise(str(parsed.get("one_line_why", "")).strip()),
        "red_flags": [sanitise(str(flag)) for flag in red_flags],
        "suggested_action": sanitise(str(parsed.get("suggested_action", "")).strip()),
        "act_by": sanitise(str(parsed.get("act_by", "")).strip()),
    }

    def gate(key, default=None):
        v = parsed.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.strip().lower() in ("true", "false"):
            return v.strip().lower() == "true"
        return None

    gates = {"sector": gate("gate_sector"),
             "cv": (cv >= CV_PASS) if cv is not None else None,
             "location": gate("gate_location"),
             "rate": gate("gate_rate")}
    rate_basis = str(parsed.get("rate_basis") or "").strip()
    # A role is real when it is software, quality or regulatory leadership in a
    # regulated or deep-tech sector, or anything at all in medtech and IVD. The
    # sector gate plus a non-trivial CV number is the closest honest proxy the
    # rubric gives us without a fifth question.
    is_real = bool(gates["sector"]) or (cv is not None and cv >= CV_FLOOR_FOR_READING)
    tier_info = derive_tier(gates, rate_basis, cv, is_real)

    scored.update({
        "gate_sector": None if gates["sector"] is None else int(gates["sector"]),
        "gate_sector_note": sanitise(str(parsed.get("gate_sector_note", "")).strip()),
        "gate_cv": None if gates["cv"] is None else int(gates["cv"]),
        "gate_cv_note": sanitise(str(parsed.get("gate_cv_note", "")).strip()),
        "gate_location": None if gates["location"] is None else int(gates["location"]),
        "gate_location_note": sanitise(str(parsed.get("gate_location_note", "")).strip()),
        "gate_rate": None if gates["rate"] is None else int(gates["rate"]),
        "gate_rate_note": sanitise(str(parsed.get("gate_rate_note", "")).strip()),
        "tier": tier_info["tier"],
        "failed_gates": json.dumps(tier_info["failed_gates"]),
        "filter_reason": tier_info["filter_reason"],
        "question_text": (sanitise(str(parsed.get("question_text") or "").strip())
                          or question_for(gates, {})) if tier_info["tier"] == "question" else None,
        "rate_stated": (None if parsed.get("rate_stated") is None
                        else int(bool(parsed.get("rate_stated")))),
        "rate_value": _num(parsed.get("rate_value")),
        "rate_basis": rate_basis or None,
        "ir35": (str(parsed.get("ir35") or "").strip().lower() or None),
        "location_class": (str(parsed.get("location_class") or "").strip().lower() or None),
    })
    if cv is None:
        return scored, usage, "scorer returned no usable cv match"
    return scored, usage, None


def _num(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Spot check. Score one opportunity JSON from stdin or --b64.")
    parser.add_argument("--b64", help="base64 encoded opportunity JSON")
    parser.add_argument("--mock", action="store_true", help="force mock mode")
    args = parser.parse_args(argv)

    if args.mock:
        os.environ["RADAR_MOCK"] = "1"
    radar_common.load_env()
    config = radar_common.load_config()

    raw = (base64.b64decode(args.b64).decode("utf-8-sig") if args.b64
           else sys.stdin.read())
    opp = json.loads(raw.lstrip("\ufeff"))
    _, score_fn = get_mock_fns()
    scored, usage, note = score_opportunity(opp, build_scorer_system(config),
                                            config, mock_fn=score_fn)
    print(json.dumps({"scored": scored, "usage": usage, "note": note}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
