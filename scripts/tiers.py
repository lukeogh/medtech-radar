"""The three open tiers, said once: their names, their colours and the one
line on what puts a role in each. The Jobs page and Home read them from here.

The tier itself is decided in code (score_item.derive_tier); the meanings
below say that rule in words. Colour lives in the tier's pill only: Top
prospects takes Radar's teal, One question away a violet and Reading grey,
clear of the green, red and amber that mean passes, fails and caution.
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

# The pill's fill, as CSS colour values on the dark palette. Each carries
# dark text (--on-accent) at better than 7 to 1.
COLOURS = {"top": "var(--accent)", "question": "#b9a2f2", "reading": "#a3aeb6"}

PILL_CSS = (
    ":root{" + "".join(f"--tier-{k}:{v};" for k, v in COLOURS.items()) + "}"
    ".tier-pill{display:inline-flex;align-items:center;height:24px;padding:0 10px;"
    "border:0;border-radius:12px;background:var(--neutral);color:var(--on-accent);"
    "font:600 14px/20px var(--font-sans);white-space:nowrap;cursor:pointer}"
    "button.tier-pill:hover{filter:brightness(1.08)}"
    "span.tier-pill{cursor:default}"
    + "".join(f".tier-pill.t-{k}{{background:var(--tier-{k})}}" for k in COLOURS)
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
