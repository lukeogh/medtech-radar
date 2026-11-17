# Page snapshots

One file per page or main view, as the server draws it from the invented
samples at a fixed clock (13 Nov 2026, 09:14 UTC). `test/run_snapshot_test.py`
makes them and compares with them; its docstring says how, and what is held
still.

Each `<style>` and `<script>` block is stored as a hash of its content and its
length, so a style or script change still shows as a change. A line break is
added before each block-level opening tag so that a diff names the element.

## Commands

Which pages did this build change? One line, always exits 0:

    python test/run_snapshot_test.py --summary

Check, as the suite does. Fails with the list of pages that differ:

    python test/run_snapshot_test.py

What changed on one page:

    python test/run_snapshot_test.py --diff jobs-open

Rewrite the stored copies. Only on purpose, after reading the diffs, and in
the same commit as the change that moved them:

    python test/run_snapshot_test.py --update

## The views

| File | Address |
| --- | --- |
| home-todo | `/` (one item already taken off with Done) |
| home-done | `/?show=done` |
| jobs-open | `/jobs` |
| jobs-reading-open | `/jobs?reading=1` |
| jobs-applied-list | `/jobs?show=applied&layout=list` |
| jobs-applied-board | `/jobs?show=applied` |
| jobs-acknowledged | `/jobs?show=acknowledged` |
| jobs-filtered | `/jobs?show=filtered` |
| role-top, role-question, role-reading, role-filtered, role-applied | `/jobs/<id>`, one role pane per tier, and an applied role |
| prepare | `/prepare/<id>`, the top role with the stored advert |
| insights | `/insights` |
| insights-story | `/insights/story/<id>`, the first story the front page links |
| watchlist | `/watchlist` |
| watchlist-company | `/watchlist/<id>`, Hartwell Infusion Ltd |
| company-dossier | `/company/<id>`, the same company's older dossier |
| cv | `/cv`. Its Settings tab is `/cv#settings`, the same page: the fragment never reaches the server |
| radar-health | `/radar` |
| archive | `/archive` |
