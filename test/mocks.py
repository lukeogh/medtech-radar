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
            **_pay_fields(salary),
            "source_url": url,
            "posted_date": "",
        })
    return json.dumps({"opportunities": opportunities})


_PERIOD_HINTS = (
    (re.compile(r"(per annum|a year|/year|per year|annually)", re.IGNORECASE), "year"),
    (re.compile(r"(per day|a day|/day|day rate|daily)", re.IGNORECASE), "day"),
    (re.compile(r"(per hour|an hour|/hour|hourly|p/h)", re.IGNORECASE), "hour"),
)
_CURRENCY_HINTS = (("£", "GBP"), ("€", "EUR"), ("$", "USD"))
_PAY_AMOUNT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _pay_fields(salary_text: str) -> dict:
    """Structured pay off the verbatim text, mirroring the extractor prompt.

    Extraction only, exactly like the real prompt demands. No conversion,
    no judgement, nulls when nothing usable is stated.
    """
    out = {"pay_currency": "", "pay_period": "",
           "pay_min": None, "pay_max": None}
    text = salary_text or ""
    for symbol, code in _CURRENCY_HINTS:
        if symbol in text:
            out["pay_currency"] = code
            break
    for pattern, period in _PERIOD_HINTS:
        if pattern.search(text):
            out["pay_period"] = period
            break
    amounts = [float(m.group(0).replace(",", ""))
               for m in _PAY_AMOUNT_RE.finditer(text)]
    # "6 month contract" style trailing numbers are not pay. Keep amounts
    # that look like money, meaning two digits or more.
    amounts = [a for a in amounts if a >= 10]
    if amounts:
        out["pay_min"], out["pay_max"] = min(amounts), max(amounts)
    return out


# --------------------------------------------------------------------- scorer

def _day_rate(text: str) -> tuple[bool, int]:
    """(is_day_rate, highest_amount). Currency symbol agnostic on purpose."""
    if not DAY_RATE_HINT.search(text):
        return False, 0
    amounts = [int(m.group(1).replace(",", "")) for m in AMOUNT_RE.finditer(text)]
    return True, max(amounts) if amounts else 0


def mock_scorer(user_content: str) -> str:
    """Deterministic four-gate scoring. No model, no network, no guessing.

    Fixtures cover all four tiers on purpose, because a tiering change that
    only ever sees one shape is untested.
    """
    import json as _json
    opp = _payload(user_content)
    title = str(opp.get("title") or "")
    company = str(opp.get("company") or "")
    location = str(opp.get("location") or "")
    salary = str(opp.get("salary_rate") or "")
    low = f"{title} {company}".lower()
    loc_low = location.lower()

    medtech = any(w in low for w in ("ivd", "diagnost", "medical", "medtech",
                                     "clinical", "biosens", "device"))
    adjacent = any(w in low for w in ("defence", "aerospace", "utilit", "nuclear"))
    leadership = any(w in low for w in ("director", "head of", "lead", "manager",
                                        "principal", "architect"))
    cv = 93 if (medtech and leadership) else 84 if medtech else \
         76 if adjacent else 55 if leadership else 30

    relocation = "relocat" in loc_low
    remote = "remote" in loc_low
    onsite_far = ("on-site" in loc_low or "onsite" in loc_low) and \
                 not any(c in loc_low for c in ("london", "surrey", "sussex",
                                                "guildford", "brighton", "belgium",
                                                "ghent", "leuven"))
    loc_class = ("relocation" if relocation else "remote" if remote
                 else "onsite-far" if onsite_far else
                 "hybrid" if "hybrid" in loc_low else "local")
    loc_pass = loc_class in ("remote", "hybrid", "local")

    stated = bool(salary.strip())
    per_day = "per day" in salary.lower() or "day rate" in salary.lower()
    digits = "".join(ch if ch.isdigit() else " " for ch in salary).split()
    amount = max((float(d) for d in digits), default=None) if digits else None
    if not stated:
        rate_pass, basis, value = True, "", None
    elif per_day:
        value, basis = amount, "day-rate"
        rate_pass = amount is not None and amount >= 650
    else:
        value = round(amount / 220) if amount else None
        basis = "converted-salary"
        rate_pass = value is not None and value >= 650

    return _json.dumps({
        "company": company, "role_title": title, "location": location,
        "source_url": opp.get("source_url", ""),
        "gate_sector": bool(medtech or adjacent),
        "gate_sector_note": ("medtech" if medtech else
                             "adjacent, regulated" if adjacent else
                             "generic software, out of sector"),
        "cv_match": cv,
        "gate_cv_note": f"evidence supports {cv}",
        "gate_location": loc_pass,
        "gate_location_note": ("relocation required" if relocation else
                              "workable from West Sussex" if loc_pass else
                              "full time on site beyond commuting distance"),
        "location_class": loc_class,
        "gate_rate": rate_pass,
        "gate_rate_note": ("rate unstated, that's your first question" if not stated
                           else f"{'at or above' if rate_pass else 'below'} the floor"),
        "rate_stated": stated, "rate_value": value, "rate_basis": basis,
        "ir35": "outside" if "outside ir35" in salary.lower() else "",
        "one_line_why": "Judged against the four gates.",
        "question_text": "", "suggested_action": "Read it and decide.",
        "act_by": "",
    })



def mock_enricher(user_content: str) -> str:
    """Deterministic company enrichment. No network, no model, no guessing.

    Reads only what the caller sent, the same discipline the real prompt is
    held to, so a test that expects an empty field gets one. The Veltrix
    fixture carries a full house, everything else comes back mostly empty,
    which is the honest shape of a first sighting.
    """
    import json as _json
    try:
        payload = _json.loads(user_content)
    except (ValueError, TypeError):
        payload = {}
    name = str(payload.get("company") or "")
    text = (str(payload.get("item_text") or "")
            + " " + str(payload.get("company_site_text") or "")).lower()
    low = name.lower()

    if "veltrix" in low:
        return _json.dumps({
            "what_they_build": "Point of care IVD analysers for hospital labs.",
            "stage": "series a", "country": "Belgium", "city": "Ghent",
            "ecosystem": "imec",
            "software_content": "Embedded firmware and a clinician facing results application.",
            "people": [{"name": "Anke De Vos", "role": "ceo"},
                       {"name": "Pieter Maes", "role": "cto"}],
        })
    if "northvale" in low:
        return _json.dumps({
            "what_they_build": "Molecular diagnostics for infectious disease.",
            "stage": "seed", "country": "United Kingdom", "city": "Cambridge",
            "ecosystem": "", "software_content": "",
            "people": [],
        })
    # The common case. A name, maybe a country, nothing else, and the empty
    # fields stay empty rather than being filled with something plausible.
    return _json.dumps({
        "what_they_build": "", "stage": "",
        "country": "Belgium" if "belgium" in text or "ghent" in text else "",
        "city": "", "ecosystem": "", "software_content": "", "people": [],
    })


def mock_drafter(user_content: str) -> str:
    """Deterministic announcement-day drafts. Playbook shape, no selling.

    Reads the supplied text for a specific detail, the same discipline the
    real prompt is held to, and returns an empty comment when there is
    nothing specific to say rather than inventing enthusiasm.
    """
    import json as _json
    try:
        payload = _json.loads(user_content)
    except (ValueError, TypeError):
        payload = {}
    company = str(payload.get("company") or "the company")
    article = str(payload.get("article_text") or "")
    headline = str(payload.get("headline") or "")
    haystack = f"{article} {headline}".lower()

    detail = ""
    for word, phrase in (("oct", "the multi-spot OCT approach"),
                         ("biosensor", "the self-cleaning biosensor work"),
                         ("metalens", "the metalens manufacturing route"),
                         ("assay", "the assay side of it"),
                         ("diagnost", "the diagnostics angle")):
        if word in haystack:
            detail = phrase
            break

    comment = (f"Congratulations on the round. {detail.capitalize()} is the "
               "part I would not have expected to see working this early."
               ) if detail else ""
    note = ("Congratulations on the round. I run software at a Belgian "
            "photonics diagnostics spin-off, so I know a little of the road "
            "ahead. If IEC 62304 or ISO 13485 ever land on your desk, happy "
            "to compare notes. Always good to know the neighbours.")
    return _json.dumps({"comment": comment, "connection_note": note})
