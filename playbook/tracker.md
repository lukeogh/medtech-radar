# Tracker. Where every touch gets logged.

Touches live in the touches table of db/radar.sqlite, the same database the
rest of Radar uses. The Monday digest reads that table to build its threads
awaiting action section, so an unlogged touch is invisible to the whole
system. One row per touch. Company, timestamp, channel, a free note, and
optionally the next planned action with a due date.

The channel is one of comment, connection-note, engagement, artefact or
other. They match the playbook steps.

## The commands.

Log a touch:

    python scripts/touch.py add "Cantilex Dx" --channel comment --note "congrats comment on seed post" --next "connection note to CTO" --next-date 2026-07-21

See recent touches, newest first, with an optional company filter:

    python scripts/touch.py list
    python scripts/touch.py list --company Cantilex

See what is waiting:

    python scripts/touch.py pending

pending shows the latest touch per company that has a next action set, dated
items first, and counts anything due or overdue at the bottom. Logging a
newer touch for the same company replaces its pending entry, so the way to
clear an action is to do it and log it. To park a company with nothing
planned, log a touch without --next.

All three commands accept --db PATH for a different database file. Tests use
a throwaway one. Day to day, leave it alone and the live database is used.
