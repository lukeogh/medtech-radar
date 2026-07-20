# Compliance cost article. Skeleton, draft introduction, guidance.

> **GATE. NOT FOR PUBLICATION IN ANY FORM.**
>
> Nothing in this file publishes until two things have happened. Luke supplies
> the dissertation so every figure is checked against source, and client
> anonymisation is confirmed in writing. Every spot that needs sign-off is
> marked [CONFIRM BEFORE PUBLISH]. The programme is described only as "a
> Class C IVD programme". No employer or client name appears anywhere in this
> draft, and none may be added at any stage.

## Working titles.

Four options in the right register. Option one is the working default.

1. What compliance actually costs
2. The bill for Class C. Four years of data on the cost of compliance
3. 1,007 hours. What a compliant toolchain really costs [CONFIRM BEFORE PUBLISH. Uses the headline figure in the title, verify it first.]
4. Nobody budgets for the toolchain

Titles two and three promise data, which is the article's whole advantage, so
lean that way if the verified numbers hold up.

## Structure.

1. The question founders ask too late
2. The dataset
3. The four-domain framework
4. The 1,007-hour figure and what drives it
5. What this means at seed stage
6. A plain close

## Draft introduction. About 200 words.

Nobody starts a diagnostics company to write documents. You start with a
measurement that works and a disease that matters, and for a long while the
only numbers that count are sensitivity, specificity and runway. Then a term
sheet lands, someone says IEC 62304, and a line called regulatory appears in
the plan with no number against it.

I have spent four years running software development on a Class C IVD
programme [CONFIRM BEFORE PUBLISH. Check this description cannot identify the
client, and that four years is the figure the dissertation supports.]. Class C
is the deep end of the IVD world, the class where a wrong result can cost a
life, and the standards treat it accordingly. Because the programme ran a
configured ALM from the start, every requirement, test, review and document
left a timestamped trail. I went back through four years of that trail and
counted.

This article is what the counting showed. The toolchain alone absorbed 1,007
hours [CONFIRM BEFORE PUBLISH. The headline figure. Verify against the
dissertation before any use.] before it paid back a single one. More usefully,
the cost splits into four domains you can reason about at seed stage, and
three early decisions set most of the bill.

If you are a founder, you want these numbers before your first QA hire, not
after.

## Section guidance.

### 1. The question founders ask too late.

- The introduction above is this section. Keep it under 220 words.
- Open with the founder's world, not with standards. The reader should be
  forty words in before any standard is named.
- The promise is specific. Real numbers from a real programme, not opinion.
- No credentials paragraph. The dataset is the credential.

### 2. The dataset.

- One short section. What was counted, where it came from, what was left out.
- Source is four years of ALM and document-register data from a Class C IVD
  programme. [CONFIRM BEFORE PUBLISH. Client anonymisation of the whole
  description, including timeframe if it narrows identification.]
- Say plainly how the data was anonymised and that the client agreed.
  [CONFIRM BEFORE PUBLISH. This sentence can only be written once agreement
  actually exists.]
- Naming the ALM product is a choice to make late. It adds credibility but
  narrows the field of programmes this could be. [CONFIRM BEFORE PUBLISH.
  Tool naming decision.]
- State the counting method in two sentences. Hours from timestamps and
  records, not from memory or estimate. That single point beats every
  compliance-cost blog post already out there.
- Include team size and programme scale only in rounded bands, if at all.
  [CONFIRM BEFORE PUBLISH. Scale details are the easiest way to identify a
  programme.]

### 3. The four-domain framework.

- Name it once, "the four-domain framework", and use the name consistently.
  It is a library asset and may get its own page later.
- The four domains, one paragraph each:
  - risk classification
  - software architecture
  - toolchain configuration
  - process maturity
- For each domain, one sentence on what it covers, one on how it drives cost,
  one concrete anonymised example. [CONFIRM BEFORE PUBLISH. Every example
  drawn from the programme needs the same anonymisation check as the
  dataset.]
- The claim to land here is that these four are independent levers. A team
  can be strong in three and still bleed hours in the fourth.
- No figures yet in this section. The framework earns the reader's trust
  before the numbers ask for it.

### 4. The 1,007-hour figure and what drives it.

- The centrepiece. One headline figure, then its breakdown.
  [CONFIRM BEFORE PUBLISH. The figure, its components, and every sub-figure
  in this section come from the dissertation and are unpublishable until
  checked.]
- Show what the hours were spent on, configuration, workflow design,
  traceability plumbing, validation of the toolchain itself, training and
  rework. [CONFIRM BEFORE PUBLISH. The actual breakdown categories must
  match the dissertation, this list is a placeholder.]
- Make clear the cost is configuration and process, not licence fees. This
  is not a complaint about any vendor and must not read as one.
- Compare against what a founder would have guessed. The gap between guess
  and measurement is the story.
- A simple table or one chart at most. The prose carries the argument.

### 5. What this means at seed stage.

- Turn the data into the three decisions that halve the bill.
  [CONFIRM BEFORE PUBLISH. "Halve" is a quantified claim, use whatever
  reduction the dissertation actually supports.]
- Working candidates for the three decisions, to confirm or replace from the
  dissertation:
  - classify honestly and early rather than re-classifying under audit
    pressure
  - architect so the highest-class footprint stays small
  - configure the toolchain before the team grows instead of after

  [CONFIRM BEFORE PUBLISH. The three decisions must be the dissertation's,
  not this placeholder list.]
- Address the seed-stage founder directly. Money is short, the CTO is doing
  five jobs, and none of these decisions needs a hire to make.
- No fear-selling. The tone is a colleague passing on the map, not a vendor
  describing the minefield.

### 6. A plain close.

- Three or four sentences. Restate the one number and the one idea, that
  compliance cost is decided early, mostly by people who did not know they
  were deciding it.
- End with where the framework lives, a single line, no pitch. The site
  version may add one line noting that a fixed-scope IEC 62304 gap
  assessment exists. Decide at publication, and it is one line either way.

## Anonymisation checklist. Run before any draft leaves this repo.

- [ ] No employer or client names anywhere, in text, filenames or metadata.
- [ ] Programme described only as "a Class C IVD programme".
- [ ] No product, analyte or technology description precise enough to
      identify the programme.
- [ ] No combination of dates, locations and round sizes that narrows it.
- [ ] No investor names tied to the programme.
- [ ] Team sizes and scale figures rounded into bands, or removed.
- [ ] Tool naming decision made deliberately, not by default.
- [ ] Every [CONFIRM BEFORE PUBLISH] marker resolved and removed.
- [ ] Dissertation figures checked against source by Luke personally.
- [ ] Client sign-off on the anonymised text, in writing, filed outside this
      repo.
