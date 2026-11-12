# What Radar costs to run

An honest estimate of the Claude API bill. Everything else is free or already paid for.

## Pricing used

| Model | Input per M tokens | Output per M tokens | Used for |
|---|---|---|---|
| claude-haiku-4-5 | $1 | $5 | extraction |
| claude-sonnet-4-6 | $3 | $15 | scoring |

## The workload

Assume 30 to 60 new adverts a week survive deduplication.

**Extraction, Haiku.** Roughly 1,000 tokens in and 300 out per advert. That is $0.0025 each, so $0.08 to $0.15 a week.

**Scoring, Sonnet.** Roughly 3,000 tokens in per advert, most of it the system prompt carrying the rubric, CV and preferences, plus about 400 out. Uncached that is about $0.015 each, or $0.45 to $0.90 a week. The system prompt is marked for prompt caching, and batch scoring means repeat calls read it at about a tenth of the price. That cuts the effective cost to roughly $0.008 per advert, so $0.25 to $0.50 a week.

**Signal scoring, Sonnet.** A handful a day, call it 20 to 50 a week. Diff
sources now fetch the article page and score up to 4,000 characters of its
text rather than a bare headline, so a signal call carries roughly 1,000 to
1,500 extra input tokens. Better judgement for about half a cent more per
item. Add $0.30 to $0.70 a week.

**Prepare application, Sonnet.** One call each time you open a role's page by its Prepare application link and the role has a description that has not been scored, never in a batch and never on a schedule (by the plain http address the link opens a button, and the call is made when you press it). About 0.9 cent a role for the description alone and about 1.7 cents with the per-ask working. The reply is capped at 4,096 tokens, so the most one call can bill for its reply is about 6 cents, and a reply that runs into the cap is thrown away after being paid for. A role is scored once per version of its description, so opening its page again, reloading it or double clicking costs nothing, and a failed fetch costs nothing because nothing is sent. Not in the weekly figures below, which stay as they were, because it only happens when you press it.

## The numbers

| | Weekly | Monthly |
|---|---|---|
| Typical range | $0.60 to $1.20 | $2.50 to $5 |
| Worst case | about $3.50 | about $15 |

Worst case assumes double the volume, no cache hits at all, and a retry on every call. Even then it is lunch money. The real risk is not the steady state, it is a bug that loops. That is what the guardrails are for.

## Where to check

Every run logs its token usage to the `runs` table in `db/radar.sqlite`, per workflow, per model, per mode. If the bill ever surprises you, the ledger is one query away.

```sql
SELECT workflow, model, SUM(input_tokens), SUM(output_tokens)
FROM runs GROUP BY workflow, model;
```

## The three guardrails

1. **Dedupe before scoring.** An advert whose URL hash is already in the database never reaches the API. Re-running a workflow costs nothing.
2. **Batch with a cached system prompt.** Scoring calls share one stable system block with cache control on it, so the expensive part of the prompt is paid for roughly once per batch, not once per advert.
3. **One role, one call, once per description.** Prepare application takes a lock per role, scores a description once per version of it, makes one call with no retry, and does not pay again for an unusable reply for five minutes. A reply that was paid for and could not be stored, because the inbox run had the database busy, is held for half an hour and stored by the next click without a second call. `SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens) FROM runs WHERE workflow = 'prepare';` is what it has cost.
