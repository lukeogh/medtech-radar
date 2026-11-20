"""The advert check made when a role is opened (advert freshness).

When Luke opens a role on Jobs or on Prepare application, and the role has a
LinkedIn advert whose page has not been read in the last day, Radar reads that
advert once. The page draws first; its script then makes one POST,
/jobs/advert-check, and the role header says what came back:

- live: Live on, and the date of that read
- closed or gone: the role is closed exactly as the page checker closes one
  (closed_at, closed_source page_check), the header shows Closed on LinkedIn,
  the role leaves the list the next time it loads, and Undo last restores it
- unclear, refused or failed: nothing is stored; the header shows Couldn't
  check beside the last known date
- held back by a limit: the last known date and nothing else

The read is the page checker's own: its fetch, its ladder and its way of
storing a verdict (check_jobs.store_verdict), through the limiter every
LinkedIn read shares (linkedin_reads). No model is asked anything: the page is
read in code, and a role whose card had no pay is re-gated in code.

Never read: a role added by hand, a role with no LinkedIn advert, and a role
that is not in an open group (acknowledged, closed, dismissed, expired or
applied).
"""

from __future__ import annotations

import html
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import radar_common as rc  # noqa: E402
import check_jobs  # noqa: E402
import linkedin_reads  # noqa: E402

FRESH_HOURS = 24        # a page read inside this is not read again on open
ROUTE = "/jobs/advert-check"


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def _uk(iso):
    """A stored UTC moment in UK time, or None."""
    try:
        moment = datetime.strptime(str(iso or "")[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    moment = moment.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo("Europe/London"))
    except Exception:  # noqa: BLE001  no tz database: UTC, right all winter
        return moment


def day(iso) -> str:
    moment = _uk(iso)
    return f"{moment.day} {moment:%b}" if moment else ""


# ------------------------------------------------------------- which roles

def job_id(row) -> str | None:
    """The LinkedIn job id of a role Radar may read, else None. A role added
    by hand is never read, whatever its link."""
    if rc._row_field(row, "added_at"):
        return None
    return check_jobs.job_view_id(rc._row_field(row, "source_url") or "")


def in_open_group(row, applied: bool) -> bool:
    """In Top prospects, One question away, Reading or Filtered: not
    acknowledged, closed, dismissed, expired or applied."""
    if applied:
        return False
    return not any(rc._row_field(row, col) for col in
                   ("acknowledged_at", "closed_at", "dismissed_at", "expired_at"))


def last_live(row) -> str:
    """When a page read last answered live, or ""."""
    kept = rc._row_field(row, "last_live_check_at") or ""
    checked = (rc._row_field(row, "last_checked_at") or ""
               if rc._row_field(row, "last_check_result") == "live" else "")
    return max(kept, checked)


def last_words(row) -> str:
    """The last known check: Live on 17 Nov, or Not checked yet."""
    live = last_live(row)
    return f"Live on {day(live)}" if live else "Not checked yet"


def due(row, applied: bool, now: str | None = None) -> bool:
    """Would opening this role read its advert: a LinkedIn advert, an open
    group, and no page read in the last day."""
    if job_id(row) is None or not in_open_group(row, applied):
        return False
    checked = rc._row_field(row, "last_checked_at")
    if not checked:
        return True
    moment = datetime.strptime(now or rc.now_iso(), "%Y-%m-%dT%H:%M:%SZ")
    cutoff = (moment - timedelta(hours=FRESH_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return checked < cutoff


def header_bit(row, applied: bool, now: str | None = None) -> str:
    """The header's check, as one fact for the facts line, or "".

    Drawn only for a role due a read. It says Checking the advert, muted, and
    the page script makes the read and says what came back."""
    if not due(row, applied, now):
        return ""
    oid = int(rc._row_field(row, "id"))
    return (f'<span class="adv-check adv-muted" data-advert-check="{oid}" data-due="1" '
            f'data-last="{esc(last_words(row))}" '
            f'data-title="{esc(rc._row_field(row, "title") or "")}" '
            'role="status" aria-live="polite">Checking the advert</span>')


# ------------------------------------------------------------- the read

def check_on_open(conn, item_id: int) -> dict:
    """Read one role's advert because it was opened. Writes only what the
    page checker would write for the same answer, and nothing on an unclear
    one. Returns {"ok": True, "state": live | closed | failed | limited |
    none, "day": the read's date for live, "why": the reason}."""
    row = conn.execute("SELECT * FROM opportunities WHERE id = ?",
                       (item_id,)).fetchone()
    if row is None:
        return {"ok": False, "note": f"no opportunity {item_id}"}
    row = dict(row)
    applied = rc.role_is_applied(conn, item_id)
    jid = job_id(row)
    if jid is None or not in_open_group(row, applied):
        return {"ok": True, "state": "none"}
    if not due(row, applied):
        return {"ok": True, "state": "limited", "why": "read in the last day"}
    got = linkedin_reads.reserve(conn, "open", item_id)
    if not got["ok"]:
        return {"ok": True, "state": "limited", "why": got["why"]}
    try:
        status, final_url, body = check_jobs.fetch_job_page(
            f"https://www.linkedin.com/jobs/view/{jid}/")
    except Exception as err:  # noqa: BLE001  never a 500 for a fetch
        print(f"advert check: the fetch for id {item_id} raised "
              f"{type(err).__name__}", file=sys.stderr)
        status, final_url, body = 0, "", ""
    verdict, reason = check_jobs.classify(status, final_url, body,
                                          row.get("title"), jid)
    linkedin_reads.finish(conn, got["id"],
                          linkedin_reads.outcome_of(status, verdict))
    if verdict not in ("live", "closed", "gone"):
        return {"ok": True, "state": "failed", "why": reason,
                "rate_limited": status == 429}
    stored = check_jobs.store_verdict(conn, row, verdict, body, True, {},
                                      stamp_unknown=False)
    if stored["left_alone"]:
        return {"ok": True, "state": "none", "why": "applied meanwhile"}
    if verdict == "live":
        fresh = conn.execute("SELECT last_live_check_at FROM opportunities"
                             " WHERE id = ?", (item_id,)).fetchone()
        return {"ok": True, "state": "live",
                "day": day(fresh["last_live_check_at"] if fresh else rc.now_iso())}
    return {"ok": True, "state": "closed", "why": reason,
            "closed": stored["closed"]}


# ------------------------------------------------------------- the page

CSS = """
.adv-muted{color:var(--text-3)}
.adv-note{margin-left:8px;color:var(--text-3)}
.adv-closed{display:inline-flex;align-items:center;height:20px;padding:0 8px;border-radius:var(--radius-2,4px);background:var(--negative-tint);color:var(--negative);font-size:13px;line-height:16px;font-weight:600;white-space:nowrap}
@media (forced-colors:active){.adv-closed{border:1px solid CanvasText}}
"""

# The read the header's check asks for, once per role drawn, after the page
# has drawn. A closed answer goes on the undo stack Jobs keeps for the tab, so
# Undo last restores the role, from Jobs or after coming back from Prepare.
SCRIPT = r"""
(function () {
  var UNDO = 'radar-jobs-undo';
  function remember(id, title) {
    var s = [];
    try { s = JSON.parse(sessionStorage.getItem(UNDO) || '[]'); } catch (e) { s = []; }
    s.push({ kind: 'close', id: id, title: title });
    try { sessionStorage.setItem(UNDO, JSON.stringify(s.slice(-20))); } catch (e) {}
    try { document.dispatchEvent(new CustomEvent('radar-undo-changed')); } catch (e) {}
  }
  function part(el, words, cls) {
    var span = document.createElement('span');
    if (cls) span.className = cls;
    span.textContent = words;
    el.appendChild(span);
  }
  function paint(el, j, last, id, title) {
    if (!el) return;
    el.removeAttribute('data-due');
    el.className = 'adv-check';
    el.textContent = '';
    var state = j && j.state;
    if (state === 'live' && j.day) { el.textContent = 'Live on ' + j.day; return; }
    if (state === 'closed') { part(el, 'Closed on LinkedIn', 'adv-closed'); remember(id, title); return; }
    if (state === 'limited' || state === 'none') { el.textContent = last; return; }
    part(el, last);
    part(el, "Couldn't check", last ? 'adv-note' : 'adv-muted');
  }
  function check(scope) {
    var root = scope && scope.querySelector ? scope : document;
    var el = root.querySelector('[data-advert-check][data-due="1"]');
    if (!el || el.getAttribute('data-started')) return;
    el.setAttribute('data-started', '1');
    var id = Number(el.getAttribute('data-advert-check'));
    var last = el.getAttribute('data-last') || '';
    var title = el.getAttribute('data-title') || '';
    el.className = 'adv-check adv-muted';
    el.textContent = 'Checking the advert';
    function here() {
      return document.querySelector('[data-advert-check="' + id + '"]');
    }
    fetch('/jobs/advert-check', { method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
          body: JSON.stringify({ id: id }) })
      .then(function (r) { return r.json(); })
      .then(function (j) { paint(here(), j, last, id, title); },
            function () { paint(here(), { state: 'failed' }, last, id, title); });
  }
  window.radarAdvertCheck = check;
  check(document);
})();
"""
