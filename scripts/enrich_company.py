#!/usr/bin/env python
"""Describe a company once, the first time the radar sees it.

Contract:

- One model pass per company, ever. A company with enriched_at set is never
  enriched again, so the same-day package never needs a research step and a
  busy inbox cannot spend the same tokens twice.
- Capped per run, like every other model call here. The cap is
  enrich_cap_per_run in config/radar.yaml. Reaching it is not an error, the
  rest wait for the next run or for scripts/backfill_enrich.py.
- Never blocks storing or scoring. Every failure path leaves the item stored
  and the score intact and writes a reason into enrich_status, because a
  missing description is a smaller problem than a lost advert.
- One polite fetch of the company's own site, and only when the item names
  one. robots.txt decides, a disallow means the fetch does not happen, and
  the enrichment proceeds from the item text alone saying so. LinkedIn is
  never fetched, by any path, ever.

Usage as a module:  enrich_company(conn, company_id, item_text, config, budget)
Usage as a script:  python scripts/enrich_company.py --company "Name" --mock
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import radar_common
import score_item

MAX_TOKENS = 900

# Hosts that are never the company's own site. A job board, a news outlet or
# an aggregator will happily serve a page about the company, and enriching
# from it would describe the publisher rather than the firm. LinkedIn heads
# the list and is a hard rule, not a preference.
NOT_COMPANY_HOSTS = (
    "linkedin.com", "lnkd.in", "indeed.com", "reed.co.uk", "cv-library.co.uk",
    "cvlibrary.co.uk", "totaljobs.com", "glassdoor.", "google.com",
    "dealroom.co", "crunchbase.com", "twitter.com", "x.com", "facebook.com",
    "youtube.com", "medium.com", "substack.com", "optics.org",
    "electronicsweekly.com", "semiconductor-digest.com", "picmagazine.net",
    "siliconsemiconductor.net", "startupsmagazine.co.uk", "imec-int.com",
    "tno.nl", "csem.ch", "tyndall.ie", "holstcentre.com",
)

_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)


def candidate_site(text: str) -> str | None:
    """The company's own site from the item text, or None.

    A URL that belongs to a board, a newspaper or an institute press desk is
    not the company. Nothing is guessed from the company name, because
    guessing a domain and fetching it is how a polite watcher starts
    knocking on strangers' doors.
    """
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(".,);]")
        host = ""
        try:
            host = url.split("//", 1)[1].split("/", 1)[0].lower()
        except IndexError:
            continue
        if any(bad in host for bad in NOT_COMPANY_HOSTS):
            continue
        return url
    return None


def fetch_site(url: str, config: dict) -> tuple[str, str]:
    """(text, note). One attempt, robots respected, never fatal."""
    if not url:
        return "", "no company site named in the item"
    ua = config.get("fetch_user_agent", "MedTechRadar/1.0")
    try:
        if not radar_common.robots_allowed(url, ua):
            return "", "company site disallowed by robots.txt, not fetched"
        status, _headers, body = radar_common.http_get(url, config)
    except Exception as err:                      # noqa: BLE001
        return "", f"company site fetch failed, {type(err).__name__}"
    if status == 999:
        return "", "company site disallowed by robots.txt, not fetched"
    if status != 200 or not body:
        return "", f"company site returned {status}"
    return radar_common.normalise_page_text(body)[:6000], ""


def get_mock_fn():
    """The deterministic enricher mock, only in mock mode."""
    if not radar_common.mock_mode_active():
        return None
    test_dir = str(SCRIPT_DIR.parent / "test")
    if test_dir not in sys.path:
        sys.path.insert(0, test_dir)
    import mocks
    return getattr(mocks, "mock_enricher", None)


def _clean(value) -> str:
    return radar_common.sanitise_free_text(str(value or "").strip())


def _clean_people(value) -> str:
    """People, kept only in the shape the prompt promised and sanitised."""
    out = []
    if isinstance(value, list):
        for p in value[:6]:
            if not isinstance(p, dict):
                continue
            name = _clean(p.get("name"))
            role = str(p.get("role") or "").strip().lower()
            if name and role in ("ceo", "cto"):
                out.append({"name": name, "role": role})
    return json.dumps(out)


# The facts and the source each one came from. The model says, per field,
# whether the company's own site or the item stated it; code keeps only
# those two answers and falls back to whichever text it actually sent.
FACT_KEYS = ("what_they_build", "stage", "country", "city", "ecosystem",
             "software_content", "people")


def _host(url: str) -> str:
    try:
        return url.split("//", 1)[1].split("/", 1)[0].lower()
    except IndexError:
        return ""


def fact_sources(parsed: dict, site_text: str, site_url: str | None,
                 item_label: str | None, at: str) -> dict:
    """{field: {"from", "label", "at"}} for every field the reading filled."""
    said = parsed.get("sources") if isinstance(parsed.get("sources"), dict) else {}
    out = {}
    for key in FACT_KEYS:
        value = parsed.get(key)
        if not value or value == "[]":
            continue
        src = str(said.get(key) or "").strip().lower()
        if src not in ("site", "item"):
            src = "site" if site_text else "item"
        if src == "site" and not site_text:
            src = "item"
        label = (f"Company site, {_host(site_url or '')}" if src == "site"
                 else (item_label or "The story or advert Radar first saw"))
        out[key] = {"from": src, "label": radar_common.sanitise_free_text(label)[:200], "at": at}
    return out


def _busy_retry(fn, tries: int = 1, pause: float = 3.0):
    """fn(), tried again after a pause while the database is busy, so a reply
    already paid for is kept rather than lost to another writer's lock."""
    import sqlite3
    import time
    for attempt in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as err:
            if attempt + 1 >= tries or "locked" not in str(err).lower() and "busy" not in str(err).lower():
                raise
            time.sleep(pause)


def enrich_company(conn, company_id: int, item_text: str, config: dict,
                   budget: dict | None = None, force: bool = False,
                   site_url: str | None = None,
                   item_label: str | None = None) -> dict:
    """Enrich one company if it has never been enriched. Never raises.

    budget is a mutable dict carrying "spent" and "cap" so one run's calls
    are counted across many companies. Omit it for a single deliberate call.

    force is the Watchlist's Read again, pressed by the owner with the cost
    shown first: it reads a company already read, once more, with one polite
    fetch of site_url (the site read before, or one the item names, never a
    guess) and one call. A field the new reading leaves empty keeps what was
    read before, with its old source and date, so a thinner page never wipes
    a fact. item_label names the item for the facts' sources.
    """
    result = {"enriched": False, "status": None, "usage": {}}
    try:
        row = conn.execute(
            "SELECT * FROM companies WHERE id = ?",
            (company_id,)).fetchone()
        if row is None:
            result["status"] = "no such company"
            return result
        if row["enriched_at"] and not force:
            result["status"] = "already enriched"
            return result
        if (not force and budget is not None
                and budget.get("spent", 0) >= budget.get("cap", 0)):
            result["status"] = "cap reached this run"
            return result

        site_url = site_url or candidate_site(item_text)
        site_text, fetch_note = fetch_site(site_url, config)

        user = json.dumps({
            "company": row["display_name"],
            "item_text": (item_text or "")[:4000],
            "company_site_text": site_text,
        }, ensure_ascii=False)
        system = score_item.load_prompt("enricher.md")
        model = config.get("claude_model_extract", "claude-haiku-4-5")
        text, usage = radar_common.claude_call(
            model, system, user, max_tokens=MAX_TOKENS, mock_fn=get_mock_fn())
        radar_common.add_usage(result["usage"], usage)
        if budget is not None:
            budget["spent"] = budget.get("spent", 0) + 1

        try:
            parsed = radar_common.extract_json(text)
        except (ValueError, json.JSONDecodeError):
            if force and row["enriched_at"]:
                result["status"] = "failed, unparseable model output"
                return result
            conn.execute(
                "UPDATE companies SET enriched_at = ?, enrich_status = ?"
                " WHERE id = ?",
                (radar_common.now_iso(), "failed, unparseable model output",
                 company_id))
            result["status"] = "failed, unparseable model output"
            return result

        status = "ok" if site_text else f"text-only, {fetch_note or 'no site text'}"
        now = radar_common.now_iso()
        values = {k: _clean(parsed.get(k)) for k in FACT_KEYS if k != "people"}
        people = _clean_people(parsed.get("people"))
        values["people"] = people if json.loads(people) else ""
        sources = fact_sources({**parsed, **values}, site_text, site_url, item_label, now)
        if force:
            # Read again: a field the new reading left empty keeps the old one.
            try:
                old_sources = json.loads(row["fact_sources"] or "{}")
            except (TypeError, ValueError, IndexError, KeyError):
                old_sources = {}
            old_ok = str(row["enrich_status"] or "").startswith("ok")
            for key in FACT_KEYS:
                if not values.get(key) and row[key] and row[key] != "[]":
                    values[key] = row[key]
                    if isinstance(old_sources, dict) and key in old_sources:
                        sources[key] = old_sources[key]
                    elif row["enriched_at"]:
                        sources[key] = {"from": "site" if old_ok else "item",
                                        "label": ("The company site or the first story" if old_ok
                                                  else "The first story or advert Radar saw"),
                                        "at": row["enriched_at"]}
        _busy_retry(lambda: conn.execute(
            "UPDATE companies SET what_they_build = ?, stage = ?, country = ?,"
            " city = ?, ecosystem = ?, software_content = ?, people = ?,"
            " enriched_at = ?, enrich_status = ?, site_url = COALESCE(?, site_url),"
            " fact_sources = ? WHERE id = ?",
            (values["what_they_build"], values["stage"], values["country"],
             values["city"], values["ecosystem"], values["software_content"],
             values["people"] or "[]", now, status,
             site_url if site_text else None, json.dumps(sources), company_id)),
            tries=4 if force else 1)
        parsed = {**parsed, "country": values["country"], "city": values["city"]}
        # Enrichment is the moment a company's country becomes known, so it
        # is also the moment its region can be decided. Doing it here keeps
        # the mirror honest without waiting for a rules change.
        try:
            region = radar_common.region_for(
                _clean(parsed.get("country")), _clean(parsed.get("city")),
                config)
            conn.execute("UPDATE companies SET region = ? WHERE id = ?",
                         (region, company_id))
            conn.execute("UPDATE opportunities SET region = ?"
                         " WHERE company_id = ?", (region, company_id))
            conn.execute("UPDATE signals SET region = ? WHERE company_id = ?",
                         (region, company_id))
            result["region"] = region
        except Exception:                          # noqa: BLE001
            pass   # a missing region is a grouping nuisance, not a lost row
        result.update({"enriched": True, "status": status})
        return result
    except Exception as err:                      # noqa: BLE001
        # Enrichment is a description, not the record. Nothing it can do
        # should cost the caller a stored advert or a finished score.
        result["status"] = f"failed, {type(err).__name__}"
        try:
            conn.execute(
                "UPDATE companies SET enriched_at = ?, enrich_status = ?"
                " WHERE id = ? AND enriched_at IS NULL",
                (radar_common.now_iso(), result["status"], company_id))
        except Exception:                         # noqa: BLE001
            pass
        return result


def new_budget(config: dict) -> dict:
    return {"spent": 0, "cap": int(config.get("enrich_cap_per_run", 5))}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Enrich one company by name, for deliberate single runs.")
    parser.add_argument("--company", required=True)
    parser.add_argument("--text", default="", help="item text to enrich from")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--db")
    args = parser.parse_args(argv)
    if args.mock:
        os.environ["RADAR_MOCK"] = "1"
    radar_common.load_env()
    config = radar_common.load_config()
    conn = radar_common.get_db(Path(args.db).resolve() if args.db else None)
    cid = radar_common.resolve_company(conn, args.company)
    if cid is None:
        print(json.dumps({"ok": False, "note": "empty company name"}))
        return 2
    res = enrich_company(conn, cid, args.text, config, new_budget(config))
    conn.commit()
    radar_common.log_run(conn, "enrich",
                         mode="mock" if radar_common.mock_mode_active() else "live",
                         items_in=1, items_new=1 if res["enriched"] else 0,
                         model=config.get("claude_model_extract"),
                         usage=res["usage"], note=str(res["status"]))
    conn.close()
    print(json.dumps({"ok": True, **{k: v for k, v in res.items() if k != "usage"}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
