"""Deterministic mock for the signal scorer. Offline acceptance testing only.

Extends the WS1 mock family (test/mocks.py) without touching it. The mock is a
keyword heuristic over the scorer's user message and returns the same JSON
shape prompts/signal-scorer.md demands. It is honest enough that the three
sample announcements rank correctly:

  perfect_signal.md  scores 100 (>= 75, triggers the fast-signal push path)
  marginal.md        scores 45  (relevant sector, no buying window, no push)
  irrelevant.md      scores 0   (< 40, consumer electronics noise)

No randomness. Same input, same output, every run.
"""

from __future__ import annotations

import json
import re

_BASE = 10

# (pattern, weight). Each group counts once, however many times it matches.
_WEIGHTS = [
    (r"seed round|series a|seed funding|funding round|raises [€$£]|raised [€$£]"
     r"|million seed|million round", 25),
    (r"spin-off|spin-out|spinout|spinoff", 20),
    (r"\bivd\b|in vitro diagnostic|in-vitro diagnostic|diagnostics", 15),
    (r"photonic|raman|optical chip", 10),
    (r"\bimec\b|ghent|leuven|tyndall|\bcsem\b|cea-leti|fraunhofer|holst|\btno\b", 10),
    (r"first software|no software team|software hire|quality and regulatory hire"
     r"|qa lead|first quality", 15),
    (r"accelerator|cohort", 15),
    (r"medical device|medtech", 10),
    (r"belgium|belgian|netherlands|dutch|germany|german|france|french"
     r"|switzerland|swiss|ireland|irish|\buk\b|europe|european", 5),
    (r"partnership|facility|expansion", 10),
    (r"hospital|clinical|point of care", 10),
    (r"pharma|pharmaceutical|phase iii|phase 3", -30),
    (r"consumer|earbuds|soundbar|smartphone|gaming|home entertainment", -30),
    (r"quarterly results|earnings|share buyback|annual report", -15),
]

_VERBS = (r"(raises|raised|secures|closes|announces|unveils|launches|opens"
          r"|partners|expands|reports|names|appoints|signs|introduces|enters"
          r"|joins|wins)")


def _field(content: str, name: str) -> str:
    match = re.search(rf"^{name}:\s*(.+)$", content, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _company_from(headline: str) -> str:
    match = re.match(rf"(.+?)\s+{_VERBS}\b", headline, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return " ".join(headline.split()[:3]) or "Unknown company"


def mock_signal_scorer(user_content: str) -> str:
    """Drop-in mock_fn for radar_common.claude_call. Returns a JSON string."""
    lowered = user_content.lower()
    score = _BASE
    for pattern, weight in _WEIGHTS:
        if re.search(pattern, lowered):
            score += weight
    score = max(0, min(100, score))

    headline = _field(user_content, "Headline") or "untitled item"
    url = _field(user_content, "URL")
    company = _company_from(headline)

    if score >= 75:
        why = ("Fresh money, regulated diagnostics ambitions and no software "
               "lead yet. The 62304 conversation starts now.")
        playbook_step = ("Comment on the announcement today, then send the "
                         "short connection note to the CEO or CTO.")
    elif score >= 40:
        why = ("Relevant sector but an established firm with settled teams. "
               "Watch for hiring signals rather than act today.")
        playbook_step = ("No same-day move. Log it and watch for a first "
                         "software or QA hire.")
    else:
        why = "Not a medtech signal. No regulated software angle here."
        playbook_step = "None. Ignore it."

    # The kind of story, by the same kind of keyword reading, so the
    # Insights sections have something honest to file under offline.
    if re.search(r"spin-off|spin-out|spinout|spinoff|spins out", lowered):
        story_type = "spin-off"
    elif re.search(r"accelerator|cohort", lowered):
        story_type = "accelerator"
    elif re.search(r"seed round|series a|series b|funding|raises|raised|grant", lowered):
        story_type = "funding"
    elif re.search(r"guidance|regulation|consultation|standard|notified body", lowered):
        story_type = "regulation"
    elif re.search(r"advertises|hiring|vacancy|first quality", lowered):
        story_type = "first-hire"
    else:
        story_type = "other"
    card_summary = {True: "Early company, regulated software ahead, no software lead named.",
                    False: "Right sector, no buying window yet."}[score >= 75]
    if score < 40:
        card_summary = "Not a medtech signal."

    return json.dumps({
        "company": company,
        "type": story_type,
        "headline": headline,
        "card_summary": card_summary,
        "relevance": score,
        "why": why,
        "playbook_step": playbook_step,
        "source_url": url,
    })
