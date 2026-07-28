"""Deterministic mock extractor and scorer for offline acceptance tests.

There is no API key on this machine tonight, so the acceptance tests run the
whole pipeline with these keyword heuristics standing in for the Claude calls.
They are deliberately simple, deterministic and honest. A fractional IVD
director role near Ghent scores high, a generic test role scores low, and a
great CV fit at a poor day rate gets a high cv_match and a collapsed
want_match, exactly the spread the real scorer is asked for.

Both functions take the user_content string that scripts/score_item.py builds
for the real model and return a JSON string in the same shape the prompts
demand. radar_common.claude_call passes them through as mock_fn.

WS2 extends mock coverage in test/mocks_signals.py, not here.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import radar_common

URL_RE = re.compile(r"https?://\S+")
CURRENCY_RE = re.compile(r"[£€$]\s?\d")
AMOUNT_RE = re.compile(r"[£€$]\s?([\d,]+)")
DAY_RATE_HINT = re.compile(r"(per day|a day|/day|day rate)", re.IGNORECASE)

MEDTECH = re.compile(
    r"\b(medical|medtech|ivd|diagnostic|diagnostics|in vitro|photonic|photonics)\b",
    re.IGNORECASE)
LEADERSHIP = re.compile(r"\b(director|head of|manager|team lead|vp)\b", re.IGNORECASE)
REGULATED = re.compile(r"\b(62304|13485|regulated|regulatory|mdr|ivdr)\b", re.IGNORECASE)
ENGAGEMENT = re.compile(r"\b(fractional|interim)\b", re.IGNORECASE)
NEAR_BASE = re.compile(r"\b(belgium|ghent|leuven|brussels|remote|hybrid)\b", re.IGNORECASE)
DIRECTOR = re.compile(r"\bdirector\b", re.IGNORECASE)
ONSITE = re.compile(r"on.?site", re.IGNORECASE)
FLEXIBLE = re.compile(r"\b(hybrid|remote)\b", re.IGNORECASE)
TEST_WORDS = re.compile(r"\b(test|testing|qa|quality)\b", re.IGNORECASE)
SOFTWARE = re.compile(r"\bsoftware\b", re.IGNORECASE)
IVD = re.compile(r"\b(ivd|in vitro)\b", re.IGNORECASE)

# The mock's working marker for a director level day rate. Heuristic only.
DAY_RATE_FLOOR = 700


def _payload(user_content: str) -> dict:
    """Pull the JSON payload out of the user message the scripts build."""
    return radar_common.extract_json(user_content)


# ------------------------------------------------------------------ extractor

def mock_extractor(user_content: str) -> str:
    """Parse job blocks out of an alert email body.

    A block is the run of non-empty lines directly above a line containing a
    URL, bounded by a blank line or a previous URL line. Within a block the
    first line is the title, a line with a currency amount is the salary, and
    a separator line (middle dot or hyphen, no currency) is company/location.
    """
    email = _payload(user_content)
    if "RADAR-POISON" in (email.get("subject") or ""):
        # Simulates a model losing the plot so tests can exercise the
        # extraction retry cap. Never valid JSON, whatever the retry says.
        return "EXTRACTION NOISE {{{ not json"
    body = email.get("body_text", "") or ""
    lines = body.splitlines()
    opportunities = []
    for i, line in enumerate(lines):
        match = URL_RE.search(line)
        if not match:
            continue
        block = []
        j = i - 1
        while j >= 0 and lines[j].strip() and not URL_RE.search(lines[j]):
            block.insert(0, lines[j].strip())
            j -= 1
        if not block:
            continue
        url = match.group(0).rstrip(">.,)")
        salary = next((l for l in block if CURRENCY_RE.search(l)), "")
        company, location = "", ""
        for l in block[1:]:
            if CURRENCY_RE.search(l):
                continue
            if " · " in l or " - " in l:
                sep = " · " if " · " in l else " - "
                company, _, location = l.partition(sep)
                break
        opportunities.append({
            "company": company.strip(),
            "title": block[0],
            "location": location.strip(),
            "salary_rate": salary,
            "source_url": url,
            "posted_date": "",
        })
    return json.dumps({"opportunities": opportunities})


# --------------------------------------------------------------------- scorer

def _day_rate(text: str) -> tuple[bool, int]:
    """(is_day_rate, highest_amount). Currency symbol agnostic on purpose."""
    if not DAY_RATE_HINT.search(text):
        return False, 0
    amounts = [int(m.group(1).replace(",", "")) for m in AMOUNT_RE.finditer(text)]
    return True, max(amounts) if amounts else 0


def mock_scorer(user_content: str) -> str:
    opp = _payload(user_content)
    title_text = str(opp.get("title") or opp.get("role_title") or "")
    if "RADAR-UNSCORABLE" in title_text and not opp.get("rescore"):
        # Scoring failure trigger for tests. The rescore pass marks its
        # payload and succeeds, proving rescore.py clears the backlog.
        return "SCORER NOISE ((( not json"
    text = " ".join(str(opp.get(k, "")) for k in
                    ("title", "role_title", "company", "location", "salary_rate"))

    cv = 20
    if MEDTECH.search(text):
        cv += 30
    if LEADERSHIP.search(text):
        cv += 20
    if SOFTWARE.search(text):
        cv += 15
    else:
        cv -= 10
    if REGULATED.search(text):
        cv += 5
    if TEST_WORDS.search(text):
        cv += 5
    if IVD.search(text):
        cv += 8
    cv = max(5, min(cv, 97))

    want = 10
    if ENGAGEMENT.search(text):
        want += 30
    if MEDTECH.search(text):
        want += 25
    if NEAR_BASE.search(text):
        want += 15
    if DIRECTOR.search(text):
        want += 10

    red_flags = []
    rate_flag = False
    is_day_rate, amount = _day_rate(text)
    if is_day_rate:
        if amount >= DAY_RATE_FLOOR:
            want += 7
        else:
            want = min(want, 30)
            rate_flag = True
            red_flags.append("Day rate well below director level")
    elif ONSITE.search(text) and not FLEXIBLE.search(text):
        want -= 10
        red_flags.append("Permanent on site role, relocation risk")
    want = max(5, min(want, 97))

    combined = round((cv + want) / 2)

    if rate_flag:
        why = ("Right work, wrong money. The day rate sits far below director "
               "level, so the want score collapses.")
    elif combined >= 85:
        why = ("Exactly the target. Senior IVD software leadership on "
               "fractional terms at a proper rate.")
    elif cv >= 70 and want < 50:
        why = ("Good CV fit but a permanent on site post, not the engagement "
               "model I want.")
    elif cv < 30:
        why = "Nothing here for me. Outside software leadership and outside medtech."
    else:
        why = "Generic software work with no medtech angle. Not the target."

    if combined >= 70:
        action = "Read the full advert and reply within two working days."
        act_by = (date.today() + timedelta(days=2)).isoformat()
    elif rate_flag:
        action = "Park it. Note the company and revisit if the rate moves."
        act_by = ""
    else:
        action = "No action needed."
        act_by = ""

    return json.dumps({
        "company": opp.get("company", ""),
        "role_title": opp.get("title") or opp.get("role_title", ""),
        "location": opp.get("location", ""),
        "source_url": opp.get("source_url", ""),
        "thread_type": "inbound",
        "cv_match_pct": cv,
        "want_match_pct": want,
        "one_line_why": why,
        "red_flags": red_flags,
        "suggested_action": action,
        "act_by": act_by,
    })
