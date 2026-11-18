"""The Add a role dialog on Jobs (hand_roles). The markup, its style and its
script. One <dialog>, on the pattern of the other dialogs: a heading with a
close button, the fields in the brief's order, and a footer above a hairline
with the scoring cost at the left, Cancel and Add and score at the right.

Add and score posts /jobs/add, which stores the role and then scores it on
the pasted advert through the full-description path, the one call the press
says yes to. The browser then goes to the role on Jobs, or to the role Radar
already holds under the same link.
"""

from __future__ import annotations

import html
import json

import hand_roles

esc = html.escape

CLOSE = ('<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false">'
         '<path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6" '
         'stroke-linecap="round"/></svg>')


def cost_words() -> str:
    """The scoring cost, worded as Prepare application's Score the full
    description says it (prepare_page.COST), from the same place."""
    import prepare_page
    return prepare_page.COST


def _segs(name: str, choices, chosen: str | None, legend: str, lid: str) -> str:
    items = "".join(
        f'<label class="seg"><input type="radio" name="{name}" value="{esc(key)}"'
        f'{" checked" if key == chosen else ""}><span>{esc(word)}</span></label>'
        for key, word in choices)
    return (f'<fieldset class="ar-field" id="{lid}"><legend>{esc(legend)}</legend>'
            f'<div class="seg-row">{items}</div></fieldset>')


def _err(key: str) -> str:
    return f'<p class="ar-err" id="ar-{key}-err" data-err="{key}" aria-live="polite"></p>'


def dialog_html(fee: str = "15") -> str:
    """The dialog, closed. Contract is chosen to begin with, the rate in pounds
    a day; no source is chosen until one is. fee is the Upwork fee from
    Settings, which the fee row starts from."""
    currencies = [(k, sign) for k, sign in hand_roles.CURRENCIES]
    return f"""<dialog class="ar-dlg" id="ar-dlg" aria-labelledby="ar-h" data-mode="add">
<form class="ar-form" id="ar-form" novalidate>
<div class="ar-top"><h2 id="ar-h">Add a role</h2>
<button type="button" class="ar-x" id="ar-x" aria-label="Close">{CLOSE}</button></div>
<div class="ar-body">
<div class="ar-field"><label for="ar-link">Link</label>
<input class="ar-in" id="ar-link" name="link" type="url" inputmode="url" autocomplete="off" maxlength="{hand_roles.CAPS['link']}" aria-describedby="ar-link-err">{_err("link")}</div>
<div class="ar-field" data-new><label for="ar-title">Title</label>
<input class="ar-in" id="ar-title" name="title" type="text" autocomplete="off" maxlength="{hand_roles.CAPS['title']}" aria-required="true" aria-describedby="ar-title-err">{_err("title")}</div>
<div class="ar-field" data-new><label for="ar-advert">Advert</label>
<textarea class="ar-in ar-advert" id="ar-advert" name="advert" rows="12" spellcheck="false" maxlength="{hand_roles.CAPS['advert']}" aria-required="true" aria-describedby="ar-advert-err"></textarea>{_err("advert")}</div>
<div class="ar-pair">
<div class="ar-field"><label for="ar-client">Client or company</label>
<input class="ar-in" id="ar-client" name="client" type="text" autocomplete="off" placeholder="Not named" maxlength="{hand_roles.CAPS['client']}" aria-describedby="ar-client-err">{_err("client")}</div>
<div class="ar-field"><label for="ar-location">Location</label>
<input class="ar-in" id="ar-location" name="location" type="text" autocomplete="off" maxlength="{hand_roles.CAPS['location']}" aria-describedby="ar-location-err">{_err("location")}</div>
</div>
{_segs("type", hand_roles.TYPES, "contract", "Type", "ar-type")}
{_segs("source", hand_roles.SOURCES, None, "Source", "ar-source")}
<fieldset class="ar-field" id="ar-rate"><legend>Rate</legend>
<div class="ar-rate-row">
<input class="ar-in ar-amt" id="ar-min" name="rate_min" type="text" inputmode="decimal" autocomplete="off" aria-label="Amount" aria-describedby="ar-rate-err">
<span class="ar-to">to</span>
<input class="ar-in ar-amt" id="ar-max" name="rate_max" type="text" inputmode="decimal" autocomplete="off" aria-label="Upper amount" aria-describedby="ar-rate-err">
<div class="seg-row" role="radiogroup" aria-label="Currency">{"".join(f'<label class="seg"><input type="radio" name="currency" value="{k}"{" checked" if k == "GBP" else ""}><span>{esc(s)}</span></label>' for k, s in currencies)}</div>
<div class="seg-row" role="radiogroup" aria-label="Period">{"".join(f'<label class="seg"><input type="radio" name="period" value="{k}"{" checked" if k == "day" else ""}><span>{esc(w)}</span></label>' for k, w in hand_roles.PERIODS)}</div>
</div>{_err("rate")}
<div class="ar-fee" id="ar-fee-row" hidden><label for="ar-fee">Upwork fee</label>
<input class="ar-in ar-fee-in" id="ar-fee" name="fee" type="text" inputmode="decimal" autocomplete="off" value="{esc(fee)}" data-default="{esc(fee)}" aria-describedby="ar-fee-err">
<span class="ar-pct">%</span></div>{_err("fee")}
<p class="ar-about" id="ar-about" aria-live="polite"></p></fieldset>
<p class="ar-err ar-form-err" data-err="form" aria-live="polite"></p>
</div>
<div class="ar-foot"><span class="ar-cost">{esc(cost_words())}</span>
<span class="ar-acts"><button type="button" class="ar-cancel" id="ar-cancel">Cancel</button>
<button type="submit" class="ar-go" id="ar-go">Add and score</button></span></div>
</form>
</dialog>"""


def open_button() -> str:
    return ('<button type="button" class="btn btn-secondary ar-open" id="ar-open" '
            'aria-haspopup="dialog">Add a role</button>')


CSS = """
.ar-open{margin-left:auto}
.ar-dlg{width:min(760px,calc(100vw - 32px));max-width:none;max-height:calc(100vh - 32px);padding:0;border:1px solid var(--border-subtle);border-radius:14px;background:var(--surface);color:var(--text-1);box-shadow:var(--shadow-overlay)}
.ar-dlg::backdrop{background:rgba(0,0,0,.6)}
.ar-dlg[open]{display:flex;flex-direction:column;overflow:hidden}
/* The heading and the footer stay put; the fields scroll between them. */
.ar-form{display:flex;flex-direction:column;flex:1 1 auto;min-height:0;max-height:calc(100vh - 34px)}
.ar-body{display:flex;flex-direction:column;gap:16px;flex:1 1 auto;min-height:0;overflow-y:auto;padding:4px 24px 16px}
.ar-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex:none;padding:24px 24px 12px}
.ar-top h2{font:600 26px/32px var(--font-sans)}
.ar-x{display:inline-flex;align-items:center;justify-content:center;flex:none;width:36px;height:36px;border:0;border-radius:8px;background:transparent;color:var(--text-2);cursor:pointer}
.ar-x:hover{background:var(--row-hover);color:var(--text-1)}
.ar-field{display:flex;flex-direction:column;gap:6px;min-width:0;margin:0;padding:0;border:0}
.ar-field>label,.ar-field>legend{padding:0;font-size:14px;line-height:20px;font-weight:600;color:var(--text-1)}
.ar-field>legend{margin-bottom:6px}
.ar-in{width:100%;height:40px;padding:0 12px;border:1px solid var(--border-strong);border-radius:8px;background:var(--surface-sunken,var(--canvas));color:var(--text-1);font:400 15px/24px var(--font-sans);min-width:0}
.ar-in::placeholder{color:var(--text-3,var(--text-2))}
.ar-advert{height:auto;min-height:240px;padding:8px 12px;resize:vertical;font:400 14px/20px var(--font-sans)}
.ar-in[aria-invalid="true"]{border-color:var(--negative)}
.ar-pair{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}
.seg-row{display:flex;flex-wrap:wrap;gap:8px}
.seg{position:relative;display:inline-flex}
.seg input{position:absolute;opacity:0;width:1px;height:1px}
.seg span{display:inline-flex;align-items:center;height:36px;padding:0 14px;border:1px solid var(--border-strong);border-radius:8px;font-size:14px;line-height:20px;color:var(--text-1);cursor:pointer;white-space:nowrap}
.seg input:checked+span{border:1.5px solid #a79cf0;padding:0 13.5px;background:rgb(167 156 240 / .16);font-weight:600}
.seg input:focus-visible+span{outline:2px solid var(--focus);outline-offset:2px}
.ar-rate-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}
.ar-amt{width:120px;flex:none}
.ar-to{font-size:14px;line-height:20px;color:var(--text-2)}
.ar-err{font-size:14px;line-height:20px;color:var(--negative)}
.ar-fee{display:flex;align-items:center;gap:8px;margin-top:12px}
.ar-fee[hidden]{display:none}
.ar-fee label{font-size:14px;line-height:20px;font-weight:600}
.ar-fee-in{width:72px}
.ar-pct{font-size:14px;line-height:20px;color:var(--text-2)}
.ar-about{margin-top:8px;font-size:14px;line-height:20px;color:var(--text-2)}
.ar-about:empty{display:none}
.ar-edit{min-height:24px;padding:0;border:0;background:none;color:var(--text-2);font:500 14px/20px var(--font-sans);text-decoration:underline;text-underline-offset:3px;cursor:pointer}
.ar-edit:hover{color:var(--text-1)}
.ar-err:empty{display:none}
.ar-foot{display:flex;flex-wrap:wrap;align-items:center;gap:12px 16px;flex:none;padding:16px 24px 24px;border-top:1px solid var(--border-subtle)}
.ar-cost{font-size:14px;line-height:20px;color:var(--text-2)}
.ar-acts{display:flex;align-items:center;gap:16px;margin-left:auto}
.ar-cancel{min-height:36px;padding:0;border:0;background:none;color:var(--text-1);font:500 14px/20px var(--font-sans);cursor:pointer}
.ar-cancel:hover{text-decoration:underline}
.ar-go{display:inline-flex;align-items:center;height:36px;padding:0 16px;border:1px solid #a79cf0;border-radius:8px;background:#a79cf0;color:#15122e;font:600 14px/20px var(--font-sans);cursor:pointer}
.ar-go:disabled{opacity:.6;cursor:progress}
.ar-dlg[data-mode="edit"] [data-new],.ar-dlg[data-mode="edit"] .ar-cost{display:none}
@media (max-width:767.98px){
  .ar-dlg{width:calc(100vw - 32px);margin:16px auto}
  .ar-top{padding:20px 16px 12px}
  .ar-body{padding:4px 16px 16px}
  .ar-foot{padding:16px 16px 20px}
  .ar-pair{grid-template-columns:minmax(0,1fr)}
  .ar-acts{margin-left:0}
}
@media (pointer:coarse){.seg span,.ar-go,.ar-cancel,.ar-x,.ar-edit{min-height:44px}.ar-x{min-width:44px}}
"""


SCRIPT = r"""
(function () {
  var dlg = document.getElementById('ar-dlg');
  if (!dlg) return;
  var form = document.getElementById('ar-form');
  var opener = document.getElementById('ar-open');
  var go = document.getElementById('ar-go');
  var MISSING = JSON.parse(dlg.getAttribute('data-missing') || '{}');
  var lastOpener = null;
  var typeTouchedPeriod = false;
  function q(sel) { return form.querySelector(sel); }
  function val(name) { var el = form.elements[name]; return el ? el.value : ''; }
  function choice(name) { var c = q('input[name="' + name + '"]:checked'); return c ? c.value : ''; }
  function setChoice(name, value) {
    Array.prototype.forEach.call(form.querySelectorAll('input[name="' + name + '"]'), function (r) {
      r.checked = (r.value === value);
    });
  }
  function clearErrors() {
    Array.prototype.forEach.call(form.querySelectorAll('[data-err]'), function (p) { p.textContent = ''; });
    Array.prototype.forEach.call(form.querySelectorAll('[aria-invalid]'), function (el) { el.removeAttribute('aria-invalid'); });
  }
  function showErrors(errors) {
    var first = null;
    Object.keys(errors).forEach(function (key) {
      var p = form.querySelector('[data-err="' + key + '"]') || form.querySelector('[data-err="form"]');
      p.textContent = errors[key];
      var field = document.getElementById(key === 'rate' ? 'ar-min' : 'ar-' + key);
      if (field) { field.setAttribute('aria-invalid', 'true'); if (!first) first = field; }
    });
    if (first) first.focus();
  }
  function openDlg(mode, data, from) {
    lastOpener = from || opener;
    dlg.setAttribute('data-mode', mode);
    form.reset();
    var feeIn = document.getElementById('ar-fee');
    if (feeIn) feeIn.value = feeIn.getAttribute('data-default') || '';
    clearErrors();
    typeTouchedPeriod = false;
    if (data) fill(data);
    go.textContent = mode === 'edit' ? 'Save' : 'Add and score';
    document.getElementById('ar-h').textContent = mode === 'edit' ? 'Edit the role' : 'Add a role';
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
    document.getElementById('ar-link').focus();
    if (window.arChanged) window.arChanged();
  }
  function closeDlg() {
    if (dlg.close) dlg.close(); else dlg.removeAttribute('open');
    if (lastOpener) lastOpener.focus();
  }
  function fill(d) {
    ['link', 'client', 'location', 'rate_min', 'rate_max', 'fee'].forEach(function (k) {
      if (k === 'fee' && !d[k]) return;    // no fee of its own: the Settings figure stays
      if (form.elements[k] && d[k] !== undefined && d[k] !== null) form.elements[k].value = d[k];
    });
    if (d.type) setChoice('type', d.type);
    if (d.source) setChoice('source', d.source);
    if (d.currency) setChoice('currency', d.currency);
    if (d.period) { setChoice('period', d.period); typeTouchedPeriod = true; }
  }
  window.arOpen = openDlg;
  // What a rate comes to, as it is typed: converted at the Settings rate, an
  // hourly figure times eight, Upwork's fee taken off, to the nearest £5. The
  // same sums as radar_common.about_words.
  var MONEY = JSON.parse(dlg.getAttribute('data-money') || '{}');
  var feeRow = document.getElementById('ar-fee-row');
  var about = document.getElementById('ar-about');
  function amount(v) {
    v = String(v || '').replace(/[,£$€\s]/g, '');
    if (!/^[0-9]+(\.[0-9]+)?$/.test(v)) return null;
    var n = parseFloat(v);
    return n > 0 ? n : null;
  }
  function pounds(n) { return '£' + String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }
  function five(n) { return Math.round(n / 5) * 5; }
  window.arChanged = function () {
    var upwork = choice('source') === 'upwork';
    feeRow.hidden = !upwork;
    var lo = amount(val('rate_min')), hi = amount(val('rate_max'));
    if (lo === null) { about.textContent = ''; return; }
    if (hi === null || hi < lo) hi = lo;
    var cur = choice('currency'), per = choice('period');
    var fee = upwork ? parseFloat(String(val('fee')).replace('%', '')) : 0;
    if (!(fee >= 0 && fee <= 99)) fee = 0;
    if (cur === 'GBP' && per !== 'hour' && !fee) { about.textContent = ''; return; }
    var rate = cur === 'GBP' ? 1 : MONEY[cur];
    if (!rate) { about.textContent = 'Check the rate'; return; }
    var k = (per === 'hour' ? 8 : 1) * rate * (1 - fee / 100);
    var a = five(lo * k), b = five(hi * k);
    var words = 'About ' + pounds(a) + (b !== a ? ' to ' + pounds(b) : '')
      + ({ hour: ' a day', day: ' a day', year: ' a year' }[per] || '');
    if (fee) words += " after Upwork's " + fee + '%';
    about.textContent = words;
  };
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('[data-ar-edit]') : null;
    if (!btn) return;
    var d = JSON.parse(btn.getAttribute('data-ar-edit'));
    dlg.setAttribute('data-id', String(d.id));
    openDlg('edit', d, btn);
  });
  if (opener) opener.addEventListener('click', function () { openDlg('add', null, opener); });
  document.getElementById('ar-x').addEventListener('click', closeDlg);
  document.getElementById('ar-cancel').addEventListener('click', closeDlg);
  form.addEventListener('change', function (e) {
    if (e.target.name === 'period') typeTouchedPeriod = true;
    if (e.target.name === 'type' && !typeTouchedPeriod) {
      setChoice('period', { contract: 'day', permanent: 'year', 'fixed-price': 'fixed' }[e.target.value] || 'day');
    }
    if (window.arChanged) window.arChanged();
  });
  form.addEventListener('input', function () { if (window.arChanged) window.arChanged(); });
  function body() {
    var b = { link: val('link'), client: val('client'), location: val('location'),
              type: choice('type'), source: choice('source') || 'other',
              rate_min: val('rate_min'), rate_max: val('rate_max'),
              currency: choice('currency'), period: choice('period') };
    if (form.elements.fee) b.fee = val('fee');
    if (dlg.getAttribute('data-mode') === 'add') { b.title = val('title'); b.advert = val('advert'); }
    else b.id = Number(dlg.getAttribute('data-id'));
    return b;
  }
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    clearErrors();
    var mode = dlg.getAttribute('data-mode');
    if (mode === 'add') {
      var missing = {};
      if (!val('title').trim()) missing.title = MISSING.title;
      if (!val('advert').trim()) missing.advert = MISSING.advert;
      if (Object.keys(missing).length) { showErrors(missing); return; }
    }
    go.disabled = true;
    fetch(mode === 'edit' ? '/jobs/edit-role' : '/jobs/add', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body())
    }).then(function (r) { return r.json().catch(function () { return { ok: false }; }); })
      .then(function (j) {
        go.disabled = false;
        if (j && j.errors) { showErrors(j.errors); return; }
        if (!j || !j.ok) { showErrors({ form: (j && j.note) || 'That did not stick.' }); return; }
        var id = j.existing || j.id;
        if (dlg.close) dlg.close(); else dlg.removeAttribute('open');
        window.location.href = '/jobs/' + id;
      })
      .catch(function () { go.disabled = false; showErrors({ form: 'The server did not answer.' }); });
  });
})();
"""


def dialog_with_data(config: dict | None = None) -> str:
    """The dialog with what its script needs: the words for a missing field,
    and the exchange rates and Upwork fee from Settings for the line that says
    what a rate comes to (radar_common.read_money_prefs)."""
    import radar_common as rc
    money = rc.read_money_prefs(config)
    fee = rc._plain_number(money["fee"])
    data = {"USD": money["USD"], "EUR": money["EUR"], "fee": money["fee"]}
    return dialog_html(fee).replace(
        'data-mode="add">',
        f'data-mode="add" data-missing="{esc(json.dumps(hand_roles.MISSING))}" '
        f'data-money="{esc(json.dumps(data))}">', 1)


def edit_link(row) -> str:
    """The small Edit on a role added by hand, which opens the dialog on the
    fields Edit may change."""
    if not hand_roles.is_hand(row):
        return ""
    return (f'<button type="button" class="ar-edit" data-ar-edit="{esc(json.dumps(hand_roles.edit_data(row)))}" '
            'aria-haspopup="dialog">Edit<span class="visually-hidden"> the role</span></button>')
