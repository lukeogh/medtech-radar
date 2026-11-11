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
import engagement

REPO_ROOT = radar_common.REPO_ROOT
PROMPTS_DIR = radar_common.PROMPTS_DIR
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

# The CV pass mark and the Filtered floor are not constants here. They are
# two lines in the preferences file, read once per run by
# radar_common.read_score_thresholds and passed to the functions below. The
# parameter defaults are the same fallback that reader uses, 70 and 40.


def cv_gate(cv_match, cv_pass=radar_common.DEFAULT_CV_PASS_SCORE):
    """The CV gate. A CV match at or above the pass mark passes it.

    None when the scorer gave no number, because an unknown is not a fail.
    """
    return None if cv_match is None else cv_match >= cv_pass


def is_real(sector_gate, cv_match,
            cv_floor=radar_common.DEFAULT_CV_FLOOR_SCORE) -> bool:
    """Whether a role is real enough not to be filtered out unread.

    A role is real when it is software, quality or regulatory leadership in a
    regulated or deep-tech sector, or anything at all in medtech and IVD.
    The sector gate plus a CV number that reaches the Filtered floor is the
    closest honest proxy the rubric gives us without a fifth question.
    """
    return bool(sector_gate) or (cv_match is not None and cv_match >= cv_floor)


def derive_tier(gates: dict, cv_match=None, is_real_role: bool = True,
                cv_floor=radar_common.DEFAULT_CV_FLOOR_SCORE) -> dict:
    """Turn four gate results into a tier. Code decides this, never the model.

    The gates are not equal. Location and rate are movable, one conversation
    can shift a hybrid pattern or restate a recruiter's number. Sector and CV
    are fixed, no question turns a generic software house into an IVD company
    and the CV number is about Luke rather than them. One question away leans
    entirely on that split. A rate fail is a rate fail whatever its basis:
    a salary under the salary floor is as movable as a day rate under the
    day floor, so both land one question away. A CV match under cv_floor,
    the Filtered floor from the preferences file, is filtered before any gate
    is weighed, and the stored reason names the floor it was judged against.
    """
    failed = [name for name in ("sector", "cv", "location", "rate")
              if gates.get(name) is False]
    out = {"tier": "reading", "failed_gates": failed, "filter_reason": None}

    if cv_match is not None and cv_match < cv_floor:
        out.update(tier="filtered", filter_reason=f"cv match under {cv_floor:g}")
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
            out["tier"] = "question"
            return out
    return out


def rate_gate_note(pay: dict) -> str:
    """One plain line saying what the code judged and against which floor."""
    words = {"day-rate": "day rate", "salary": "salary",
             "hourly": "hourly rate, times eight"}
    verdict = "at or above" if pay["gate"] else "below"
    return (f"Stated {words.get(pay['basis'], pay['basis'])} £{pay['figure']:,.0f}"
            f" against the £{pay['floor']:,.0f} floor, {verdict}, judged in code.")


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


class ScorerSystem(list):
    """The scorer's system blocks, carrying the version of the rubric in them.

    A plain list to everything that sends it. The version is the hash of
    scorer.md as it was when the blocks were built, so a file edited while a
    run is in flight cannot put the wrong version on the scores that run
    makes.
    """
    prompt_version = None


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
    blocks = ScorerSystem(
        [{"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}])
    blocks.prompt_version = radar_common.prompt_hash(rubric)
    return blocks


INVALID_JSON_NOTE = "response was not valid JSON after one retry"
CUT_OFF_NOTE = "reply was cut off at max_tokens, so it was not used as a score"


def _advert_label(opp: dict) -> str:
    """Company and title of an advert, for a log line that has to name it."""
    return f"{opp.get('company') or 'an unnamed company'}, {opp.get('title') or 'an untitled role'}"


def _call_for_json(model: str, system, user_content: str, mock_fn,
                   refuse_cut_off: bool = False, label: str = "an advert"):
    """Call, parse, retry once with a firmer instruction, then give up.

    With refuse_cut_off, a reply the model did not finish, stop_reason
    max_tokens, is never parsed or used, even one that would parse, because
    a cut off reply cannot be trusted to be the whole answer. It is logged
    to stderr under the label and counts as a failed attempt like any other.
    Only a score asks for that. The extractor and the pay backfill parse a
    reply as they always have.

    Returns (parsed_dict_or_None, usage, note_or_None). The usage is the
    summed token counts plus the model and stop_reason of the last call,
    and the note says why the last attempt failed.
    """
    total: dict = {}
    cut_off = False
    for content in (user_content,
                    user_content + "\n\nReturn only the JSON object, nothing else."):
        text, usage = radar_common.claude_call(model, system, content,
                                               max_tokens=MAX_TOKENS,
                                               mock_fn=mock_fn)
        radar_common.add_usage(total, usage)
        total["model"] = usage.get("model")
        total["stop_reason"] = usage.get("stop_reason")
        cut_off = refuse_cut_off and radar_common.reply_was_cut_off(usage)
        if cut_off:
            print(f"WARNING. The reply from {model} for {label} was cut off at "
                  f"max_tokens ({MAX_TOKENS}), so it was not used.", file=sys.stderr)
            continue
        try:
            return radar_common.extract_json(text), total, None
        except (ValueError, json.JSONDecodeError):
            pass
    return None, total, CUT_OFF_NOTE if cut_off else INVALID_JSON_NOTE


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


def score_opportunity(opp: dict, system_blocks: list, config: dict, mock_fn=None,
                      floors: dict | None = None,
                      thresholds: dict | None = None,
                      basis: str = radar_common.SCORE_BASIS_CARD):
    """Score one opportunity. Returns (normalised_dict_or_None, usage, note).

    The normalised dict uses the database column names. combined is
    round((cv_match + want_match) / 2), computed here, never by the model.
    The rate gate is judged in code from the extractor's pay columns
    wherever a figure was stored, against the floor of its own basis; the
    model's gate_rate stands only when nothing usable was stated. The CV
    gate and the Filtered floor are judged in code against thresholds, the
    pass mark and the floor from the preferences file, read here when the
    caller has not passed them.

    The dict also carries how the score was made, which the writers store.
    That is the basis the scorer was shown (card, or spec for a full job
    spec), the version of the rubric, the model that answered and the time.
    The version comes from the system blocks when they carry one, so it is
    the rubric the model actually saw. None is returned for a score that
    failed, including a reply that was cut off at max_tokens.
    """
    user = "Score this opportunity.\n\n" + json.dumps(opp, ensure_ascii=False)
    parsed, usage, note = _call_for_json(config["claude_model_score"],
                                         system_blocks, user, mock_fn,
                                         refuse_cut_off=True,
                                         label=_advert_label(opp))
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

    thresholds = thresholds or radar_common.read_score_thresholds(config)
    gates = {"sector": gate("gate_sector"),
             "cv": cv_gate(cv, thresholds["pass"]),
             "location": gate("gate_location"),
             "rate": gate("gate_rate")}
    rate_basis = str(parsed.get("rate_basis") or "").strip()
    rate_value = _num(parsed.get("rate_value"))
    rate_stated = (None if parsed.get("rate_stated") is None
                   else int(bool(parsed.get("rate_stated"))))
    gate_rate_note = sanitise(str(parsed.get("gate_rate_note", "")).strip())
    # The rate gate is code's call wherever the extractor stored a figure.
    # Two floors, no conversion between them. The model's verdict stands
    # only for an advert that stated nothing usable, where it reads pass.
    floors = floors or radar_common.read_rate_floors(config)
    pay = radar_common.gate_pay(opp.get("pay_currency"), opp.get("pay_period"),
                                opp.get("pay_min"), opp.get("pay_max"),
                                floors, config)
    if pay["gate"] is not None:
        gates["rate"] = pay["gate"]
        rate_basis, rate_value, rate_stated = pay["basis"], pay["figure"], 1
        gate_rate_note = rate_gate_note(pay)
    real = is_real(gates["sector"], cv, thresholds["floor"])
    tier_info = derive_tier(gates, cv, real, thresholds["floor"])

    scored.update({
        # How this score was made. Set here in code, never by the model.
        "score_basis": basis,
        "prompt_version": (getattr(system_blocks, "prompt_version", None)
                           or radar_common.prompt_version()),
        "score_model": usage.get("model") or config.get("claude_model_score"),
        "scored_at": radar_common.now_iso(),
        "gate_sector": None if gates["sector"] is None else int(gates["sector"]),
        "gate_sector_note": sanitise(str(parsed.get("gate_sector_note", "")).strip()),
        "gate_cv": None if gates["cv"] is None else int(gates["cv"]),
        "gate_cv_note": sanitise(str(parsed.get("gate_cv_note", "")).strip()),
        "gate_location": None if gates["location"] is None else int(gates["location"]),
        "gate_location_note": sanitise(str(parsed.get("gate_location_note", "")).strip()),
        "gate_rate": None if gates["rate"] is None else int(gates["rate"]),
        "gate_rate_note": gate_rate_note,
        "tier": tier_info["tier"],
        "failed_gates": json.dumps(tier_info["failed_gates"]),
        "filter_reason": tier_info["filter_reason"],
        "question_text": (sanitise(str(parsed.get("question_text") or "").strip())
                          or question_for(gates, {})) if tier_info["tier"] == "question" else None,
        "rate_stated": rate_stated,
        "rate_value": rate_value,
        "rate_basis": rate_basis or None,
        "ir35": (str(parsed.get("ir35") or "").strip().lower() or None),
        "location_class": (str(parsed.get("location_class") or "").strip().lower() or None),
        # Off the advert, clamped to the three allowed words. The note is
        # the phrase that decided it, "fixed term" kept for a fixed term.
        "engagement_type": engagement.normalise(parsed.get("engagement_type")),
        "engagement_note": sanitise(str(parsed.get("engagement_note") or "").strip()) or None,
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
