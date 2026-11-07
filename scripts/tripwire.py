#!/usr/bin/env python
"""A first quality or regulatory hire at an untouched company is a signal.

The doctrine says the buying window opens when a company first hires for QA,
regulatory or software leadership. Where that company is already in the touch
log, process_email records a buying window against it. Where it is not, the
advert has until now been scored as a job for Luke, scored low because a QA
manager post is not what he wants, and sunk.

That is the wrong reading. The advert is not an opportunity for Luke, it is
news about a company that has just started caring about IEC 62304 and ISO
13485. This routes a copy down the signal path so the rubric can judge it as
what it is.

Three gates, all of which must pass, and all of them cheap and testable:

- The title reads as a first quality or regulatory hire.
- The advert reads as medtech or IVD, so a hospital trust QA manager or a
  food-safety auditor never fires it. Only fields the extractor already
  returns are consulted, no extra model call to decide whether to spend a
  model call.
- The company is not in the touch log. A touched company gets a buying
  window instead, and getting both would be the same event twice.

One per company, ever, checked against the signals already stored rather
than a flag, so a database restored from backup cannot re-open one.

Nothing here fixes a relevance. An untouched company deserves the rubric's
judgement rather than a number chosen in advance.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import radar_common

SOURCE_ID = "qa-tripwire"

# First quality or regulatory hire. Deliberately about the function rather
# than seniority, because the first one of these at a small company is the
# signal whatever it is called.
_TITLE = re.compile(
    r"\b("
    r"qa|q\.a\.|quality assurance|quality manager|quality engineer|"
    r"quality lead|quality director|head of quality|quality systems?|"
    r"regulatory|regulatory affairs|\bra\b|ra manager|ra specialist|"
    r"qms|quality and regulatory|reg affairs"
    r")\b", re.I)

# The advert has to read as medical devices or diagnostics. These are the
# words that industry uses about itself, plus the standards that only apply
# to it, and a company name is as good a place to find them as a title.
_MEDTECH = re.compile(
    r"\b("
    r"medical device|medical-device|medtech|med-tech|"
    r"ivd|in vitro diagnostic|diagnostics?|"
    r"iso ?13485|iec ?62304|iso ?14971|mdr|ivdr|"
    r"biotech|life science|clinical|healthcare technology|"
    r"assay|biosensor|point of care|point-of-care|"
    r"pharmaceutical|pharma|drug delivery|combination product"
    r")\b", re.I)

# Places where a QA or regulatory role says nothing about a product company
# starting to care about device standards. A hospital always has quality
# people and always will, and that is not news.
_NOT_A_PRODUCT_COMPANY = re.compile(
    r"\b("
    r"nhs|hospital|trust|clinic|surgery|gp practice|"
    r"council|university|college|school|"
    r"recruit|staffing|agency|consultancy services|umbrella|"
    r"food|catering|hospitality|automotive|aerospace|construction|"
    r"rail|nuclear|oil and gas|utilities"
    r")\b", re.I)


def is_first_hire_title(title) -> bool:
    """Does this title read as a quality or regulatory hire."""
    return bool(_TITLE.search(str(title or "")))


def reads_as_medtech(company, title, location=None) -> bool:
    """Does the advert read as a medical device or diagnostics company.

    Reads only what the extractor already gives us. The exclusion wins over
    the inclusion, because "Quality Manager, NHS Trust, medical devices" is
    a hospital job and the hospital is the stronger fact.
    """
    haystack = " ".join(str(x or "") for x in (company, title, location))
    if _NOT_A_PRODUCT_COMPANY.search(haystack):
        return False
    return bool(_MEDTECH.search(haystack))


def already_tripped(conn, company) -> bool:
    """Has this company already fired a tripwire, ever."""
    key = radar_common.normalise_company(company)
    if not key:
        return True          # an unnamed company cannot be tracked, so never fire
    for row in conn.execute(
            "SELECT company FROM signals WHERE source_id = ?", (SOURCE_ID,)):
        if radar_common.normalise_company(row["company"]) == key:
            return True
    return False


def should_trip(conn, company, title, location, touched: set) -> tuple[bool, str]:
    """All three gates plus the one-per-company rule. (fire, why_not)."""
    if not is_first_hire_title(title):
        return False, "title is not a quality or regulatory hire"
    if not reads_as_medtech(company, title, location):
        return False, "advert does not read as medtech or IVD"
    key = radar_common.normalise_company(company)
    if not key:
        return False, "advert names no company"
    if key in touched:
        return False, "company is already in the touch log, buying window covers it"
    if already_tripped(conn, company):
        return False, "company has already tripped once"
    return True, ""
