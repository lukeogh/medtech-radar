"""Roles added by hand. Luke finds work Radar cannot collect, on Upwork and
elsewhere, and adds it himself: the advert pasted, a few fields filled in.

A role added by hand is a row in opportunities like any other, scored on its
pasted advert through the full-description path (prepare_application), and
read on Jobs and Prepare application like any other. Three things set it
apart, all set in code, never by a model:

- added_at, when it was added. NULL on every collected row, so it is also
  what says a row was added by hand (is_hand).
- hand_type, the type Luke chose: contract, permanent or fixed-price. The
  scorer reads its own engagement type off the advert and would overwrite
  engagement_type, so the choice is kept apart and held over the scorer's
  (hold_hand_fields).
- source, "hand-" and the source chosen (SOURCES), so it never reads as an
  alert board.

Radar never requests the link. Nothing here fetches anything, the advert
checker skips these rows, and the expiry pass leaves them: Luke closes them
himself. No company row is created and no enrichment runs: the client links
to a company record only where the name matches one already on file.
"""

from __future__ import annotations

import re
import secrets

import radar_common as rc

SOURCE_PREFIX = "hand-"
SOURCES = (("upwork", "Upwork"), ("referral", "Referral"),
           ("recruiter", "Recruiter"), ("company-site", "Company site"),
           ("other", "Other"))
SOURCE_WORDS = dict(SOURCES)
TYPES = (("contract", "Contract"), ("permanent", "Permanent"),
         ("fixed-price", "Fixed price"))
TYPE_WORDS = dict(TYPES)
CURRENCIES = (("GBP", "£"), ("USD", "$"), ("EUR", "€"))
CURRENCY_SIGNS = dict(CURRENCIES)
PERIODS = (("hour", "an hour"), ("day", "a day"), ("year", "a year"),
           ("fixed", "fixed"))
PERIOD_WORDS = dict(PERIODS)
FIXED = "fixed"

# Characters a field may hold. Over it, nothing is saved and the field says
# so. The advert's cap is the page checker's, the most a stored description
# ever holds.
CAPS = {"link": 2_000, "title": 200, "advert": 20_000, "client": 200,
        "location": 200}
AMOUNT_MAX = 10_000_000

MISSING = {"title": "Add a title.", "advert": "Paste the advert."}

# The fields that make a role, in the dialog's order, and what Edit may change
# on a role already added.
FIELDS = ("link", "title", "advert", "client", "location", "type", "source",
          "rate_min", "rate_max", "currency", "period")
EDITABLE = ("link", "client", "location", "type", "source", "rate_min",
            "rate_max", "currency", "period")

_REMOTE = re.compile(r"(?<![0-9A-Za-z])remote(?![0-9A-Za-z])", re.IGNORECASE)


# ------------------------------------------------------------ reading a row

def is_hand(row) -> bool:
    """Was this row added by hand."""
    try:
        return bool(row["added_at"])
    except (KeyError, IndexError, TypeError):
        return False


def source_key(source) -> str | None:
    """upwork, referral and the rest, for a hand source, else None."""
    text = str(source or "")
    if not text.startswith(SOURCE_PREFIX):
        return None
    key = text[len(SOURCE_PREFIX):]
    return key if key in SOURCE_WORDS else None


def source_word(source) -> str | None:
    key = source_key(source)
    return SOURCE_WORDS.get(key) if key else None


def is_upwork(row) -> bool:
    return source_key(_get(row, "source")) == "upwork"


def type_word(row) -> str:
    return TYPE_WORDS.get(str(_get(row, "hand_type") or ""), "")


def is_fixed(row) -> bool:
    """A fixed price: the period of the rate, whatever the type says."""
    return str(_get(row, "pay_period") or "").strip().lower() == FIXED


def _get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _money(amount: float, sign: str) -> str:
    return f"{sign}{amount:,.0f}" if float(amount) == int(amount) else f"{sign}{amount:,.2f}"


def rate_words(currency, period, pay_min, pay_max) -> str:
    """The rate as written: "$60 to $90 an hour", "£4,000 fixed"."""
    if not pay_min:
        return ""
    sign = CURRENCY_SIGNS.get(str(currency or "").upper(), "")
    words = _money(float(pay_min), sign)
    if pay_max and float(pay_max) != float(pay_min):
        words += f" to {_money(float(pay_max), sign)}"
    period_word = PERIOD_WORDS.get(str(period or ""), "")
    return f"{words} {period_word}".strip()


# ------------------------------------------------------------ the fields

def _text(body: dict, key: str) -> str:
    value = body.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError
    return value


def _amount(raw) -> float | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        raise ValueError
    number = float(str(raw).replace(",", "").replace("£", "").replace("$", "")
                   .replace("€", "").strip())
    if number != number or not 0 < number <= AMOUNT_MAX:
        raise ValueError
    return number


def check_fields(body: dict, only: tuple = FIELDS) -> tuple[dict, dict]:
    """The fields a request sends, checked. (fields, errors), errors keyed by
    field, each one line in the page's words. Only the keys in only are read,
    so Edit cannot reach the title or the advert."""
    errors: dict = {}
    out: dict = {}
    if not isinstance(body, dict):
        return {}, {"form": "Nothing was sent."}
    for key in ("link", "title", "advert", "client", "location"):
        if key not in only:
            continue
        try:
            value = _text(body, key)
        except ValueError:
            errors[key] = "Not text."
            continue
        value = value.strip("\n") if key == "advert" else " ".join(value.split())
        if len(value) > CAPS[key]:
            errors[key] = f"Over {CAPS[key]:,} characters."
        out[key] = value
    if "title" in only and not out.get("title"):
        errors.setdefault("title", MISSING["title"])
    if "advert" in only and not str(out.get("advert") or "").strip():
        errors.setdefault("advert", MISSING["advert"])
    if out.get("link") and not out["link"].lower().startswith(("http://", "https://")):
        errors["link"] = "Check the link."
    if "type" in only:
        out["type"] = body.get("type") or "contract"
        if out["type"] not in TYPE_WORDS:
            errors["type"] = "Choose a type."
    if "source" in only:
        out["source"] = body.get("source") or "other"
        if out["source"] not in SOURCE_WORDS:
            errors["source"] = "Choose a source."
    if "rate_min" in only:
        try:
            low, high = _amount(body.get("rate_min")), _amount(body.get("rate_max"))
            currency = str(body.get("currency") or "GBP").upper()
            period = str(body.get("period") or "day").lower()
            if currency not in CURRENCY_SIGNS or period not in PERIOD_WORDS:
                raise ValueError
            if low is None and high is not None:
                raise ValueError
            if low is not None and high is not None and high < low:
                raise ValueError
        except (TypeError, ValueError):
            errors["rate"] = "Check the rate."
        else:
            if low is None:
                out.update(pay_currency=None, pay_period=None, pay_min=None, pay_max=None)
            else:
                out.update(pay_currency=currency, pay_period=period, pay_min=low,
                           pay_max=high if high is not None else low)
    return out, errors


# ------------------------------------------------------------ in code

def location_class(location) -> str | None:
    """remote where the location says so, a whole word. Else None, which leaves
    the class to the scorer, as for any advert."""
    return "remote" if _REMOTE.search(str(location or "")) else None


def region_of(location, config: dict) -> str:
    """The region from the location text by the existing region rules
    (rc.region_for): each part between commas, slashes and brackets is tried as
    a place and as a country, and the first group in the rules' order that any
    part matches wins. Nothing matched is the last group, Elsewhere."""
    parts = [p.strip() for p in re.split(r"[,/()|;]+", str(location or "")) if p.strip()]
    names = rc.region_group_names(config)
    fallback = names[-1]
    found = {rc.region_for(p, p, config) for p in parts}
    for name in names:
        if name in found and name != fallback:
            return name
    return fallback


def known_company(conn, client) -> int | None:
    """The id of a company already on file under this name, never a new one."""
    key = rc.normalise_company(client)
    if not key:
        return None
    row = conn.execute("SELECT id FROM companies WHERE norm_name = ?", (key,)).fetchone()
    return int(row[0]) if row else None


def engagement_of(hand_type: str) -> tuple[str, str | None]:
    """engagement_type and its note for the type chosen. A fixed price is
    contract work, said so in the note."""
    if hand_type == "permanent":
        return "permanent", None
    if hand_type == "fixed-price":
        return "contract", "fixed price"
    return "contract", None


def hold_hand_fields(row: dict, scored: dict) -> None:
    """On a score of a role added by hand, what Luke chose stands over what the
    scorer read off the advert: the type, and Remote where he said so."""
    if not is_hand(row):
        return
    kind, note = engagement_of(str(row.get("hand_type") or "contract"))
    scored["engagement_type"], scored["engagement_note"] = kind, note
    if location_class(row.get("location")):
        scored["location_class"] = "remote"


def existing_by_link(conn, link: str) -> int | None:
    if not link:
        return None
    row = conn.execute("SELECT id FROM opportunities WHERE url_hash = ?",
                       (rc.url_hash(link),)).fetchone()
    return int(row[0]) if row else None


def add_role(conn, body: dict, now: str, config: dict) -> dict:
    """Store one role added by hand, unscored. The caller holds the write lock
    and commits. {"ok": True, "id"} for a new role, {"ok": True, "existing": id}
    where Radar already holds the link, {"ok": False, "invalid": True,
    "errors"} where a field is missing or wrong, and nothing is stored."""
    fields, errors = check_fields(body)
    if errors:
        return {"ok": False, "invalid": True, "errors": errors}
    link = fields["link"]
    held = existing_by_link(conn, link)
    if held is not None:
        return {"ok": True, "existing": held}
    h = rc.url_hash(link) if link else rc.url_hash(f"hand:{now}:{secrets.token_hex(16)}")
    kind, note = engagement_of(fields["type"])
    client, location, title = fields["client"], fields["location"], fields["title"]
    day_rate = rc.convert_to_day_rate(fields["pay_currency"], fields["pay_period"],
                                      fields["pay_min"], fields["pay_max"], config)
    cur = conn.execute(
        "INSERT OR IGNORE INTO opportunities"
        " (url_hash, first_seen, last_seen, source, company, title, location,"
        "  salary_rate, source_url, thread_type, status, status_changed_at,"
        "  pay_currency, pay_period, pay_min, pay_max, day_rate, company_id, region,"
        "  role_key, location_class, engagement_type, engagement_note,"
        "  description_text, description_fetched_at, read_at, added_at, hand_type)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (h, now, now, SOURCE_PREFIX + fields["source"], client, title, location,
         rate_words(fields["pay_currency"], fields["pay_period"],
                    fields["pay_min"], fields["pay_max"]),
         link, "inbound", "new", now,
         fields["pay_currency"], fields["pay_period"], fields["pay_min"],
         fields["pay_max"], day_rate, known_company(conn, client),
         region_of(location, config), rc.role_key(title, client),
         location_class(location), kind, note, fields["advert"], now, now, now,
         fields["type"]))
    if not cur.rowcount:
        held = existing_by_link(conn, link)
        if held is not None:
            return {"ok": True, "existing": held}
        return {"ok": False, "note": "The role could not be stored. Try again."}
    return {"ok": True, "id": int(cur.lastrowid)}
