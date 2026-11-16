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
  on a parse failure, then give up with a note. A full description (Prepare
  application) is scored with one call and no retry, at its own ceiling, with
  the CV numbered so that the asks in the reply can cite and be checked.
- Hold the quote of each reason a reply gives to the text that was scored,
  the alert card or the full description, and keep only what is found there.

Also runnable on its own for a spot check of one opportunity:
  python scripts/score_item.py --mock < opportunity.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
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
# The ceiling of the extractor, per the build conventions, and of the pay
# backfill (backfill_pay.py), which passes no ceiling of its own to
# _call_for_json and so gets this one.
MAX_TOKENS = 1024

# A card's score has a ceiling of its own, because its reply now carries up to
# four reasons, each with a quote. Until then a card shared the extractor's
# 1,024. The worst card reply the prompt allows is the gates and their notes at
# the lengths the full description's count below gives them, 1,842 characters,
# and four reasons of 20 words, each with a quote of 25, in words as long as an
# advert's, 1,741 characters more. That is 3,583 characters, about 900 tokens at
# four characters a token and 1,194 at a pessimistic three, which is more than
# 1,024. At 2,048 the pessimistic count is under three quarters of the ceiling,
# the room the full description's ceiling keeps. What is billed is what the
# model writes, so the ceiling costs nothing until a reply reaches for it, and a
# reply that runs out of it is thrown away, so the margin is the cheap part. A
# typical card reply, 160 to 360 tokens before the reasons, is about 180 tokens
# longer with them. The extractor and the pay backfill keep 1,024.
# test/run_prepare_test.py builds the worst card reply and holds this ceiling
# to it.
CARD_MAX_TOKENS = 2048

# The full description call writes more than a card does, because it returns
# the per-ask working beside the gates and the reasons, and this is the ceiling
# for it. A reply that runs out of it is thrown away after being paid for, so
# the margin is cheap, and the ceiling costs nothing until a reply reaches for
# it, because what is billed is what the model writes. The worst reply the
# prompt allows is 25 asks of 20 words each, up to four CV lines apiece, beside
# the gates, the four reasons and the pay quote. That is 9,961 characters, about
# 2,490 tokens at four characters a token and 3,320 at three, which is hard on
# JSON and on wording as long as an advert's. Before the reasons it was 8,220
# characters and 2,740 tokens at three, which 4,096 held under three quarters of
# itself; with them the pessimistic count is 81 per cent of 4,096, so the
# ceiling was 6,144, where it was 54 per cent. Each ask now also carries the
# advert sentence it came from (decisions of 15 Nov), up to 25 words, which
# makes the worst reply 15,961 characters, 5,320 tokens at three, 87 per cent
# of 6,144, so the ceiling was 8,192, where it was 65 per cent. Each partly or
# missing ask now also carries a short reason of its own (why, up to
# ASK_WHY_WORDS words, 16 Nov), which makes the worst reply, every ask partly,
# about 20,700 characters, 6,900 tokens at three, 84 per cent of 8,192, so the
# ceiling is 10,240, where it is 67 per cent. There is no tokenizer here to
# count it exactly, so the ceiling is set well over the pessimistic count. A
# typical advert has about twelve asks, which is about 540 tokens beyond a
# card's reply, about 400 more with their sentences, and about 150 more with
# the reasons of the five or so that are partly or missing.
# test/run_prepare_test.py builds the worst reply and holds the ceiling to it,
# so raising the caps without raising this fails a check.
SPEC_MAX_TOKENS = 10240
SPEC_ATTEMPTS = 1       # one model call per click, no retry on a bad reply
MAX_ASKS = 25           # the most asks kept from one advert
ASK_TEXT_MAX = 240      # characters kept of one ask
CV_LINES_PER_ASK = 4    # the most CV lines kept as evidence for one ask
CV_LINE_TEXT_MAX = 240  # characters kept of a cited CV line
ASK_STATUSES = ("met", "partly", "missing")
ASK_WHY_WORDS = 20      # the most words kept of a partly or missing ask's reason
ASK_WHY_DROPS = ("empty", "not_text", "too_long", "quoted")

# The reasons a score gives, on either path, and the quote each rests on.
MAX_REASONS = 4         # the most reasons kept from one reply
REASON_TEXT_MAX = 240   # characters kept of one reason
QUOTE_WORDS_MAX = 25    # a quote of more words than this is not kept
# The fields of an alert card a quote on the card path is held to, as the
# scorer was given them. Each is searched on its own, so a quote cannot run
# from the end of one field into the start of the next.
CARD_QUOTE_FIELDS = ("title", "company", "location", "salary_rate")
# Why a quote was not kept, recorded on its reason as quote_dropped: empty is
# a quote missing, blank or with no letter or digit in it, not_text one that
# is not a string, too_long one of more than QUOTE_WORDS_MAX words, and
# not_found one that is not in the text word for word.
QUOTE_DROPS = ("empty", "not_text", "too_long", "not_found")

# The line in prompts/scorer.md under which the full description's part of the
# rubric begins. A card is scored with the file above it, exactly as it always
# was, and a full description with the whole file.
SPEC_MARKER = "<!-- full description."


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

MOVABLE_GATES = radar_common.MOVABLE_GATES   # one conversation can shift these
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
    """Turn the four gate states into a tier. Code decides this, never the model,
    so the same four states always give the same tier.

    Each gate is True (passes), False (fails), radar_common.NOT_STATED (the
    advert gives nothing to judge, neither a pass nor a fail) or None (not
    judged). Only Rate can be not stated today.

    - Top prospects: Sector, CV and Location pass, and Rate passes or is not
      stated.
    - One question away: Sector and CV pass, and exactly one of Location and
      Rate fails. Location and Rate are movable, one conversation can shift
      them. Sector and CV are fixed, no question turns a software house into
      an IVD firm. A rate under its floor is one question away whatever its
      basis.
    - Reading: everything else, Sector or CV failing, Location and Rate both
      failing, or a gate the tier needs that was never judged.

    A CV match under cv_floor, the Filtered floor from the preferences file,
    is filtered before any gate is weighed, and the stored reason names the
    floor it was judged against.
    """
    failed = [name for name in radar_common.GATE_NAMES
              if gates.get(name) is False]
    out = {"tier": "reading", "failed_gates": failed, "filter_reason": None}

    if cv_match is not None and cv_match < cv_floor:
        out.update(tier="filtered", filter_reason=f"cv match under {cv_floor:g}")
        return out
    if not is_real_role:
        out.update(tier="filtered", filter_reason="not a real leadership role")
        return out

    def passes(name):
        return gates.get(name) is True

    if not (passes("sector") and passes("cv")):
        return out
    movable_failed = [name for name in MOVABLE_GATES if gates.get(name) is False]
    if (not movable_failed and passes("location")
            and (passes("rate") or gates.get("rate") == radar_common.NOT_STATED)):
        out["tier"] = "top"
    elif len(movable_failed) == 1:
        out["tier"] = "question"
    return out


RATE_NOT_STATED_NOTE = radar_common.RATE_NOT_STATED_NOTE


def rate_gate_note(pay: dict) -> str:
    """One plain line saying what the code judged and against which floor."""
    if pay.get("gate") is None:
        return RATE_NOT_STATED_NOTE
    words = {"day-rate": "day rate", "salary": "salary",
             "hourly": "hourly rate, times eight"}
    verdict = "at or above" if pay["gate"] else "below"
    return (f"Stated {words.get(pay['basis'], pay['basis'])} £{pay['figure']:,.0f}"
            f" against the £{pay['floor']:,.0f} floor, {verdict}, judged in code.")


def rate_gate(pay: dict):
    """The Rate gate for derive_tier from gate_pay's verdict: True or False
    for a stated figure, NOT_STATED for none."""
    return radar_common.NOT_STATED if pay.get("gate") is None else bool(pay["gate"])


def question_for(gates: dict, notes: dict | None = None) -> str:
    """The one question a conversation could settle, for the question tier.
    It asks only about the movable gate that fails (radar_common.question_for)."""
    return radar_common.question_for(gates)


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
    makes. Blocks built for a full description also carry the lines of the CV
    they numbered and the label of the CV version those lines belong to, so
    that the citations in a reply can be checked against the very text the
    model was shown.
    """
    prompt_version = None
    basis = radar_common.SCORE_BASIS_CARD
    cv_lines = None
    cv_version = None
    cv_hash = None


def split_rubric(text: str) -> tuple[str, str]:
    """The part of scorer.md a card is scored with, and the part a full
    description adds. The second is empty when the file has no marker."""
    card, marker, spec = text.partition(SPEC_MARKER)
    return card, (marker + spec if marker else "")


def build_scorer_system(config: dict,
                        basis: str = radar_common.SCORE_BASIS_CARD,
                        cv: dict | None = None) -> list:
    """One stable system block, cache_control ephemeral, per the conventions.

    The default is the block a card is scored with, and it is the same bytes
    as before the full description existed: the rubric above the marker, the
    CV as it is, the preferences. For a full description (basis spec) the
    block is the whole of scorer.md and the CV is numbered line by line, so
    the model can cite a line and the reply can be checked. The version is
    the hash of the whole file either way, because the version of a prompt is
    the file.

    cv is a CV to score in place of the master, {"text", "label",
    "version"}, which is how a role's tailored copy is scored: the same
    rubric, the same preferences, the same numbering and the same block, with
    the copy's text where the master's would be. Nothing in the prompt
    changes for it, and the master is not read.
    """
    whole = load_prompt("scorer.md")
    spec = basis == radar_common.SCORE_BASIS_SPEC
    rubric = whole if spec else split_rubric(whole)[0]
    if cv is None:
        cv_text, cv_src = load_cv_text(config)
    else:
        cv_text, cv_src = cv["text"], cv["label"]
    prefs_text, prefs_src = load_prefs_text(config)
    if spec:
        cv_block = (f"\n\n## CV TEXT. Capability evidence, source {cv_src}. "
                    "Every line is numbered in square brackets.\n\n"
                    + cv_store.numbered_cv(cv_text))
    else:
        cv_block = (f"\n\n## CV TEXT. Capability evidence, source {cv_src}."
                    "\n\n" + cv_text)
    stable = (
        rubric + cv_block
        + f"\n\n## PREFERENCES. Desire evidence, source {prefs_src}.\n\n" + prefs_text
    )
    blocks = ScorerSystem(
        [{"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}])
    blocks.prompt_version = radar_common.prompt_hash(whole)
    blocks.basis = basis
    if spec:
        blocks.cv_lines = cv_store.cv_lines(cv_text)
        blocks.cv_version = (get_cv_version(config) if cv is None
                             else cv["version"])
        blocks.cv_hash = cv_store.cv_hash(cv_text)
    return blocks


INVALID_JSON_NOTE = "response was not valid JSON after one retry"
CUT_OFF_NOTE = "reply was cut off at max_tokens, so it was not used as a score"


def _advert_label(opp: dict) -> str:
    """Company and title of an advert, for a log line that has to name it."""
    return f"{opp.get('company') or 'an unnamed company'}, {opp.get('title') or 'an untitled role'}"


def _call_for_json(model: str, system, user_content: str, mock_fn,
                   refuse_cut_off: bool = False, label: str = "an advert",
                   max_tokens: int | None = None, attempts: int = 2):
    """Call, parse, retry once with a firmer instruction, then give up.

    With refuse_cut_off, a reply the model did not finish, stop_reason
    max_tokens, is never parsed or used, even one that would parse, because
    a cut off reply cannot be trusted to be the whole answer. It is logged
    to stderr under the label and counts as a failed attempt like any other.
    Only a score asks for that. The extractor and the pay backfill parse a
    reply as they always have.

    max_tokens defaults to MAX_TOKENS. attempts is how many calls a bad reply
    may use before the call gives up, two unless the caller will pay for one
    and no more, as a full description's score does.

    Returns (parsed_dict_or_None, usage, note_or_None). The usage is the
    summed token counts plus the model and stop_reason of the last call,
    and the note says why the last attempt failed.
    """
    ceiling = max_tokens or MAX_TOKENS
    total: dict = {}
    cut_off = False
    for content in (user_content,
                    user_content + "\n\nReturn only the JSON object, nothing else."
                    )[:max(1, attempts)]:
        text, usage = radar_common.claude_call(model, system, content,
                                               max_tokens=ceiling,
                                               mock_fn=mock_fn)
        radar_common.add_usage(total, usage)
        total["model"] = usage.get("model")
        total["stop_reason"] = usage.get("stop_reason")
        cut_off = refuse_cut_off and radar_common.reply_was_cut_off(usage)
        if cut_off:
            print(f"WARNING. The reply from {model} for {label} was cut off at "
                  f"max_tokens ({ceiling}), so it was not used.", file=sys.stderr)
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
    # OverflowError is what int() says of an infinity, and of an integer too
    # long to be a float, which a reply can be made of and a number cannot.
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0, min(100, number))


def _line_numbers(value) -> list[int]:
    """The CV line numbers a reply cites, as whole numbers and nothing else.

    A list of numbers is what the prompt asks for. A lone number, a number
    written as text, or a float that is a whole number is read as the number
    it plainly means. A bool is not a number here, and neither is anything
    that is not whole. Order is kept, because the prompt asks for the most
    direct line first.
    """
    if isinstance(value, (int, float, str)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:24]:
        if isinstance(item, bool):
            continue
        if isinstance(item, str):
            # Plain ASCII digits and no more than six of them. str.isdigit is
            # also true of a superscript two, which int() cannot read, and
            # int() will not read a very long run of digits either, so a reply
            # that has one is not allowed to raise after it has been paid for.
            digits = item.strip()
            if digits.isascii() and digits.isdigit() and len(digits) <= 6:
                item = int(digits)
        elif isinstance(item, float) and item.is_integer():
            item = int(item)
        if isinstance(item, int):
            out.append(item)
    return out


def normalise_asks(raw, cv_lines: list[str],
                   sources: list[str] | None = None) -> tuple[list[dict], int]:
    """The asks of a full description as they are kept, and how many were not.

    Each entry the model gave is checked, in code. The ask is its words, run
    through the voice rules like every free-text field. The status is met,
    partly or missing and nothing else, an entry with any other word is
    dropped and counted. For met and partly, each CV line cited must exist in
    the CV the model was shown and have text on it, and the ones that do are
    kept with their text as it stood, so a page can show the evidence and a
    later page can find it again. A met or partly with no valid line is kept
    and recorded as unanchored (anchored False), which is the warning the
    working exists to give: the model said the CV evidences it and the CV
    cannot be pointed at. A missing ask cites nothing, whatever the reply
    said, and has no anchor to lack (anchored None). At most MAX_ASKS are kept.

    Each ask also carries the advert sentence it came from (sentence), held
    to sources, the text that was scored (quote_sources), by the very rule a
    reason's quote is held to (_hold_quote, radar_common.find_verbatim with
    whole words): kept only where it is found there word for word, as that
    text's own characters, and otherwise None with sentence_dropped saying
    why (QUOTE_DROPS). The ask is kept either way, and a page shows it
    without a quoted sentence. With no sources nothing is kept, so a caller
    that cannot say what was scored keeps no sentence at all.

    A partly or missing ask also carries the scorer's short reason for it
    (why, at most ASK_WHY_WORDS words, its own words and not a quote, see
    _ask_why), or None with why_dropped saying why; a met ask carries none.
    Returns (asks, dropped).
    """
    if not isinstance(raw, list):
        return [], 0
    asks: list[dict] = []
    dropped = 0
    sanitise = radar_common.sanitise_free_text
    for item in raw:
        text = ""
        status = ""
        if isinstance(item, dict):
            text = sanitise(str(item.get("ask") or "").strip())
            status = str(item.get("status") or "").strip().lower()
        if not text or status not in ASK_STATUSES or len(asks) >= MAX_ASKS:
            dropped += 1
            continue
        lines: list[dict] = []
        if status != "missing":
            seen = set()
            for n in _line_numbers(item.get("cv_lines")):
                if n in seen or not 1 <= n <= len(cv_lines):
                    continue
                seen.add(n)
                line = cv_lines[n - 1].strip()
                if not line:
                    continue
                lines.append({"n": n, "text": line[:CV_LINE_TEXT_MAX]})
                if len(lines) >= CV_LINES_PER_ASK:
                    break
        kept, why = (_hold_quote(item.get("sentence"), sources)
                     if sources is not None else (None, "empty"))
        reason, reason_dropped = ((None, None) if status == "met"
                                  else _ask_why(item.get("why"), sources))
        asks.append({"ask": text[:ASK_TEXT_MAX], "status": status,
                     "lines": lines,
                     "anchored": None if status == "missing" else bool(lines),
                     "sentence": kept, "sentence_dropped": why,
                     "why": reason, "why_dropped": reason_dropped})
    return asks, dropped


def _ask_why(value, sources: list[str] | None) -> tuple[str | None, str | None]:
    """(the short reason a partly or missing ask carries, None), or (None,
    why it was not kept, ASK_WHY_DROPS).

    The reason is the scorer's own words, run through the voice rules like
    every free-text field, with any quotation marks round it taken off. One
    of more than ASK_WHY_WORDS words is not kept, and neither is one that is
    found whole, word for word, in the text that was scored, because that is
    a quote of the advert and not a reason (_hold_quote, the rule the
    sentences and the reasons' quotes are held to).
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "empty"
    if not isinstance(value, str):
        return None, "not_text"
    text = radar_common.sanitise_free_text(value.strip()).strip()
    text = text.strip("\"'“”‘’ ").strip()
    if not re.search(r"\w", text):
        return None, "empty"
    if len(text.split()) > ASK_WHY_WORDS:
        return None, "too_long"
    # A quote of the description is found by the very rule its sentence and
    # the reasons' quotes are held to, and is not a reason.
    if sources and _hold_quote(text, sources)[0] is not None:
        return None, "quoted"
    if not re.search(r"[.?]$", text):
        text += "."
    return text, None


def quote_sources(opp: dict, basis: str = radar_common.SCORE_BASIS_CARD) -> list[str]:
    """The text the quotes of a reply are held to, as the scorer was given it.

    For a full description, the description, which is the stored
    description_text the score is made on. For a card, the alert card's own
    words, title, company, location and pay text, each field on its own, as
    the scorer was given them. A re-score sends the row's own four fields, so
    they are what it stores. The inbox stores the title, company and location
    as the scorer's reply named them, so it holds the quotes kept here to the
    words it stores as well (hold_reasons).
    """
    if basis == radar_common.SCORE_BASIS_SPEC:
        return [str(opp.get("description") or "")]
    return [str(opp.get(field) or "") for field in CARD_QUOTE_FIELDS]


def _hold_quote(quote, sources: list[str]) -> tuple[str | None, str | None]:
    """(the text's own words for a quote, None), or (None, why it was dropped).

    The quote is found in one of the sources by radar_common.find_verbatim,
    the rule the pay line of a full description is held to, with whole words
    asked for as well: a quote that starts or ends part way through a word or
    a figure of the text is not found. What is kept is that source's
    characters. A quote with no letter or digit in it is dropped as empty. It
    is checked for length before it is looked for, as the pay line is, so a
    long quote is never searched for.
    """
    if quote is None or (isinstance(quote, str) and not quote.strip()):
        return None, "empty"
    if not isinstance(quote, str):
        return None, "not_text"
    flat = radar_common.flat_text(quote)
    if not re.search(r"\w", flat):
        return None, "empty"
    if len(flat.split(" ")) > QUOTE_WORDS_MAX:
        return None, "too_long"
    for source in sources:
        found = radar_common.find_verbatim(quote, source, whole_words=True)
        if found:
            return found, None
    return None, "not_found"


def normalise_reasons(raw, sources: list[str]) -> tuple[list[dict], int]:
    """The reasons of a reply as they are kept, and how many were not.

    A reason is its words, run through the voice rules like every free-text
    field and cut at REASON_TEXT_MAX characters, with the quote the model gave
    for it held to the text that was scored (quote_sources): kept only where it
    is found there word for word, with case and runs of white space ignored and
    its first and last words whole words of the text, and kept as that text's
    own characters. A quote that is empty, is not text, has more than
    QUOTE_WORDS_MAX words or is not found is dropped, its reason is kept, and
    the reason's quote_dropped says which of those it was (QUOTE_DROPS). A
    reason given as a bare string is kept, with no quote.
    Anything else in the list, an entry with no words for its reason among it,
    is left out and counted, and so is every reason past the first
    MAX_REASONS, and so is a reasons field that is there and is not a list.
    No reasons at all is no reasons, and not a failed score. Returns
    (reasons, dropped).
    """
    if raw is None:
        return [], 0
    if not isinstance(raw, list):
        return [], 1
    reasons: list[dict] = []
    # A list longer than any reply should hold is read no further than this.
    dropped = max(0, len(raw) - 24)
    sanitise = radar_common.sanitise_free_text
    for item in raw[:24]:
        if isinstance(item, str):
            text, quote = item, None
        elif isinstance(item, dict) and isinstance(item.get("reason"), str):
            text, quote = item["reason"], item.get("quote")
        else:
            dropped += 1
            continue
        text = (sanitise(text.strip()) or "")[:REASON_TEXT_MAX].strip()
        if not text or len(reasons) >= MAX_REASONS:
            dropped += 1
            continue
        kept, why = _hold_quote(quote, sources)
        reasons.append({"reason": text, "quote": kept, "quote_dropped": why})
    return reasons, dropped


def reasons_json(raw, sources: list[str], basis: str, scored_at: str,
                 gates: dict | None = None) -> str:
    """The reasons of a reply, checked, as the JSON stored beside its score.

    {"v", "scored_at", "basis", "reasons", "dropped"}, and "gates" when given.
    scored_at is the date of the score they belong to, which a reader checks
    before it shows them (radar_common.reasons_record). gates are the four
    gates of that score as the row stores them, sector, cv, location and rate,
    so that a page can tell when code has judged them again since and say so
    (radar_common.reasons_gates_moved). A reasons field that cannot be read
    for any reason at all costs the score nothing: the score keeps no reasons
    and the record counts one dropped.
    """
    try:
        reasons, dropped = normalise_reasons(raw, sources)
    except Exception:  # noqa: BLE001  the reasons never cost a score
        reasons, dropped = [], 1
    record = {"v": radar_common.REASONS_VERSION, "scored_at": scored_at,
              "basis": basis, "reasons": reasons, "dropped": dropped}
    if gates is not None:
        record["gates"] = gates
    return json.dumps(record, ensure_ascii=False)


def hold_reasons(raw, sources: list[str]) -> str | None:
    """Stored reasons, with every quote they kept held to sources as well.

    For a writer that stores other words than the scorer was given, as the
    inbox stores the title, company and location as the reply named them.
    Each quote kept from the card as it was sent stays only where sources,
    the words the row stores, hold it too, by the same rule, and where they
    do not it is dropped as not_found and its reason is kept. A quote that
    stays keeps the characters of the card it was found in, which are the
    advert's. raw is the JSON reasons_json made, and None stays None. Never
    raises: a record that cannot be read comes back as None, no reasons,
    rather than as reasons whose quotes were not checked.
    """
    if raw is None:
        return None
    try:
        record = json.loads(raw)
        for item in record["reasons"]:
            if item.get("quote") is not None:
                found, why = _hold_quote(item["quote"], sources)
                if found is None:
                    item["quote"], item["quote_dropped"] = None, why
        return json.dumps(record, ensure_ascii=False)
    except Exception:  # noqa: BLE001  the reasons never cost a score
        return None


def score_opportunity(opp: dict, system_blocks: list, config: dict, mock_fn=None,
                      floors: dict | None = None,
                      thresholds: dict | None = None,
                      basis: str = radar_common.SCORE_BASIS_CARD):
    """Score one opportunity. Returns (normalised_dict_or_None, usage, note).

    The normalised dict uses the database column names. combined is
    round((cv_match + want_match) / 2), computed here, never by the model.
    The rate gate is judged in code from the extractor's pay columns, a
    stated figure against the floor of its own basis, and no figure on file
    is not stated (gate_rate NULL, rate_stated 0). The model gives no rate
    verdict and no question; the question is code's. The CV
    gate and the Filtered floor are judged in code against thresholds, the
    pass mark and the floor from the preferences file, read here when the
    caller has not passed them.

    The dict also carries how the score was made, which the writers store.
    That is the basis the scorer was shown (card, or spec for a full job
    spec), the version of the rubric, the model that answered and the time.
    The version comes from the system blocks when they carry one, so it is
    the rubric the model actually saw. None is returned for a score that
    failed, including a reply that was cut off at max_tokens.

    It also carries the reasons the reply gave, as score_reasons, the JSON a
    writer stores beside the score: at most four, each quote held to the text
    that was scored (quote_sources), the card's fields for a card and the
    description for a full description, and dropped, with the drop recorded,
    where it is not found there word for word. Reasons that cannot be read
    never fail a score. A card is scored at CARD_MAX_TOKENS.

    A full description (basis spec) is one model call at SPEC_MAX_TOKENS and
    no retry, so the caller pays for one reply and no more. The reply carries
    the asks as well, and the dict then also carries them checked against the
    CV lines in the system blocks (asks), the per-ask working ready to store
    (working), the version of the description it was made against
    (description_hash) and the pay quote the model gave, untouched
    (rate_quote_raw), for the caller to hold to the description. A spec reply
    without a list of asks is not a score, and neither is one that cannot be
    read for any other reason. Both come back as no score, a note and the
    usage, because the reply has been paid for.
    """
    spec = basis == radar_common.SCORE_BASIS_SPEC
    user = "Score this opportunity.\n\n" + json.dumps(opp, ensure_ascii=False)
    parsed, usage, note = _call_for_json(
        config["claude_model_score"], system_blocks, user, mock_fn,
        refuse_cut_off=True, label=_advert_label(opp),
        max_tokens=SPEC_MAX_TOKENS if spec else CARD_MAX_TOKENS,
        attempts=SPEC_ATTEMPTS if spec else 2)
    if parsed is None:
        return None, usage, note
    if spec and not isinstance(parsed.get("asks"), list):
        return None, usage, "the reply had no list of asks, so it was not used"
    if not spec:
        return _finish_score(parsed, usage, opp, system_blocks, config, floors,
                             thresholds, basis)
    # A full description's reply has been paid for by now. Whatever in it
    # cannot be read is a failed score with a note and its usage, never an
    # exception that loses the usage on the way out.
    try:
        return _finish_score(parsed, usage, opp, system_blocks, config, floors,
                             thresholds, basis)
    except Exception as err:  # noqa: BLE001  see above
        return None, usage, ("the reply could not be read "
                             f"({type(err).__name__})")


def _finish_score(parsed: dict, usage: dict, opp: dict, system_blocks,
                  config: dict, floors, thresholds, basis: str):
    """What score_opportunity does once the reply has been parsed.

    Kept apart so that a full description's reply, which has been paid for by
    the time this runs, can be guarded as a whole. See score_opportunity.
    """
    spec = basis == radar_common.SCORE_BASIS_SPEC

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
    # The rate gate is code's alone, from the extractor's pay columns. Two
    # floors, no conversion between them. An advert with no figure on file is
    # not stated, neither a pass nor a fail, whatever the reply says about
    # pay: the scorer is not asked for a rate verdict and one it gives anyway
    # is not read.
    floors = floors or radar_common.read_rate_floors(config)
    pay = radar_common.gate_pay(opp.get("pay_currency"), opp.get("pay_period"),
                                opp.get("pay_min"), opp.get("pay_max"),
                                floors, config)
    gates = {"sector": gate("gate_sector"),
             "cv": cv_gate(cv, thresholds["pass"]),
             "location": gate("gate_location"),
             "rate": rate_gate(pay)}
    rate_basis, rate_value = pay["basis"], pay["figure"]
    rate_stated = 0 if pay["gate"] is None else 1
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
        "gate_rate": radar_common.gate_column(gates["rate"]),
        "gate_rate_note": gate_rate_note,
        "tier": tier_info["tier"],
        "failed_gates": json.dumps(tier_info["failed_gates"]),
        "filter_reason": tier_info["filter_reason"],
        # Code's question, about the one gate that fails. The scorer is not
        # asked for one, because it cannot know whether the rate is stated.
        "question_text": (question_for(gates)
                          if tier_info["tier"] == "question" else None),
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
    # The reasons, each quote held to the text that was scored, and stored
    # with the date of this score so that a reader can tell they belong to it,
    # and with its gates so that a page can tell when code has judged those
    # again since.
    scored["score_reasons"] = reasons_json(
        parsed.get("reasons"), quote_sources(opp, basis), basis,
        scored["scored_at"],
        gates={name: scored[f"gate_{name}"] for name in radar_common.GATE_NAMES})
    if spec:
        # Whatever in the asks cannot be read is caught by score_opportunity,
        # which guards all of this as one, so it is not caught again here.
        cv_lines = list(getattr(system_blocks, "cv_lines", None) or [])
        asks, dropped = normalise_asks(parsed.get("asks"), cv_lines,
                                       quote_sources(opp, basis))
        counts = {status: sum(1 for a in asks if a["status"] == status)
                  for status in ASK_STATUSES}
        counts["unanchored"] = sum(1 for a in asks if a["anchored"] is False)
        counts["dropped"] = dropped
        description_hash = radar_common.description_version(
            opp.get("description"))
        scored.update({
            "asks": asks,
            "description_hash": description_hash,
            # Exactly what the model wrote, never run through the voice
            # rules, because the caller holds it to the description word
            # for word.
            "rate_quote_raw": str(parsed.get("rate_quote") or "").strip(),
            # What belongs beside the score it was made with. scored_at is
            # in it so that a reader can tell the working still belongs to
            # the score the row holds, and the pay quote is filled in by
            # the caller once it has held the model's quote to the
            # description.
            "working": {
                "v": 1, "basis": basis, "scored_at": scored["scored_at"],
                "prompt_version": scored["prompt_version"],
                "cv_version": getattr(system_blocks, "cv_version", None),
                # The version of the CV as it was numbered for this score,
                # so that a copy of it can be told from the CV that was
                # scored without the text itself being kept twice.
                "cv_hash": getattr(system_blocks, "cv_hash", None),
                "cv_line_count": len(cv_lines),
                # The whole of any cited line longer than an ask keeps,
                # by number, so that a page can tell an edit to the end of
                # a long line from no edit at all.
                "long_lines": {str(c["n"]): cv_lines[c["n"] - 1].strip()
                               for a in asks for c in a["lines"]
                               if len(cv_lines[c["n"] - 1].strip())
                               > CV_LINE_TEXT_MAX},
                "description_hash": description_hash,
                "asks": asks, "counts": counts,
                "rate_quote": None, "rate_quote_from": None,
            },
        })
    if cv is None:
        return scored, usage, "scorer returned no usable cv match"
    return scored, usage, None


def _num(value):
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    # An infinity is not a figure, and would be stored as one.
    if number is not None and number in (float("inf"), float("-inf")):
        return None
    return number


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
