# What Radar costs to run

An honest estimate of the Claude API bill. Everything else is free or already paid for.

## Pricing used

| Model | Input per M tokens | Output per M tokens | Used for |
|---|---|---|---|
| claude-haiku-4-5 | $1 | $5 | extraction |
| claude-sonnet-4-6 | $3 | $15 | scoring |

A cached prompt is read at a tenth of the input price and written, the first time, at one and a quarter times it: $0.30 and $3.75 a million on Sonnet.

## The workload

Assume 30 to 60 new adverts a week survive deduplication.

**Extraction, Haiku.** Roughly 1,000 tokens in and 300 out per advert. That is $0.0025 each, so $0.08 to $0.15 a week.

**Scoring, Sonnet.** Roughly 3,150 tokens in per advert, most of it the system prompt carrying the rubric, CV and preferences, plus about 580 out, the four reasons and their quotes among them. Uncached that is about $0.018 each, or $0.55 to $1.10 a week. The system prompt is marked for prompt caching, and batch scoring means repeat calls read it at about a tenth of the price. That cuts the effective cost to roughly $0.011 per advert, so $0.33 to $0.66 a week.

**The quoted reasons, on both paths.** Every score asks for up to four reasons, each with a quote of 25 words or fewer from the advert. On a card they add 632 bytes to the rubric, about 160 tokens of input (210 at a pessimistic three characters a token), which the cache serves at $0.30 a million after the first advert of a run and writes at $3.75 a million on the first, and about 180 tokens to the reply for four typical reasons, at $15 a million. That is about 0.27 cents more a score with the cache warm and 0.33 cents with it cold, $0.08 to $0.20 a week at the volumes above, and it is inside the scoring figures above. On a full description the prompt grows by 761 bytes, about 190 tokens, and the reply by the same 180 or so, about 0.34 cents more a click. The most four reasons can add to one reply is about 580 tokens, four of 20 words with quotes of 25, which is about 0.9 cents on either path. A card's reply is now capped at 2,048 tokens, its own ceiling, where it had the extractor's 1,024, and a full description's at 6,144 where it was 4,096. So the most a runaway reply can bill for what it writes is about 3 cents on a card, and a card asks once more when its reply is cut off, so about 6 cents for one score, and about 9 cents on a full description, which makes one call. What is billed is what the model writes, so the ceilings cost nothing until a reply reaches them.

**Signal scoring, Sonnet.** A handful a day, call it 20 to 50 a week. Diff
sources now fetch the article page and score up to 4,000 characters of its
text rather than a bare headline, so a signal call carries roughly 1,000 to
1,500 extra input tokens. Better judgement for about half a cent more per
item. Add $0.30 to $0.70 a week. Since the Insights news page (scorer v1.2) each reply also carries a
type and a one-sentence card summary, about 40 tokens more, a few hundredths of a cent a signal. The
one-off scripts/backfill_stories.py --live reads the last 90 days again at one call a row, roughly
0.7 to 1.4 cents each; its dry run says how many rows first. On the box on 15 Nov 2026 it read 215
rows for 42,910 input and 42,319 output tokens, about $0.76, or 0.35 cent a row.

**Prepare application, Sonnet.** One call each time you open a role's page by its Prepare application link and the role has a description that has not been scored, never in a batch and never on a schedule (by the plain http address the link opens a button, and the call is made when you press it). About 0.9 cent a role for the description alone, about 1.7 cents with the per-ask working and about 2 cents with the quoted reasons as well. The reply is capped at 6,144 tokens, so the most one call can bill for its reply is about 9 cents, and a reply that runs into the cap is thrown away after being paid for. A role is scored once per version of its description, so opening its page again, reloading it or double clicking costs nothing, and a failed fetch costs nothing because nothing is sent. Not in the weekly figures below, because it only happens when you press it.

## The numbers

| | Weekly | Monthly |
|---|---|---|
| Typical range | $0.70 to $1.50 | $3 to $6.50 |
| Worst case | about $7.75 | about $33 |

Each row is the sum of the lines above it, extraction, scoring and signal scoring, at their own prices, and a month is 4.33 weeks. The typical range is $0.08 to $0.15, $0.33 to $0.66 and $0.30 to $0.70, which is $0.71 to $1.51 a week. Worst case assumes double the volume, no cache hits at all, and a retry on every call: 120 adverts each extracted and scored twice, 240 at $0.0025 and 240 at the uncached $0.018, and 100 signals each scored twice at about 1.4 cents, which is $0.60, $4.32 and $2.80, about $7.72 a week. Even then it is lunch money. The real risk is not the steady state, it is a bug that loops. That is what the guardrails are for.

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
