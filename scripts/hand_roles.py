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
- upwork_fee, on a role from Upwork, the percentage Upwork takes, which comes
  off the rate before the Rate gate compares it with a floor. It starts from
  the Settings figure and each role keeps its own (fee_for).

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
          "rate_min", "rate_max", "currency", "period", "fee")
FEE_MAX = 99
EDITABLE = ("link", "client", "location", "type", "source", "rate_min",
            "rate_max", "currency", "period", "fee")

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


def rate_words(currency, period, pay_min, pay_max) -> str:
    """The rate as written: "$60 to $90 an hour", "£4,000 fixed"."""
    return rc.rate_as_written(currency, period, pay_min, pay_max)


def fee_for(row, money: dict | None = None):
    """The fee that comes off this role's rate: its own on a role added by hand
    from Upwork, the Settings figure where it has none yet, None for every
    other role."""
    if not (is_hand(row) and is_upwork(row)):
        return None
    own = _get(row, "upwork_fee")
    if own is not None:
        return float(own)
    return (money if money is not None else rc.read_money_prefs())["fee"]


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


def check_fields(body: dict, only: tuple = FIELDS,
                 default_fee: float = rc.DEFAULT_UPWORK_FEE) -> tuple[dict, dict]:
    """The fields a request sends, checked. (fields, errors), errors keyed by
    field, each one line in the page's words. Only the keys in only are read,
    so Edit cannot reach the title or the advert. A role from Upwork carries a
    fee, default_fee (the Settings figure) where none is sent; any other role
    carries none."""
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
    if "fee" in only:
        out["upwork_fee"] = None
        if out.get("source") == "upwork":
            raw = body.get("fee")
            try:
                if raw is None or (isinstance(raw, str) and not raw.strip()):
                    fee = float(default_fee)
                elif isinstance(raw, bool):
                    raise ValueError
                else:
                    fee = float(str(raw).replace("%", "").strip())
                if fee != fee or not 0 <= fee <= FEE_MAX:
                    raise ValueError
                out["upwork_fee"] = fee
            except (TypeError, ValueError):
                errors["fee"] = "Check the fee."
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
    fields, errors = check_fields(body, default_fee=rc.read_money_prefs(config)["fee"])
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
        "  description_text, description_fetched_at, read_at, added_at, hand_type,"
        "  upwork_fee)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (h, now, now, SOURCE_PREFIX + fields["source"], client, title, location,
         rate_words(fields["pay_currency"], fields["pay_period"],
                    fields["pay_min"], fields["pay_max"]),
         link, "inbound", "new", now,
         fields["pay_currency"], fields["pay_period"], fields["pay_min"],
         fields["pay_max"], day_rate, known_company(conn, client),
         region_of(location, config), rc.role_key(title, client),
         location_class(location), kind, note, fields["advert"], now, now, now,
         fields["type"], fields["upwork_fee"]))
    if not cur.rowcount:
        held = existing_by_link(conn, link)
        if held is not None:
            return {"ok": True, "existing": held}
        return {"ok": False, "note": "The role could not be stored. Try again."}
    return {"ok": True, "id": int(cur.lastrowid)}


# ------------------------------------------------------------ Edit

EDIT_REFUSED = "Only a role added by hand can be edited."
EDIT_APPLIED = "This role is marked applied, so it keeps what it had."
LINK_HELD = "Radar already holds that link."


def edit_role(conn, body: dict, now: str, config: dict) -> dict:
    """Change what Edit may change on a role added by hand: the link, client,
    location, type, source, rate and fee, never the title or the advert. The
    Rate gate and the tier are judged again in code (regate_rates.judge_row),
    no model call, the score the role held kept in score_history first. The
    caller holds the write lock and commits on ok. A role marked applied keeps
    what it had."""
    import regate_rates
    oid = body.get("id") if isinstance(body, dict) else None
    if not isinstance(oid, int) or isinstance(oid, bool):
        return {"ok": False, "invalid": True, "errors": {"form": "No role named."}}
    raw = conn.execute("SELECT * FROM opportunities WHERE id = ?", (oid,)).fetchone()
    if raw is None:
        return {"ok": False, "gone": True, "note": "That role is no longer on file."}
    row = dict(raw)
    if not is_hand(row):
        return {"ok": False, "note": EDIT_REFUSED}
    if rc.role_is_applied(conn, oid):
        return {"ok": False, "note": EDIT_APPLIED}
    money = rc.read_money_prefs(config)
    fields, errors = check_fields(body, only=EDITABLE, default_fee=(
        row["upwork_fee"] if row.get("upwork_fee") is not None else money["fee"]))
    link = fields.get("link", "")
    if not errors and link != (row.get("source_url") or ""):
        held = existing_by_link(conn, link)
        if held is not None and held != oid:
            errors["link"] = LINK_HELD
    if errors:
        return {"ok": False, "invalid": True, "errors": errors}
    client, location = fields["client"], fields["location"]
    kind, note = engagement_of(fields["type"])
    # Remote where the location says so. Where it no longer does, a class this
    # code set for an earlier Remote is cleared rather than left standing.
    loc_class = location_class(location) or (
        None if location_class(row.get("location")) else row.get("location_class"))
    sets = {
        "source_url": link,
        "url_hash": (row["url_hash"] if link == (row.get("source_url") or "")
                     else rc.url_hash(link) if link
                     else rc.url_hash(f"hand:{now}:{secrets.token_hex(16)}")),
        "company": client, "location": location,
        "source": SOURCE_PREFIX + fields["source"], "hand_type": fields["type"],
        "salary_rate": rate_words(fields["pay_currency"], fields["pay_period"],
                                  fields["pay_min"], fields["pay_max"]),
        "pay_currency": fields["pay_currency"], "pay_period": fields["pay_period"],
        "pay_min": fields["pay_min"], "pay_max": fields["pay_max"],
        "day_rate": rc.convert_to_day_rate(fields["pay_currency"], fields["pay_period"],
                                           fields["pay_min"], fields["pay_max"], config),
        "company_id": known_company(conn, client), "region": region_of(location, config),
        "role_key": rc.role_key(row.get("title"), client), "engagement_type": kind,
        "engagement_note": note, "location_class": loc_class,
        "upwork_fee": fields["upwork_fee"],
    }
    moved = None
    if row.get("tier") is not None:
        new, _pay = regate_rates.judge_row({**row, **sets}, rc.read_rate_floors(config),
                                           config, money=money)
        if any(new[c] != row.get(c) for c in regate_rates._GATE_COLS):
            # The score the role held, kept before code changes it.
            rc.archive_score(conn, oid, "hand_roles")
            sets.update(new)
            if new["tier"] != row["tier"]:
                moved = {"from": row["tier"], "to": new["tier"]}
    conn.execute("UPDATE opportunities SET " + ", ".join(f"{c} = ?" for c in sets)
                 + " WHERE id = ? AND " + rc.NOT_APPLIED_SQL, (*sets.values(), oid))
    return {"ok": True, "id": oid, "tier_move": moved}


def edit_data(row) -> dict:
    """What Edit fills the dialog with, for one role added by hand."""
    def plain(value):
        if value is None:
            return ""
        value = float(value)
        return str(int(value)) if value == int(value) else f"{value:g}"
    period = str(_get(row, "pay_period") or "")
    return {"id": int(_get(row, "id")), "link": _get(row, "source_url") or "",
            "client": _get(row, "company") or "", "location": _get(row, "location") or "",
            "type": _get(row, "hand_type") or "contract",
            "source": source_key(_get(row, "source")) or "other",
            "rate_min": plain(_get(row, "pay_min")),
            "rate_max": (plain(_get(row, "pay_max"))
                         if _get(row, "pay_max") != _get(row, "pay_min") else ""),
            "currency": _get(row, "pay_currency") or "GBP",
            "period": period or "day",
            "fee": plain(_get(row, "upwork_fee")) if _get(row, "upwork_fee") is not None else ""}
