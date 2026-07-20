# Announcement day. The playbook.

Radar fires when a company worth knowing does something that matters. A funding
round, a spin-off launch, an accelerator place, a first hire that says the
regulatory work is getting real. This file is the exact sequence for that day
and the weeks after. Improvise the words, never the structure.

Two ideas sit underneath everything here.

The job is memory, not attention. You cannot sell a solution to someone who has
not met the problem. Founders meet IEC 62304 months after the round closes, so
the aim today is only to become the name already attached to the problem on the
day it finally lands on their desk.

The peer frame is the whole trick. A software director at one imec diagnostics
spin-off congratulating the founder of another is a colleague, not a vendor.
Every template below stays inside that frame. The moment you pitch, the frame
breaks and cannot be rebuilt.

## Step 1. Same day. The public comment.

Comment on the company's announcement post, or the lead investor's, whichever
is getting the traffic. From the personal profile, never the company page.

One genuine technical observation plus congratulations. No pitch, no standards,
no link. The observation is the whole comment. It should be specific enough
that only someone who has built one of these things could have written it.

Template:

> Congratulations to the [company] team. [One or two sentences on a technical
> choice visible in the announcement and why it is the right call, or what it
> makes easier later.] [Short closing wish.]

Worked example one. Cantilex Dx, an imaginary Ghent spin-off that has raised a
seed round for a silicon photonics protein biosensor.

> Congratulations to the Cantilex team. Getting evanescent-field sensing off
> the optical bench and into a packaged cartridge is the step that kills most
> photonics diagnostics, and keeping the optics passive so the complexity sits
> in the reader looks like the right trade for cost per test. Good luck with
> the build.

Worked example two. Mireva Biosystems, an imaginary Eindhoven company entering
an accelerator cohort with a microfluidic point-of-care analyser.

> Congratulations to Mireva Biosystems on the cohort place. Sealed pre-loaded
> reagent cartridges are the right call at the point of care. Shelf life
> becomes a chemistry problem instead of a user training problem, and chemistry
> problems get fixed in the lab rather than in the field. Looking forward to
> the first data.

Writing yours. Read the announcement twice and find the one technical decision
you actually have a view on. If you have no view, congratulate a named
engineering achievement instead and keep it shorter. Never comment on the
money.

## Step 2. Same day. The connection note.

Send a connection request to the CEO or CTO. CTO first where one exists. The
note must stay under 300 characters, which is the LinkedIn limit, so every
word has to earn its place.

The standard note, 230 characters. One change from the original working draft,
ISO added before 13485 for precision. Everything else stays because it works.

> Congratulations on the round. I run software at another imec diagnostics
> spin-off, so I know a little of the road ahead. If IEC 62304 or ISO 13485
> ever land on your desk, happy to compare notes. Always good to know the
> neighbours.

Why it works. "Another imec diagnostics spin-off" does the introduction and
the peer frame in five words. Naming the standards plants the memory without
selling anything. "Compare notes" is an offer between equals. The last line is
warm and asks for nothing.

Variant for a company outside the imec ecosystem, 237 characters:

> Congratulations on the round. I run software at a Belgian photonics
> diagnostics spin-off, so I know a little of the road ahead. If IEC 62304 or
> ISO 13485 ever land on your desk, happy to compare notes. Always good to
> know the neighbours.

Variant for an accelerator place rather than a round, 237 characters:

> Congratulations on the cohort place. I run software at another imec
> diagnostics spin-off, so I know a little of the road you're starting. If
> IEC 62304 or ISO 13485 ever come up, happy to compare notes. Always good to
> know the neighbours.

Then log both touches:

    python scripts/touch.py add "Company" --channel comment --note "congrats comment on seed post"
    python scripts/touch.py add "Company" --channel connection-note --note "note to CTO" --next "week-3 engagement" --next-date YYYY-MM-DD

## Step 3. Week three. One engagement.

Around three weeks after the announcement, engage once with something the
company or founder has posted. A comment, or a share with one line of your
own. Genuine and brief, two sentences at most. No standards, no links, no
reference back to the earlier note.

If they have posted nothing worth engaging with, skip it. A forced comment
reads as exactly what it is. Leave the pending action open until something
real appears.

Log it with channel engagement and set the next action to watching for the
buying window, with no date:

    python scripts/touch.py add "Company" --channel engagement --note "commented on their packaging post" --next "watch for buying window"

## Step 4. The buying window.

Radar watches for the moment the company first hires for QA, regulatory or
software leadership, or posts its first software job advert. That is the week
IEC 62304 and ISO 13485 stop being abstract for them. This touch can be months
after the last one. That gap is normal and does no harm.

Send a short message with the compliance-cost article. It is the useful
artefact and it does the selling by not selling. The gap assessment appears
once, in the final line, and never earlier.

Template:

> Hi [first name]. I saw the [role] opening go up, which usually means the
> regulatory side is getting real. I wrote up what compliance actually cost
> across four years of a Class C IVD programme, with the numbers. It might
> save you and your first hire some time. [article link]
>
> If it would ever help to put a number on where you stand, I also run a
> fixed-scope IEC 62304 gap assessment.

If the article is not yet published, do not improvise a substitute artefact.
Send the first paragraph without the link, offer to compare notes, and leave
the assessment line out entirely.

Log it:

    python scripts/touch.py add "Company" --channel artefact --note "sent compliance-cost article after first QA advert" --next "reply or nothing, no follow-up"

## The rules.

1. Never pitch in first contact. The first two touches contain no offer, no
   link and no ask. If a line feels like selling, cut it.
2. One live thread per company. Never run a comment thread, a message thread
   and an email thread at once. Pick the one that is alive and let the others
   rest.
3. Log every touch. Ten seconds with scripts/touch.py. If it is not in the
   tracker it did not happen, and the Monday digest cannot chase what it
   cannot see.
4. Silence is fine. No reply to the connection note means nothing at all. The
   next contact is the buying-window one, months later, and that is fine. Do
   not follow up in between.

The playbook ends at the buying window. Everything after that is an ordinary
professional conversation, and those need no template.
