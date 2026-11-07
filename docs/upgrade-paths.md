# Upgrade paths for the signal watcher.

The diff watcher in `scripts/check_signals.py` is deliberately small. It fetches a listing page, hashes the visible text and diffs the article links. That covers most of the watchlist, but it cannot see pages that build their listings with JavaScript, and tonight that already bites twice. The imec press listing renders client-side and New Electronics blocks plain fetches outright. The natural upgrade is changedetection.io, self-hosted in Docker, which fits the existing Proxmox and n8n setup. It renders JavaScript through a real browser, lets you scope each watch to a CSS selector so sidebar churn stops causing false changes, handles browser-like headers for the awkward sites, and pushes straight to ntfy. The migration is gentle. Keep this script as the scorer, point changedetection.io at the same URLs, and have its webhook hand new text to the scoring path. Worth doing once more than two or three sources go dark to plain HTTP.

The paid route is data rather than plumbing. A full Dealroom subscription, or PitchBook or CB Insights, would catch the quiet top-up rounds that never get a press release, and the Axithra history shows those happen. Sifted Pro adds decent European startup coverage. The honest verdict at one-person scale is that none of them earn their cost yet. These products price for funds and corporate development teams, typically thousands of pounds a year, and their edge over free Dealroom alerts plus this watchlist is a handful of quiet rounds annually in a niche this narrow. The free email alerts already cover the imec, Ghent and Leuven ecosystem that matters most. Revisit only if a real buying window passes unseen and a database would have caught it. That miss has a price, and the day it exceeds the subscription is the day to pay.

**Superseded for imec, 3 August 2026.** The trigger above was met and the answer
turned out to be smaller than the proposal. imec's listing does render
client-side, but imec also publishes `sitemap_press.xml`, server-rendered XML,
robots-allowed, 821 urls of which 301 are press releases. Reading that needs no
browser, no new container and no new service, so `check_signals.py` gained a
third method, `sitemap`, instead. Watch out for two things if this pattern is
extended. A sitemap is the whole site, so `include_patterns` whitelists the part
that is news, and imec's carries 279 job vacancies and 240 research papers beside
its press. And `lastmod` is partly rebuild-driven, hundreds of entries restamped
in a day, so detection keys on urls never seen before and ignores dates entirely.

changedetection.io still stands for a source that genuinely cannot be read any
other way. Two things to weigh first, from the reconnaissance on 3 August. Neither
host has comfortable room for a browser container, LXC 110 had 1.7GB free of 3GB
while running n8n and Postgres, and the Pi runs Pi-hole and the proxy. And New
Electronics returns 403 to non-browser clients while its robots allows `*`, so
driving a headless browser at it would be circumventing an access control the
site chose deliberately rather than following one it published. That is a
decision for Luke, not a technical detail.
