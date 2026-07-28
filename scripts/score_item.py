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

REPO_ROOT = radar_common.REPO_ROOT
PROMPTS_DIR = REPO_ROOT / "prompts"

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


def assert_profile_ready(config: dict) -> None:
    """Fail before any API call when live scoring would use the fixture CV.

    A cv.pdf that exists but yields no text still gets caught by the same
    refusal inside load_cv_text, also before any API call.
    """
    if _fixture_allowed(config):
        return
    cv_txt = REPO_ROOT / "config" / "profile" / "cv.txt"
    cv_pdf = REPO_ROOT / config.get("cv_file", "config/profile/cv.pdf")
    if not cv_txt.exists() and not cv_pdf.exists():
        raise SystemExit(FIXTURE_REFUSAL)


def load_cv_text(config: dict) -> tuple[str, str]:
    """CV text plus a label saying where it came from.

    Order: config/profile/cv.txt, then cv.pdf via pypdf if installed, then
    the test fixture. The fixture is for mock mode and deliberate
    experiments only. A live run that would fall through to it stops with a
    clear error instead, before any API call.
    """
    cv_txt = REPO_ROOT / "config" / "profile" / "cv.txt"
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

    cv = _pct(parsed.get("cv_match_pct"))
    want = _pct(parsed.get("want_match_pct"))
    combined = round((cv + want) / 2) if cv is not None and want is not None else None

    red_flags = parsed.get("red_flags") or []
    if not isinstance(red_flags, list):
        red_flags = [str(red_flags)]
    thread_type = parsed.get("thread_type")
    if thread_type not in ("inbound", "signal"):
        thread_type = "inbound"

    scored = {
        "company": parsed.get("company") or opp.get("company", ""),
        "role_title": parsed.get("role_title") or opp.get("title", ""),
        "location": parsed.get("location") or opp.get("location", ""),
        "source_url": parsed.get("source_url") or opp.get("source_url", ""),
        "thread_type": thread_type,
        "cv_match": cv,
        "want_match": want,
        "combined": combined,
        "one_line_why": str(parsed.get("one_line_why", "")).strip(),
        "red_flags": [str(flag) for flag in red_flags],
        "suggested_action": str(parsed.get("suggested_action", "")).strip(),
        "act_by": str(parsed.get("act_by", "")).strip(),
    }
    if combined is None:
        return scored, usage, "scorer returned non numeric percentages"
    return scored, usage, None


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
