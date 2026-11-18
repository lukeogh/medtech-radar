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


def dialog_html() -> str:
    """The dialog, closed. Contract is chosen to begin with, the rate in pounds
    a day; no source is chosen until one is."""
    currencies = [(k, sign) for k, sign in hand_roles.CURRENCIES]
    return f"""<dialog class="ar-dlg" id="ar-dlg" aria-labelledby="ar-h" data-mode="add">
<form class="ar-form" id="ar-form" novalidate>
<div class="ar-top"><h2 id="ar-h">Add a role</h2>
<button type="button" class="ar-x" id="ar-x" aria-label="Close">{CLOSE}</button></div>
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
</div>{_err("rate")}</fieldset>
<p class="ar-err ar-form-err" data-err="form" aria-live="polite"></p>
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
.ar-form{display:flex;flex-direction:column;gap:16px;padding:24px}
.ar-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
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
.ar-err:empty{display:none}
.ar-foot{display:flex;flex-wrap:wrap;align-items:center;gap:12px 16px;margin-top:4px;padding-top:16px;border-top:1px solid var(--border-subtle)}
.ar-cost{font-size:14px;line-height:20px;color:var(--text-2)}
.ar-acts{display:flex;align-items:center;gap:16px;margin-left:auto}
.ar-cancel{min-height:36px;padding:0;border:0;background:none;color:var(--text-1);font:500 14px/20px var(--font-sans);cursor:pointer}
.ar-cancel:hover{text-decoration:underline}
.ar-go{display:inline-flex;align-items:center;height:36px;padding:0 16px;border:1px solid #a79cf0;border-radius:8px;background:#a79cf0;color:#15122e;font:600 14px/20px var(--font-sans);cursor:pointer}
.ar-go:disabled{opacity:.6;cursor:progress}
.ar-dlg[data-mode="edit"] [data-new],.ar-dlg[data-mode="edit"] .ar-cost{display:none}
@media (max-width:767.98px){
  .ar-dlg{width:calc(100vw - 32px);margin:16px auto}
  .ar-form{padding:20px 16px}
  .ar-pair{grid-template-columns:minmax(0,1fr)}
  .ar-acts{margin-left:0}
}
@media (pointer:coarse){.seg span,.ar-go,.ar-cancel,.ar-x{min-height:44px}.ar-x{min-width:44px}}
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
      if (form.elements[k] && d[k] !== undefined && d[k] !== null) form.elements[k].value = d[k];
    });
    if (d.type) setChoice('type', d.type);
    if (d.source) setChoice('source', d.source);
    if (d.currency) setChoice('currency', d.currency);
    if (d.period) { setChoice('period', d.period); typeTouchedPeriod = true; }
  }
  window.arOpen = openDlg;
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


def dialog_with_data() -> str:
    """The dialog with what its script needs to say a field is missing."""
    return dialog_html().replace(
        'data-mode="add">',
        f'data-mode="add" data-missing="{esc(json.dumps(hand_roles.MISSING))}">', 1)
