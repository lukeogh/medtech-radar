"""The three open tiers, said once: their names, their colours and the one
line on what puts a role in each. The Jobs page and Home read them from here.

The tier itself is decided in code (score_item.derive_tier); the meanings
below say that rule in words. Colour lives in the tier's pill only, in the
Jobs group's purple (--group-jobs, page_frame; owner, 17 Nov): Top prospects
filled, One question away a tint of it with a rule, Reading grey. All three
are clear of the green, red and amber that mean passes, fails and caution,
and of the teal that now means Applications.
"""

from __future__ import annotations

import html

OPEN_TIERS = ("top", "question", "reading")

NAMES = {"top": "Top prospects", "question": "One question away", "reading": "Reading"}

# One role, as a role pane names its tier.
SINGULAR = {"top": "Top prospect", "question": "One question away", "reading": "Reading"}

MEANINGS = {
    "top": "Sector, CV match and Location pass, and Rate passes or is not stated.",
    "question": "Sector and CV match pass, and one of Location or Rate fails.",
    "reading": "Everything else: Sector or CV match fails, or Location and Rate both fail.",
}

# The pill's fill, its text and its rule, as CSS colour values on the dark
# palette (owner, 17 Nov). Text on fill: Top prospects 7.4 to 1; One question
# away at least 6 to 1 on any ground the pill sits on; Reading 11 to 1.
COLOURS = {"top": "var(--group-jobs)", "question": "rgba(167,156,240,0.18)",
           "reading": "#2a3238"}
INKS = {"top": "#15122e", "question": "#c9c2f8", "reading": "var(--text-1)"}
RULES = {"top": "transparent", "question": "rgba(167,156,240,0.45)",
         "reading": "transparent"}

PILL_CSS = (
    ":root{" + "".join(f"--tier-{k}:{v};--tier-{k}-ink:{INKS[k]};--tier-{k}-rule:{RULES[k]};"
                       for k, v in COLOURS.items()) + "}"
    ".tier-pill{display:inline-flex;align-items:center;height:24px;padding:0 10px;"
    "border:1px solid transparent;border-radius:12px;background:var(--neutral);color:var(--on-accent);"
    "font:600 14px/20px var(--font-sans);white-space:nowrap;cursor:pointer}"
    "button.tier-pill:hover{filter:brightness(1.08)}"
    "span.tier-pill{cursor:default}"
    + "".join(f".tier-pill.t-{k}{{background:var(--tier-{k});color:var(--tier-{k}-ink);"
              f"border-color:var(--tier-{k}-rule)}}" for k in COLOURS)
    + "@media (forced-colors:active){.tier-pill{border:1px solid CanvasText}}"
)


def pill_html(tier: str, label: str | None = None, toggle_id: str | None = None,
              singular: bool = False) -> str:
    """The tier's pill. With toggle_id it is a button that shows and hides the
    element of that id (the tier's meaning); without, a plain label."""
    words = label or (SINGULAR if singular else NAMES).get(tier, tier.capitalize())
    cls = f"tier-pill t-{html.escape(tier)}" if tier in COLOURS else "tier-pill"
    if toggle_id:
        return (f'<button type="button" class="{cls}" data-pill aria-expanded="false" '
                f'aria-controls="{html.escape(toggle_id)}">{html.escape(words)}</button>')
    return f'<span class="{cls}">{html.escape(words)}</span>'
